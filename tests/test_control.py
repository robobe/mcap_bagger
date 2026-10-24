import json
import time
from contextlib import contextmanager

import pytest
import zmq
from bullet_msgs.control import COMMAND_TOPIC, HEARTBEAT_TOPIC, STATE_TOPIC
from mcap.reader import make_reader

from mcap_bagger.control import ControlledRecorder
from mcap_bagger.recorder import Recorder


@contextmanager
def listener(config):
    context = zmq.Context()
    telemetry = context.socket(zmq.PUB)
    commands = context.socket(zmq.PUB)
    telemetry.bind(config.transport["telemetry_endpoint"])
    commands.bind(config.transport["control_endpoint"])
    recorder = Recorder(on_error=lambda message: None)
    recorder.config = config
    now = [time.monotonic()]
    service = ControlledRecorder(recorder, report=lambda message: None, clock=lambda: max(now[0], time.monotonic()))
    try:
        yield service, telemetry, commands, now
    finally:
        service.close()
        telemetry.close(0)
        commands.close(0)
        context.term()


def state(episode="first", status="running", **changes):
    return dict(schema_version=1, physics_id="physics", controller_id="godot",
                episode_id=episode, state=status, reason="completed" if status == "stopped" else status,
                simulation_time_s=0, **changes)


def send(socket, topic, value):
    socket.send_multipart([topic, json.dumps(value).encode()])


def until(service, predicate, publish=lambda: None):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, "control condition did not arrive"
        publish()
        service.poll()


def establish(service, telemetry):
    until(service, lambda: service.episode is not None,
          lambda: send(telemetry, STATE_TOPIC, state("initial", "stopped")))


def command(service, commands, now, action, sequence, **changes):
    value = dict(schema_version=1, controller_id="godot", sequence=sequence,
                 issued_ms=int(now[0] * 1000), action=action)
    value.update(changes)
    topic = HEARTBEAT_TOPIC if action == "heartbeat" else COMMAND_TOPIC
    send(commands, topic, value)
    return value


def heartbeat(service, commands, now):
    until(service, lambda: service.last_heartbeat is not None,
          lambda: command(service, commands, now, "heartbeat", 1))


def test_controlled_run_files_and_readback(config):
    with listener(config) as (service, telemetry, commands, now):
        establish(service, telemetry)
        heartbeat(service, commands, now)
        assert service.recorder.recording is None
        command(service, commands, now, "start", 2)
        until(service, lambda: service.sequence == 2)
        assert service.recorder.recording is None  # Start alone never opens a file.
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state()))
        run = service.recorder.recording
        run.receive(config.topics[1], b'{"number":42}')
        command(service, commands, now, "start", 3)
        until(service, lambda: service.sequence == 3)
        assert service.recorder.recording is run
        command(service, commands, now, "stop", 4)
        until(service, lambda: not service.recorder.active)
        assert run.path.suffix == ".mcap" and run.error is None
        # Queued running messages after Stop cannot reopen the closed episode.
        send(telemetry, STATE_TOPIC, state())
        service.poll()
        assert service.recorder.recording is run
        with run.path.open("rb") as stream:
            messages = list(make_reader(stream).iter_messages())
            assert [message.data for _, _, message in messages] == [b'{"number":42}']
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state("second")))
        second = service.recorder.recording
        assert second.path != run.path
        command(service, commands, now, "reset", 5)
        until(service, lambda: not service.recorder.active)
        send(telemetry, STATE_TOPIC, state("reset", "stopped"))
        until(service, lambda: service.episode[1] == "reset")
        assert len(list(config.output_dir.glob("*.mcap"))) == 2
        assert not list(config.output_dir.glob("*.partial"))


@pytest.mark.parametrize("ending", ["stopped", "error", "timeout"])
def test_late_join_and_automatic_end(config, ending):
    with listener(config) as (service, telemetry, commands, now):
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state()))
        run = service.recorder.recording
        if ending == "timeout":
            now[0] += 3.01
        else:
            send(telemetry, STATE_TOPIC, state(status=ending))
        until(service, lambda: not service.recorder.active)
        assert run.error is None
        with run.path.open("rb") as stream:
            assert make_reader(stream).get_summary() is not None
        send(telemetry, STATE_TOPIC, state())
        service.poll()
        assert service.recorder.recording is run
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state("next")))


def test_recording_failure_retries_only_next_episode(config, monkeypatch):
    with listener(config) as (service, telemetry, commands, now):
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state()))
        failed = service.recorder.recording
        def disk_full(**kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(failed.writer, "add_message", disk_full)
        failed.receive(config.topics[1], b'{"number":42}')
        assert failed.done.wait(3)
        send(telemetry, STATE_TOPIC, state())
        service.poll()
        assert service.recorder.recording is failed
        assert failed.path.suffix == ".partial"
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state("next")))
        assert service.recorder.recording is not failed


