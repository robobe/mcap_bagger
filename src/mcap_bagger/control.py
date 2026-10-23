"""Command-aware recording, with physics state as the authority for opening files."""

import time

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
        except BaseException:
            self.close()
            raise

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
                self._stop()
            else:
                self.awaiting_stop = False

    def _terminal(self, result, now):
        validate_end(result)
        source = result['final_source']
        episode = (source['physics_id'], source['episode_id'])
        if self.active_episode != episode:
            return
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
                self._stop()
        elif self.awaiting_stop:
            self.closed_episodes.add(episode)
        elif episode not in self.closed_episodes:
            self.closed_episodes.add(episode)
            try:
                self.report(self.recorder.start())
                self.active_episode = episode
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
                    if socket is self.sockets[1] and topic == STATE_TOPIC:
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
