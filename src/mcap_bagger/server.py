"""Trusted-LAN recording controls and confined file management."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import re
import stat
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .pipeline import Pipeline


class StartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filename: str


class RecordingManager:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.pipeline = None
        self.state = 'idle'
        self.filename = None
        self.error = None
        config.output_dir.mkdir(parents=True, exist_ok=True)

    def status(self):
        with self.lock:
            if self.pipeline and self.pipeline.done.is_set() and self.state == 'recording':
                self.error = str(self.pipeline.error) if self.pipeline.error else None
                self.state = 'error' if self.error else 'idle'
            return dict(state=self.state, filename=self.filename, error=self.error)

    def start(self, name):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}(?:\.mcap)?', name):
            raise HTTPException(422, 'Use 1–64 letters, digits, underscores or hyphens, optionally followed by .mcap')
        name = name if name.endswith('.mcap') else name + '.mcap'
        with self.lock:
            if self.status()['state'] in ('starting', 'recording', 'stopping'):
                raise HTTPException(409, 'A recording is already active')
            if any(os.path.lexists(self.config.output_dir / n) for n in (name, name + '.partial')):
                raise HTTPException(409, 'Recording name already exists')
            self.state, self.filename, self.error = 'starting', name, None
            self.pipeline = Pipeline(self.config, name)
            try:
                self.pipeline.start()
            except Exception as exc:
                self.state, self.error = 'error', str(exc)
                raise HTTPException(500, str(exc)) from exc
            self.state = 'recording'
            return self.status()

    def stop(self):
        with self.lock:
            if self.status()['state'] != 'recording':
                raise HTTPException(409, 'No active recording')
            self.state = 'stopping'
            pipeline = self.pipeline
        try:
            pipeline.finish()
        except Exception as exc:
            with self.lock:
                self.state, self.error = 'error', str(exc)
            raise HTTPException(500, str(exc)) from exc
        with self.lock:
            self.state = 'idle'
            return self.status()

    def shutdown(self):
        if self.pipeline and self.pipeline.thread:
            try:
                self.pipeline.finish()
            except Exception:
                pass

    def path(self, name):
        if Path(name).name != name or '/' in name or '\\' in name or not name.endswith(('.mcap', '.mcap.partial')):
            raise HTTPException(422, 'Invalid recording name')
        path = self.config.output_dir / name
        if path.is_symlink():
            raise HTTPException(422, 'Symlinks are not allowed')
        if not path.is_file():
            raise HTTPException(404, 'Recording not found')
        if self.status()['state'] in ('starting', 'recording', 'stopping') and name in (self.filename, self.filename + '.partial'):
            raise HTTPException(409, 'Recording is active')
        return path


def create_app(config):
    manager = RecordingManager(config)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(manager.shutdown)

    app = FastAPI(lifespan=lifespan)
    app.state.manager = manager

    @app.get('/', response_class=HTMLResponse)
    def page():
        return Path(__file__).with_name('web.html').read_text()

    @app.get('/api/status')
    def status():
        return manager.status()

    @app.post('/api/recordings/start', status_code=201)
    def start(request: StartRequest):
        return manager.start(request.filename)

    @app.post('/api/recordings/stop')
    def stop():
        return manager.stop()

    @app.get('/api/recordings')
    def listing():
        with manager.lock:
            state = manager.status()
            rows = []
            for path in sorted(config.output_dir.iterdir()):
                if path.is_symlink() or not path.is_file() or not path.name.endswith(('.mcap', '.mcap.partial')):
                    continue
                info = path.stat()
                active = state['state'] in ('starting', 'recording', 'stopping') and path.name in (manager.filename, str(manager.filename) + '.partial')
                rows.append(dict(name=path.name, size=info.st_size, modified=info.st_mtime, active=active, partial=path.name.endswith('.partial')))
            return rows

    @app.get('/api/recordings/{name}/download')
    def download(name: str):
        with manager.lock:
            path = manager.path(name)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise HTTPException(422, 'Not a regular recording')
            stream = os.fdopen(fd, 'rb')
        def chunks():
            with stream:
                while data := stream.read(1024 * 1024):
                    yield data
        from urllib.parse import quote
        return StreamingResponse(chunks(), media_type='application/octet-stream',
            headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name)})

    @app.delete('/api/recordings/{name}')
    def delete(name: str):
        with manager.lock:
            manager.path(name).unlink()
            return {'deleted': name}

    @app.delete('/api/recordings')
    def clear():
        with manager.lock:
            deleted, errors = [], {}
            for row in listing():
                if row['active']:
                    continue
                try:
                    manager.path(row['name']).unlink()
                    deleted.append(row['name'])
                except (OSError, HTTPException) as exc:
                    errors[row['name']] = str(exc)
            return dict(deleted=deleted, errors=errors)

    return app
