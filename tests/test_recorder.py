
import pytest
from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
from mcap.reader import make_reader

from mcap_bagger import recorder as module
from mcap_bagger.recorder import Recorder, Recording


def sample(data):
    return data


def test_lifecycle_and_round_trip(config_files):
    recorder = Recorder()
    assert recorder.close() == "No recording is open"
    with pytest.raises(ValueError, match="init"):
        recorder.start()
    recorder.init(config_files[0])
    with pytest.raises(Exception):
        recorder.init("missing.yaml")
    assert recorder.config is not None
    recorder.start()
    run = recorder.recording
    with pytest.raises(ValueError, match="already"):
        recorder.start()
    with pytest.raises(ValueError, match="close"):
        recorder.init(config_files[0])
    pixels = bytes(range(12))
    run.receive(run.config.topics[0], sample(pixels))
    run.receive(run.config.topics[1], sample(b'{"number":42}'))
    recorder.close()
    assert run.done.is_set() and run.error is None
    assert run.path.suffix == ".mcap"
    assert not run.path.with_suffix(".mcap.partial").exists()
    with run.path.open("rb") as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        assert summary.statistics.message_count == 2
        assert summary.chunk_indexes and summary.schemas
        messages = list(reader.iter_messages())
        assert [message.sequence for _, _, message in messages] == [0, 0]
        for schema, channel, message in messages:
            assert message.log_time == message.publish_time
            if channel.topic.endswith("image"):
                assert schema.name == "foxglove.RawImage"
                image = RawImage.FromString(message.data)
                assert image.data == pixels
                assert image.timestamp.ToNanoseconds() == message.log_time
            else:
                assert message.data == b'{"number":42}'
                assert schema.encoding == "jsonschema"
    assert recorder.close() == run.summary()
    recorder.start()
    recorder.close()
    assert recorder.recording.path != run.path
    assert recorder.recording.error is None


@pytest.mark.parametrize(
    "payload",
    [
        b"bad",
        b"",
    ],
)
def test_bad_message_stops_and_allows_restart(config, payload):
    errors = []
    recorder = Recorder(on_error=errors.append)
    recorder.config = config
    recorder.start()
    run = recorder.recording
    run.receive(config.topics[0], sample(payload))
    assert run.done.wait(3)
    assert run.error and errors
    assert run.path.name.endswith(".partial")
    assert not run.final_path.exists()
    recorder.start()
    recorder.close()
    assert recorder.recording.error is None


def test_queue_overflow(config, monkeypatch):
    monkeypatch.setattr(module, "QUEUE_BYTES", 8)
    run = Recording(config, on_error=lambda message: None)
    run.start()
    run.receive(config.topics[0], sample(bytes(12)))
    assert run.done.wait(3)
    assert "overflow" in run.error
    assert run.counts[config.topics[0].key]["accepted"] == 0
    assert run.queued_bytes == 0
    assert not run.final_path.exists()


def test_write_error(config, monkeypatch):
    run = Recording(config, on_error=lambda message: None)
    run.start()

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(run.writer, "add_message", fail)
    run.receive(config.topics[0], sample(bytes(12)))
    assert run.done.wait(3)
    assert "disk full" in run.error
    assert run.counts[config.topics[0].key] == {
        "accepted": 1,
        "written": 0,
    }
    assert run.stream.closed and not run.final_path.exists()


def test_finalization_failure(config, monkeypatch):
    run = Recording(config, on_error=lambda message: None)
    run.start()

    def fail(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(module.os, "fsync", fail)
    run.close()
    assert "fsync" in run.error
    assert run.path.exists() and run.path.suffix == ".partial"
    assert run.stream.closed and not run.final_path.exists()


def test_start_failure_and_existing_recording(config, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("subscription failed")

    monkeypatch.setattr(module.zmq.Context, "socket", fail)
    run = Recording(config, on_error=lambda message: None)
    with pytest.raises(RuntimeError, match="subscription"):
        run.start()
    assert run.done.is_set() and run.stream.closed
    assert run.error and not run.final_path.exists()
    run = Recording(config, on_error=lambda message: None)
    config.output_dir.mkdir(exist_ok=True)
    run.final_path.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        run.start()
    assert run.final_path.read_bytes() == b"existing"


def test_completion_never_replaces_collision(config):
    run = Recording(config, on_error=lambda message: None)
    run.start()
    run.final_path.write_bytes(b"existing")
    run.close()
    assert run.error and run.path.suffix == ".partial"
    assert run.final_path.read_bytes() == b"existing"


def test_overflow_drains_previously_accepted_frames(config, monkeypatch):
    import threading

    from mcap_bagger.config import Topic

    monkeypatch.setattr(module, "QUEUE_BYTES", 12)
    entered = threading.Event()
    release = threading.Event()
    encode = Topic.encode

    def slow_encode(self, data, received_ns):
        entered.set()
        assert release.wait(3)
        return encode(self, data, received_ns)

    monkeypatch.setattr(Topic, "encode", slow_encode)
    run = Recording(config, on_error=lambda message: None)
    run.start()
    try:
        run.receive(config.topics[0], sample(bytes(12)))
        assert entered.wait(3)
        run.receive(config.topics[0], sample(bytes(12)))
        run.receive(config.topics[0], sample(bytes(12)))
        assert "overflow" in run.error
    finally:
        release.set()
        run.close()
    assert run.counts[config.topics[0].key]["accepted"] == 2
    assert run.counts[config.topics[0].key]["written"] == 2
    assert run.peak_queued_bytes == 12
    with run.path.open("rb") as stream:
        assert make_reader(stream).get_summary().statistics.message_count == 2
