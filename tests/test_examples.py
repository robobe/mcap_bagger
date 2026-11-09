import io
import runpy
from pathlib import Path

import pytest
import yaml
from PIL import Image
from mcap.reader import make_reader

from mcap_bagger.shm import FramePublisher, FrameReader
from mcap_bagger.registration import RegistrationConfig, register_channels

DEMO = runpy.run_path('examples/run_demo.py')
SUMMARY = runpy.run_path('examples/summarize_mcap.py')


def test_frame_and_shm_cleanup():
    first = DEMO['make_frame'](1)
    assert len(first) == 640 * 480 * 3
    assert first != DEMO['make_frame'](2)
    publisher = FramePublisher()
    paths = [p for p, _ in publisher.slots]
    reader = FrameReader('image')
    try:
        notification = publisher.write('image', first)
        _, pixels = reader.read(notification)
        assert pixels == first
        publisher.write('image', DEMO['make_frame'](2))
        assert pixels == first
    finally:
        reader.close()
        publisher.close()
    assert not any(p.exists() for p in paths)


def test_short_demo_and_summary(tmp_path):
    import socket
    with socket.socket() as a, socket.socket() as b:
        a.bind(('127.0.0.1', 0))
        b.bind(('127.0.0.1', 0))
        ports = [a.getsockname()[1], b.getsockname()[1]]
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['output_dir'] = str(tmp_path / 'recordings')
    for channel in data['channels']:
        channel['endpoint'] = f"tcp://127.0.0.1:{ports[0 if channel['shm'] else 1]}"
    path = tmp_path / 'demo.yaml'
    path.write_text(yaml.safe_dump(data))
    output = DEMO['run_demo'](path, duration=.3)
    rows = SUMMARY['summarize'](output)
    assert set(rows) == {'camera/front/image', 'imu', 'camera_info'}
    assert rows['camera/front/image']['count'] > 1
    assert rows['imu']['count'] > 1
    assert rows['camera_info']['rate'] is None
    with output.open('rb') as stream:
        for _, channel, message in make_reader(stream).iter_messages():
            if channel.topic == 'camera/front/image':
                image = Image.open(io.BytesIO(message.data))
                assert image.size == (640, 480) and image.format == 'JPEG'


def test_empty_summary(tmp_path):
    config = RegistrationConfig.load('config/recorder.yaml')
    from dataclasses import replace
    path, _ = register_channels(replace(config, output_dir=tmp_path))
    assert all(r['count'] == 0 and r['rate'] is None for r in SUMMARY['summarize'](path).values())


def test_busy_slot_and_wrong_pixels():
    import fcntl
    import os
    publisher = FramePublisher(2, 2)
    fd = os.open(publisher.slots[0][0], os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert publisher.write('image', bytes(12)) is None
        with pytest.raises(ValueError):
            publisher.write('image', b'')
    finally:
        os.close(fd)
        publisher.close()


def test_summary_known_rates(tmp_path):
    from mcap.writer import Writer
    path = tmp_path / 'rates.mcap'
    with path.open('wb') as stream:
        writer = Writer(stream)
        writer.start()
        a = writer.register_channel('a', '', 0)
        b = writer.register_channel('b', '', 0)
        for stamp in (3_000_000_000, 1_000_000_000, 2_000_000_000):
            writer.add_message(a, stamp, b'x', stamp)
        for _ in range(2):
            writer.add_message(b, 1, b'', 1)
        writer.finish()
    rows = SUMMARY['summarize'](path)
    assert rows['a']['count'] == 3 and rows['a']['rate'] == 1
    assert rows['b']['rate'] is None


def test_demo_start_failure_cleans_shm(tmp_path, monkeypatch):
    globals_ = DEMO['run_demo'].__globals__
    publishers = []

    def publisher():
        item = FramePublisher()
        publishers.append(item)
        return item

    def fail(*args, **kwargs):
        raise OSError('cannot start recorder')

    monkeypatch.setitem(globals_, 'FramePublisher', publisher)
    monkeypatch.setattr(globals_['subprocess'], 'Popen', fail)
    with pytest.raises(OSError):
        DEMO['run_demo'](Path('config/recorder.yaml'), duration=.01)
    assert publishers and publishers[0].slots == []


def test_summary_table_layout():
    rows = {
        'camera/front/image': dict(count=300, span_s=9.967, rate=30., unit='FPS'),
        'imu': dict(count=1000, span_s=9.99, rate=100., unit='Hz'),
        'empty': dict(count=0, span_s=0, rate=None, unit='Hz'),
    }
    text = SUMMARY['format_summary']('demo.mcap', rows)
    assert text.startswith('MCAP summary: demo.mcap\n')
    assert '1,000' in text and '30.00 FPS' in text and 'N/A' in text
    assert 'Total: 1,300 messages across 3 topics' in text
    table = [line for line in text.splitlines() if ' | ' in line]
    assert len({len(line) for line in table}) == 1
    assert 'Messages' in SUMMARY['format_summary']('empty.mcap', {})


def test_publish_only_never_launches_recorder(tmp_path, monkeypatch):
    import socket
    with socket.socket() as a, socket.socket() as b:
        a.bind(('127.0.0.1', 0))
        b.bind(('127.0.0.1', 0))
        ports = [a.getsockname()[1], b.getsockname()[1]]
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['output_dir'] = str(tmp_path / 'recordings')
    for channel in data['channels']:
        channel['endpoint'] = f"tcp://127.0.0.1:{ports[0 if channel['shm'] else 1]}"
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))

    def fail(*args, **kwargs):
        raise AssertionError('publisher-only mode must not launch a recorder')

    monkeypatch.setattr(DEMO['run_demo'].__globals__['subprocess'], 'Popen', fail)
    assert DEMO['run_demo'](path, duration=.05, publish_only=True) is None
    assert not (tmp_path / 'recordings').exists()
