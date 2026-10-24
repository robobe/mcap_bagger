"""Command-aware recording, with physics state as the authority for opening files."""

import time
import json

import zmq
from bullet_msgs.scenario import END_TOPIC, TERMINAL_DRAIN_S, validate_end
from bullet_msgs.control import (
    COMMAND_MAX_AGE_S,
    COMMAND_TOPIC,
    HEARTBEAT_TIMEOUT_S,
    HEARTBEAT_TOPIC,
    MAX_CONTROL_BYTES,
    STATE_TOPIC,
    decode_json,
    validate_command,
    validate_state,
)


class ControlledRecorder:
    """Own control sockets and the Recorder lifecycle on the calling thread."""

    def __init__(self, recorder, *, report=print, clock=time.monotonic):
        if recorder.config is None:
            raise ValueError("initialize the recorder before listening")
        self.recorder = recorder
        self.report = report
        self.clock = clock
        self.episode = None
        self.active_episode = None
        self.closed_episodes = set()
        self.retired_physics = set()
        self.last_state = None
        self.controller = None
        self.retired_controllers = set()
        self.sequence = 0
        self.last_heartbeat = None
        self.terminal_deadline = None
        self.awaiting_stop = False
        self.cached_snapshots = {}
        self.context = zmq.Context()
        self.sockets = []
        self.poller = zmq.Poller()
        try:
            for endpoint, topics in (
                ("control_endpoint", (COMMAND_TOPIC, HEARTBEAT_TOPIC)),
                ("telemetry_endpoint", (STATE_TOPIC, END_TOPIC)),
            ):
                socket = self.context.socket(zmq.SUB)
                self.sockets.append(socket)
                socket.setsockopt(zmq.LINGER, 0)
                socket.setsockopt(zmq.RCVHWM, 16)
                socket.setsockopt(zmq.MAXMSGSIZE, MAX_CONTROL_BYTES)
                for topic in topics:
                    socket.setsockopt(zmq.SUBSCRIBE, topic)
                socket.connect(recorder.config.transport[endpoint])
                self.poller.register(socket, zmq.POLLIN)
            self.snapshot_sockets = {}
            snapshot_keys = {'analysis/physics/config', 'analysis/app/config', 'flight/drone_1/config', 'simulation/wind_state', 'simulation/spawn_state', 'analysis/render/health', 'app/drone_1/mission'}
            by_endpoint = {}
            for topic in recorder.config.topics:
                if topic.key in snapshot_keys:
                    endpoint = topic.endpoint or recorder.config.transport['telemetry_endpoint']
                    by_endpoint.setdefault(endpoint, []).append(topic)
            for endpoint, topics in by_endpoint.items():
                socket = self.context.socket(zmq.SUB)
                self.sockets.append(socket)
                socket.setsockopt(zmq.LINGER, 0)
                socket.setsockopt(zmq.RCVHWM, 16)
                socket.setsockopt(zmq.MAXMSGSIZE, 256 * 1024)
                for topic in topics:
                    socket.setsockopt(zmq.SUBSCRIBE, topic.key.encode())
                socket.connect(endpoint)
                self.snapshot_sockets[socket] = {topic.key.encode(): topic for topic in topics}
                self.poller.register(socket, zmq.POLLIN)
        except BaseException:
            self.close()
            raise

    def _schedule_stop(self, reason, now):
        if self.active_episode is not None:
            if not self.recorder.recording.outcome.startswith("mission_fault:"):
                self.recorder.recording.outcome = reason
            if self.terminal_deadline is None:
                self.terminal_deadline = now + TERMINAL_DRAIN_S
        else:
            self._stop()

    def _stop(self):
        self.terminal_deadline = None
        if self.episode is not None:
            self.closed_episodes.add(self.episode)
        if self.active_episode is not None or self.recorder.active:
            self.report(self.recorder.close())
            self.active_episode = None

    def _command(self, value, now):
        age = now - value["issued_ms"] / 1000
        if not 0 <= age <= COMMAND_MAX_AGE_S:
            return
        controller = value["controller_id"]
        if controller in self.retired_controllers:
            return
        if controller != self.controller:
            if value["action"] != "heartbeat":
                return
            if self.controller is not None:
                self.retired_controllers.add(self.controller)
                self._stop()
            self.controller = controller
            self.sequence = 0
            self.last_heartbeat = None
        if value["sequence"] <= self.sequence:
            return
        self.sequence = value["sequence"]
        action = value["action"]
        if action == "heartbeat":
            self.last_heartbeat = now
        elif self.last_heartbeat is not None and now - self.last_heartbeat <= HEARTBEAT_TIMEOUT_S:
            if action in ("stop", "reset"):
                # With no open episode, a queued running state may precede Stop.
                self.awaiting_stop = self.active_episode is None
                self._schedule_stop(action, now)
            else:
                self.awaiting_stop = False

    def _terminal(self, result, now):
        validate_end(result)
        source = result['final_source']
        episode = (source['physics_id'], source['episode_id'])
        if self.active_episode != episode:
            return
        self.recorder.recording.outcome = result["reason"] if "reason" in result else "target_hit"
        self.recorder.record_terminal(result)
        if self.terminal_deadline is None:
            self.terminal_deadline = now + TERMINAL_DRAIN_S

    def _state(self, value, now):
        episode = (value["physics_id"], value["episode_id"])
        if episode[0] in self.retired_physics:
            return
        if self.episode is not None and episode[0] != self.episode[0]:
            self.retired_physics.add(self.episode[0])
        # Delayed states cannot resurrect an episode already finalized or failed.
        if episode != self.episode:
            if episode in self.closed_episodes:
                return
            self._stop()
            self.episode = episode
        self.last_state = now
        if value["state"] != "running":
            self.awaiting_stop = False
            if value.get('terminal_result') is not None:
                self._terminal(value['terminal_result'], now)
            if self.terminal_deadline is None:
                self._schedule_stop(value['reason'], now)
        elif self.awaiting_stop:
            self.closed_episodes.add(episode)
        elif episode not in self.closed_episodes:
            self.closed_episodes.add(episode)
            try:
                self.report(self.recorder.start())
                self.active_episode = episode
                self.recorder.recording.episode = dict(physics_id=episode[0], episode_id=episode[1])
                for topic, payload in self.cached_snapshots.values():
                    value = json.loads(payload)
                    physics_id = value.get('physics_id', value.get('source', {}).get('physics_id'))
                    if physics_id == episode[0]:
                        self.recorder.recording.receive(topic, payload)
            except Exception as exc:
                self.report(f"Recording failed: {exc}; waiting for the next episode")

    def poll(self):
        ready = dict(self.poller.poll(50))
        for socket in self.sockets:
            if socket not in ready:
                continue
            for _ in range(16):
                try:
                    parts = socket.recv_multipart(zmq.NOBLOCK)
                except zmq.Again:
                    break
                if len(parts) != 2:
                    continue
                topic, payload = parts
                try:
                    if socket in self.snapshot_sockets:
                        definition = self.snapshot_sockets[socket].get(topic)
                        if definition is not None:
                            try:
                                definition.encode(payload, time.time_ns())
                            except Exception:
                                continue
                            if definition.key == 'app/drone_1/mission':
                                value = json.loads(payload)
                                if self.active_episode == (value['physics_id'], value['episode_id']) and value['phase'] == 'FAULT':
                                    outcome = 'mission_fault:' + value['reason']
                                    if self.recorder.recording.outcome != outcome:
                                        self.recorder.recording.receive(definition, payload)
                                    self.recorder.recording.outcome = outcome
                            else:
                                self.cached_snapshots[definition.key] = (definition, payload)
                    elif socket is self.sockets[1] and topic == STATE_TOPIC:
                        self._state(validate_state(decode_json(payload)), self.clock())
                    elif socket is self.sockets[1] and topic == END_TOPIC:
                        self._terminal(validate_end(decode_json(payload)), self.clock())
                    elif socket is self.sockets[0] and topic in (COMMAND_TOPIC, HEARTBEAT_TOPIC):
                        value = validate_command(decode_json(payload))
                        expected = HEARTBEAT_TOPIC if value["action"] == "heartbeat" else COMMAND_TOPIC
                        if topic == expected:
                            self._command(value, self.clock())
                except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
                    self.report(f"Rejected simulation control message: {exc}")
        if self.terminal_deadline is not None and self.clock() >= self.terminal_deadline:
            self._stop()
        # Check after draining: finalization can delay polling while state queues up.
        if self.last_state is not None and self.clock() - self.last_state >= HEARTBEAT_TIMEOUT_S:
            if self.active_episode is not None:
                self.report("Physics state timed out; closing recording")
                self.recorder.recording.outcome = "physics_timeout"
                self._stop()
        if self.active_episode is not None and not self.recorder.active:
            self._stop()

    def close(self):
        try:
            self._stop()
        finally:
            for socket in self.sockets:
                socket.close(linger=0)
            self.context.term()
