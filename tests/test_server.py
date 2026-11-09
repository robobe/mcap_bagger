from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from mcap.reader import make_reader

from mcap_bagger.registration import RegistrationConfig
from mcap_bagger.server import create_app


@pytest.fixture
def client(tmp_path):
    config = replace(RegistrationConfig.load('config/recorder.yaml'), output_dir=tmp_path)
    with TestClient(create_app(config)) as client:
        yield client


def test_start_stop_download_and_conflicts(client, tmp_path):
    assert client.get('/').status_code == 200
    assert client.post('/api/recordings/start', json={'filename': 'test'}).status_code == 201
    assert client.post('/api/recordings/start', json={'filename': 'other'}).status_code == 409
    rows = client.get('/api/recordings').json()
    assert rows[0]['active']
    assert client.get('/api/recordings/test.mcap.partial/download').status_code == 409
    assert client.delete('/api/recordings/test.mcap.partial').status_code == 409
    assert client.delete('/api/recordings').json()['deleted'] == []
    assert client.post('/api/recordings/stop').status_code == 200
    assert client.post('/api/recordings/stop').status_code == 409
    result = client.get('/api/recordings/test.mcap/download')
    assert result.status_code == 200 and result.content == (tmp_path / 'test.mcap').read_bytes()
    with (tmp_path / 'test.mcap').open('rb') as stream:
        assert make_reader(stream).get_summary() is not None
    assert client.post('/api/recordings/start', json={'filename': 'test.mcap'}).status_code == 409
    assert client.post('/api/recordings/start', json={'filename': 'next'}).status_code == 201
    assert client.post('/api/recordings/stop').status_code == 200
    assert client.delete('/api/recordings/test.mcap').status_code == 200
    assert client.delete('/api/recordings').json()['deleted'] == ['next.mcap']


@pytest.mark.parametrize('name', ['../bad', '/tmp/bad', 'bad/name', 'bad\\name', '', '.hidden', 'a'*65])
def test_invalid_names(client, name):
    assert client.post('/api/recordings/start', json={'filename': name}).status_code == 422


def test_symlinks_and_unrelated_files(client, tmp_path):
    (tmp_path / 'notes.txt').write_text('keep')
    (tmp_path / 'link.mcap').symlink_to(tmp_path / 'notes.txt')
    (tmp_path / 'failed.mcap.partial').write_bytes(b'partial')
    assert client.get('/api/recordings/link.mcap/download').status_code == 422
    assert client.delete('/api/recordings/link.mcap').status_code == 422
    assert client.get('/api/recordings/failed.mcap.partial/download').content == b'partial'
    assert client.delete('/api/recordings').json()['deleted'] == ['failed.mcap.partial']
    assert (tmp_path / 'notes.txt').read_text() == 'keep'
    assert (tmp_path / 'link.mcap').is_symlink()
    assert client.get('/api/recordings/missing.mcap/download').status_code == 404


def test_concurrent_starts(client):
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda name: client.post('/api/recordings/start', json={'filename': name}), ['a', 'b']))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert client.post('/api/recordings/stop').status_code == 200


def test_shutdown_drains(tmp_path):
    config = replace(RegistrationConfig.load('config/recorder.yaml'), output_dir=tmp_path)
    app = create_app(config)
    with TestClient(app) as client:
        assert client.post('/api/recordings/start', json={'filename': 'shutdown'}).status_code == 201
        app.state.manager.pipeline.enqueue(0, ('imu', 10, b'opaque'))
    with (tmp_path / 'shutdown.mcap').open('rb') as stream:
        assert next(make_reader(stream).iter_messages())[2].data == b'opaque'
    assert app.state.manager.pipeline.done.is_set()


def test_start_failure(client, monkeypatch):
    def fail(self):
        raise OSError('cannot create file')
    monkeypatch.setattr('mcap_bagger.server.Pipeline.start', fail)
    assert client.post('/api/recordings/start', json={'filename': 'failed'}).status_code == 500
    assert client.get('/api/status').json()['state'] == 'error'


def test_writer_failure_allows_new_session(client, monkeypatch):
    from mcap_bagger import writer
    original = writer.Writer.add_message

    def fail(*args, **kwargs):
        raise OSError('disk failure')

    monkeypatch.setattr(writer.Writer, 'add_message', fail)
    assert client.post('/api/recordings/start', json={'filename': 'failed_write'}).status_code == 201
    pipeline = client.app.state.manager.pipeline
    pipeline.enqueue(0, ('imu', 1, b'data'))
    assert pipeline.done.wait(3)
    status = client.get('/api/status').json()
    assert status['state'] == 'error' and 'disk failure' in status['error']
    monkeypatch.setattr(writer.Writer, 'add_message', original)
    assert client.post('/api/recordings/start', json={'filename': 'recovered'}).status_code == 201
    assert client.post('/api/recordings/stop').status_code == 200
