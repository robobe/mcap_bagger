import io
import threading
import time

import pytest
import zmq
from PIL import Image
from mcap.reader import make_reader

from mcap_bagger.pipeline import Pipeline
from mcap_bagger.registration import Channel, RegistrationConfig


@pytest.mark.parametrize('compress', ['raw', 'jpeg'])
def test_both_queues_are_written(tmp_path, compress):
    config = RegistrationConfig('test', tmp_path, (
        Channel('imu', 'tcp://127.0.0.1:5555', False),
        Channel('image', 'tcp://127.0.0.1:5556', True, compress)))
    pipeline = Pipeline(config)
    pipeline.enqueue(0, ('imu', 123, b'\x00binary'))
    pipeline.enqueue(0, ('imu', 124, b''))
    payload = bytes([255, 0, 0]) * 4
    if compress == 'jpeg':
        buffer = io.BytesIO()
        Image.frombytes('RGB', (2, 2), payload).save(buffer, 'JPEG', quality=90)
        payload = buffer.getvalue()
    pipeline.enqueue(1, ('image', 125, payload))
    pipeline.stop.set()
    pipeline.write()
    assert pipeline.error is None
    with next(tmp_path.glob('*.mcap')).open('rb') as stream:
        reader = make_reader(stream)
        messages = list(reader.iter_messages())
        assert len(messages) == 3
        assert [m.data for _, c, m in messages if c.topic == 'imu'] == [b'\x00binary', b'']
        assert next(m.data for _, c, m in messages if c.topic == 'image') == payload
        assert len(reader.get_summary().channels) == 2


def test_tcp_message_receiver(tmp_path):
    with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
        port = publisher.bind_to_random_port('tcp://127.0.0.1')
        pipeline = Pipeline(RegistrationConfig('test', tmp_path, (
            Channel('imu', f'tcp://127.0.0.1:{port}', False),)))
        thread = threading.Thread(target=pipeline.receive, args=(0,))
        thread.start()
        try:
            assert publisher.poll(3000)
            publisher.recv()
            publisher.send_multipart([b'imu', b'opaque'])
            with pipeline.condition:
                assert pipeline.condition.wait_for(lambda: pipeline.queues[0], timeout=3)
                assert pipeline.queues[0][0][2] == b'opaque'
        finally:
            pipeline.stop.set()
            thread.join(3)
        assert not thread.is_alive()
        assert pipeline.error is None


def test_queue_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr('mcap_bagger.pipeline.QUEUE_BYTES', 2)
    pipeline = Pipeline(RegistrationConfig('test', tmp_path, ()))
    with pytest.raises(ValueError, match='overflow'):
        pipeline.enqueue(0, ('imu', 1, b'123'))


@pytest.mark.parametrize('compress', ['raw', 'jpeg'])
def test_shm_image_handler(tmp_path, compress):
    import json
    import os
    import struct
    from pathlib import Path
    from uuid import uuid4
    from mcap_bagger.shm import HEADER, MAGIC

    session = uuid4().hex
    name = f'/bullet_msgs_{session}_s0'
    path = Path('/dev/shm' + name)
    pixels = bytes([255, 0, 0]) * 4
    metadata = dict(version=1, session=session, slot=0, shm_name=name,
                    width=2, height=2, encoding='rgb8', step=6, bytes=12,
                    sequence=1, capture_ns=1)
    descriptor = json.dumps(metadata).encode()
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, HEADER.pack(MAGIC, 1, 0, 2, 2, 1, 6, 12, 1, 1, 1,
                                session.encode().ljust(64, b'\0')) + pixels)
        os.close(fd)
        with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            port = publisher.bind_to_random_port('tcp://127.0.0.1')
            pipeline = Pipeline(RegistrationConfig('test', tmp_path, (
                Channel('image', f'tcp://127.0.0.1:{port}', True, compress),)))
            thread = threading.Thread(target=pipeline.receive, args=(1,))
            thread.start()
            try:
                assert publisher.poll(3000)
                publisher.recv()
                publisher.send(b'image\0' + struct.pack('<I', len(descriptor)) + descriptor)
                with pipeline.condition:
                    assert pipeline.condition.wait_for(lambda: pipeline.queues[1] or pipeline.error, timeout=3)
                    assert pipeline.error is None
                    payload = pipeline.queues[1][0][2]
                if compress == 'raw':
                    assert payload == pixels
                else:
                    image = Image.open(io.BytesIO(payload))
                    assert image.format == 'JPEG' and image.size == (2, 2)
                    red, green, blue = image.getpixel((0, 0))
                    assert red > 240 and green < 15 and blue < 15
            finally:
                pipeline.stop.set()
                thread.join(3)
    finally:
        path.unlink(missing_ok=True)
