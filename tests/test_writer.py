import threading

import pytest
from mcap.reader import make_reader

from mcap_bagger.registration import Channel, RegistrationConfig
from mcap_bagger.writer import SharedQueues, McapWriter


def config(tmp_path):
    return RegistrationConfig('test', tmp_path, (
        Channel('imu', 'tcp://127.0.0.1:5555', False),
        Channel('image', 'tcp://127.0.0.1:5556', True, 'jpeg')))


def test_queue_alternation_and_close():
    queues = SharedQueues()
    for index, topic in enumerate(('imu', 'image')):
        for number in range(2):
            queues.put(index, (topic, number, b'x'))
    queues.close()
    assert [queues.get_next()[:2] for _ in range(4)] == [('imu', 0), ('image', 0), ('imu', 1), ('image', 1)]
    assert queues.get_next() is None
    assert queues.sizes == [0, 0]
    with pytest.raises(ValueError, match='closed'):
        queues.put(0, ('imu', 0, b''))


def test_idle_queue_wakes_on_close():
    queues = SharedQueues()
    result = []
    thread = threading.Thread(target=lambda: result.append(queues.get_next()))
    thread.start()
    queues.close()
    thread.join(2)
    assert not thread.is_alive() and result == [None]


@pytest.mark.parametrize('success', [True, False])
def test_writer_drains_and_preserves_records(tmp_path, success):
    queues = SharedQueues()
    queues.put(0, ('imu', 1, b'\x00binary'))
    queues.put(0, ('imu', 2, b''))
    queues.put(1, ('image', 3, b'jpeg'))
    writer = McapWriter(config(tmp_path), queues)
    writer.start()
    path = writer.finish(success)
    assert str(path).endswith('.mcap' if success else '.mcap.partial')
    with path.open('rb') as stream:
        reader = make_reader(stream)
        records = list(reader.iter_messages(log_time_order=False))
        assert [c.topic for _, c, _ in records] == ['imu', 'image', 'imu']
        assert [m.data for _, _, m in records] == [b'\x00binary', b'jpeg', b'']
        assert [m.sequence for _, _, m in records] == [0, 0, 1]
        assert all(m.log_time == m.publish_time for _, _, m in records)
    assert writer.done.is_set() and not writer.thread.is_alive()


@pytest.mark.parametrize('stage', ['start', 'register_channel', 'add_message', 'finish', 'fsync', 'link'])
def test_writer_failure_retains_partial(tmp_path, monkeypatch, stage):
    from mcap_bagger import writer as module
    calls = []

    def fail(*args, **kwargs):
        calls.append(stage)
        raise OSError(stage)

    owner = module.os if stage in ('fsync', 'link') else module.Writer
    monkeypatch.setattr(owner, stage, fail)
    queues = SharedQueues()
    queues.put(0, ('imu', 1, b'data'))
    writer = McapWriter(config(tmp_path), queues)
    with pytest.raises(OSError, match=stage):
        writer.start()
        writer.finish()
    assert writer.done.is_set() and not writer.thread.is_alive()
    assert calls == [stage]
    assert not list(tmp_path.glob('*.mcap'))
    assert list(tmp_path.glob('*.partial'))


def test_queue_limits_and_wakeup():
    queues = SharedQueues(max_bytes=2, max_entries=1)
    result = []
    thread = threading.Thread(target=lambda: result.append(queues.get_next()))
    thread.start()
    queues.put(0, ('imu', 1, b'xx'))
    thread.join(2)
    assert not thread.is_alive()
    assert result == [('imu', 1, b'xx')]
    queues.put(0, ('imu', 2, b''))
    with pytest.raises(ValueError, match='overflow'):
        queues.put(0, ('imu', 3, b''))
    with pytest.raises(ValueError, match='overflow'):
        queues.put(1, ('image', 1, b'xxx'))
    queues.close()


@pytest.mark.parametrize('stage', ['flush', 'fsync'])
def test_idle_periodic_sync_and_failure(tmp_path, monkeypatch, stage):
    from dataclasses import replace
    from mcap_bagger import writer as module
    called = threading.Event()

    def fail(*args):
        called.set()
        raise OSError('sync failed')

    monkeypatch.setattr(module.os if stage == 'fsync' else module.Writer, stage, fail)
    writer = McapWriter(replace(config(tmp_path), sync_interval_s=.01), SharedQueues())
    writer.start()
    assert called.wait(2)
    assert writer.done.wait(2)
    with pytest.raises(OSError, match='sync failed'):
        writer.finish()
    assert not list(tmp_path.glob('*.mcap'))


def test_queue_timeout():
    queues = SharedQueues()
    with pytest.raises(TimeoutError):
        queues.get_next(timeout=.001)
    queues.close()
    assert queues.get_next(timeout=.001) is None
