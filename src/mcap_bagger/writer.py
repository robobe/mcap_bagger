"""Bounded producer queues and the single owner of an MCAP output stream."""
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

from mcap.writer import Writer, CompressionType


class SharedQueues:
    def __init__(self, max_bytes=64 * 1024 * 1024, max_entries=4096):
        self.condition = threading.Condition()
        self.queues = [deque(), deque()]
        self.sizes = [0, 0]
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.closed = False
        self.turn = 0

    def put(self, index, message):
        with self.condition:
            if self.closed:
                raise ValueError('queue input is closed')
            if self.sizes[index] + len(message[2]) > self.max_bytes or len(self.queues[index]) >= self.max_entries:
                raise ValueError('receive queue overflow')
            self.queues[index].append(message)
            self.sizes[index] += len(message[2])
            self.condition.notify_all()

    def get_next(self, timeout=None):
        with self.condition:
            if not self.condition.wait_for(lambda: any(self.queues) or self.closed, timeout):
                raise TimeoutError
            if not any(self.queues):
                return None
            index = self.turn if self.queues[self.turn] else 1 - self.turn
            message = self.queues[index].popleft()
            self.sizes[index] -= len(message[2])
            self.turn = 1 - index
            return message

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class McapWriter:
    def __init__(self, config, queues, filename=None):
        self.filename = filename
        self.config = config
        self.queues = queues
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error = None
        self.success = True
        self.path = None
        self.thread = threading.Thread(target=self._write, name='mcap-writer')

    def start(self):
        self.thread.start()
        self.ready.wait()
        if self.error is not None:
            self.thread.join()
            raise self.error

    def finish(self, success=True):
        self.success = success
        self.queues.close()
        self.thread.join()
        if self.error is not None:
            raise self.error
        return self.path

    def _write(self):
        try:
            config = self.config
            config.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            final = config.output_dir / (self.filename or f'{config.project}_{stamp}.mcap')
            if final.exists():
                raise FileExistsError(final)
            self.path = final.with_suffix('.mcap.partial')
            with self.path.open('xb') as stream:
                writer = Writer(stream, compression=CompressionType.NONE)
                writer.start()
                channels, counts = {}, {}
                for channel in config.channels:
                    metadata = dict(endpoint=channel.endpoint, shm=str(channel.shm).lower())
                    if channel.compress:
                        metadata['compress'] = channel.compress
                    channels[channel.topic] = writer.register_channel(channel.topic, '', 0, metadata)
                    counts[channel.topic] = 0
                self.ready.set()
                interval = config.sync_interval_s
                deadline = time.monotonic() + interval
                dirty = True
                while True:
                    if interval and time.monotonic() >= deadline:
                        if dirty:
                            writer.flush()
                            os.fsync(stream.fileno())
                            dirty = False
                        deadline = time.monotonic() + interval
                    try:
                        message = self.queues.get_next(max(0, deadline - time.monotonic()) if interval else None)
                    except TimeoutError:
                        continue
                    if message is None:
                        break
                    topic, received, payload = message
                    writer.add_message(channels[topic], received, payload, received, counts[topic] % 2**32)
                    counts[topic] += 1
                    dirty = True
                writer.finish()
                stream.flush()
                os.fsync(stream.fileno())
            if self.success:
                os.link(self.path, final)
                self.path.unlink()
                self.path = final
        except Exception as exc:
            self.error = exc
            self.queues.close()
        finally:
            self.ready.set()
            self.done.set()
