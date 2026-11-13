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


@pytest.mark.parametrize('raw', [b'{}', b'[]', b'null', b'bad', b'\xff',
    b'{"publish_time_ns":true,"sequence":0}',
    b'{"publish_time_ns":0,"sequence":-1}',
    b'{"publish_time_ns":18446744073709551616,"sequence":0}',
    b'{"publish_time_ns":0,"sequence":4294967296}',
    b'{"publish_time_ns":0,"sequence":0,"sequence":1}',
    b'{"publish_time_ns":0,"sequence":0,"extra":NaN}'])
def test_invalid_message_headers(raw):
    from mcap_bagger.pipeline import parse_message_header
    with pytest.raises(ValueError):
        parse_message_header(raw)


def test_message_header_boundaries():
    from mcap_bagger.pipeline import parse_message_header
    assert parse_message_header(b'{"publish_time_ns":0,"sequence":0,"extra":1}') == (0, 0)
    assert parse_message_header(b'{"publish_time_ns":18446744073709551615,"sequence":4294967295}') == (2**64-1, 2**32-1)


def test_tcp_headers_and_legacy_readback(tmp_path):
    with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
        port = publisher.bind_to_random_port('tcp://127.0.0.1')
        pipeline = Pipeline(RegistrationConfig('test', tmp_path, (
            Channel('imu', f'tcp://127.0.0.1:{port}', False),)))
        thread = threading.Thread(target=pipeline.receive, args=(0,))
        thread.start()
        try:
            assert publisher.poll(3000)
            publisher.recv()
            for sequence, payload in [(42, b'\x00binary'), (0, b'')]:
                publisher.send_multipart([b'imu', ('{"publish_time_ns":1,"sequence":%d}' % sequence).encode(), payload])
            publisher.send_multipart([b'imu', b'legacy'])
            with pipeline.condition:
                assert pipeline.condition.wait_for(lambda: len(pipeline.queues[0]) == 3, timeout=3)
        finally:
            pipeline.stop.set()
            thread.join(3)
        assert pipeline.error is None
        pipeline.write()
        with pipeline.writer.path.open('rb') as stream:
            records = list(make_reader(stream).iter_messages(log_time_order=False))
        assert [m.data for _, _, m in records] == [b'\x00binary', b'', b'legacy']
        assert [m.sequence for _, _, m in records] == [42, 0, 2]
        assert [m.publish_time for _, _, m in records[:2]] == [1, 1]
        assert all(m.log_time > 1 for _, _, m in records)


@pytest.mark.parametrize('parts', [[b'imu', b'{}', b'payload'], [b'imu', b'a', b'b', b'c']])
def test_invalid_tcp_header_or_framing_retains_partial(tmp_path, parts):
    with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
        port = publisher.bind_to_random_port('tcp://127.0.0.1')
        pipeline = Pipeline(RegistrationConfig('test', tmp_path, (
            Channel('imu', f'tcp://127.0.0.1:{port}', False),)))
        pipeline.start()
        try:
            assert publisher.poll(3000)
            publisher.recv()
            publisher.send_multipart(parts)
            assert pipeline.done.wait(3)
        finally:
            with pytest.raises(ValueError):
                pipeline.finish()
        assert not list(tmp_path.glob('*.mcap'))
        assert list(tmp_path.glob('*.mcap.partial'))