def test_rejected_messages_do_not_stop_run_or_refresh_state(config):
    with listener(config) as (service, telemetry, commands, now):
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state()))
        heartbeat(service, commands, now)
        for value in [b"bad", b"{}", b"NaN", b"\xff"]:
            telemetry.send_multipart([STATE_TOPIC, value])
            commands.send_multipart([COMMAND_TOPIC, value])
        telemetry.send_multipart([STATE_TOPIC, b"{}", b"extra"])
        send(telemetry, STATE_TOPIC + b"/suffix", state(status="stopped"))
        send(commands, COMMAND_TOPIC + b"/suffix", dict(
            schema_version=1, controller_id="godot", sequence=99,
            issued_ms=int(now[0] * 1000), action="stop"))
        command(service, commands, now, "stop", 1)  # duplicate sequence
        command(service, commands, now, "reset", 2, issued_ms=int((now[0] - 2) * 1000))
        command(service, commands, now, "stop", 3, controller_id="other")
        # A valid heartbeat is a delivery fence on this command socket.
        command(service, commands, now, "heartbeat", 4)
        until(service, lambda: service.sequence == 4)
        assert service.recorder.active
        now[0] += 3.01
        until(service, lambda: not service.recorder.active)


def test_physics_restart_and_old_episode_cannot_close_new_run(config):
    with listener(config) as (service, telemetry, commands, now):
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state()))
        first = service.recorder.recording
        restarted = state("new")
        restarted["physics_id"] = "replacement"
        until(service, lambda: service.recorder.recording is not first,
              lambda: send(telemetry, STATE_TOPIC, restarted))
        second = service.recorder.recording
        send(telemetry, STATE_TOPIC, state(status="stopped"))
        service.poll()
        assert service.recorder.recording is second and service.recorder.active
        assert first.done.is_set()


def test_open_failure_is_not_retried_for_same_run(config, monkeypatch):
    with listener(config) as (service, telemetry, commands, now):
        original = service.recorder.start
        attempts = []
        def fail():
            attempts.append(1)
            raise OSError("cannot open")
        monkeypatch.setattr(service.recorder, "start", fail)
        until(service, lambda: attempts,
              lambda: send(telemetry, STATE_TOPIC, state()))
        send(telemetry, STATE_TOPIC, state())
        service.poll()
        assert attempts == [1]
        monkeypatch.setattr(service.recorder, "start", original)
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state("next")))


def test_signal_during_start_still_finalizes_file(config, monkeypatch):
    with listener(config) as (service, telemetry, commands, now):
        original = service.recorder.start
        def interrupt():
            original()
            raise KeyboardInterrupt
        monkeypatch.setattr(service.recorder, "start", interrupt)
        with pytest.raises(KeyboardInterrupt):
            until(service, lambda: service.recorder.active,
                  lambda: send(telemetry, STATE_TOPIC, state()))
        run = service.recorder.recording
        service.close()
        assert run.done.is_set() and run.error is None
        assert run.path.suffix == ".mcap"


def test_controller_replacement_and_retired_commands(config):
    with listener(config) as (service, telemetry, commands, now):
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state()))
        heartbeat(service, commands, now)
        command(service, commands, now, "heartbeat", 1, controller_id="replacement")
        until(service, lambda: service.controller == "replacement")
        assert not service.recorder.active
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state("next")))
        command(service, commands, now, "heartbeat", 100)  # retired controller
        command(service, commands, now, "stop", 101)
        command(service, commands, now, "heartbeat", 2, controller_id="replacement")
        until(service, lambda: service.sequence == 2)
        assert service.controller == "replacement" and service.recorder.active


@pytest.mark.parametrize("known_stopped_state", [False, True])
def test_stop_before_first_running_state_does_not_open_file(config, known_stopped_state):
    with listener(config) as (service, telemetry, commands, now):
        if known_stopped_state:
            establish(service, telemetry)
        heartbeat(service, commands, now)
        command(service, commands, now, "stop", 2)
        until(service, lambda: service.sequence == 2)
        until(service, lambda: service.episode == ("physics", "first"),
              lambda: send(telemetry, STATE_TOPIC, state()))
        assert service.recorder.recording is None
        send(telemetry, STATE_TOPIC, state(status="stopped"))
        until(service, lambda: not service.awaiting_stop)
        # Even if the following Start is missed, a new episode recovers recording.
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state("next")))
