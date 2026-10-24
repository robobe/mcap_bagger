"""Bounded receive queue and a single owner of each MCAP output file."""

import os
import json
from bullet_msgs import schema_bytes
from bullet_msgs.scenario import END_TOPIC, validate_end
from bullet_msgs.flight import decode
from bullet_msgs.control import MAX_CONTROL_BYTES
import threading
import time
from collections import deque
from datetime import datetime, timezone

import zmq
from bullet_msgs.shm import FrameReader, MAX_MESSAGE_BYTES, topic_prefix
from mcap.writer import CompressionType, Writer

from .config import Config
from jsonschema import Draft202012Validator

TELEMETRY_HWM = 64
IMAGE_HWM = 8
POLL_TIMEOUT_MS = 50
QUEUE_BYTES = 64 * 1024 * 1024
MAX_TOPIC_BYTES = 4096
MAX_TERMINAL_EVENTS = 256


class Recording:
    def __init__(self, config: Config, on_error=print):
        self.config = config
        self.on_error = on_error
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.final_path = config.output_dir / f"{config.project}_{stamp}.mcap"
        self.path = self.final_path.with_suffix(".mcap.partial")
        self.condition = threading.Condition()
        self.queue = deque()
        self.queued_bytes = 0
        self.peak_queued_bytes = 0
        self.accepting = False
        self.stopping = False
        self.error = None
        self.done = threading.Event()
        self.counts = {
            topic.key: {"accepted": 0, "written": 0}
            for topic in config.topics
        }
        self.terminal_event_ids = set()
        self.skipped_images = 0
        self.receive_stop = threading.Event()
        self.receive_ready = threading.Event()
        self.receiver = threading.Thread(target=self._receive, name="zmq-receiver")
        self.stream = None
        self.writer = None
        self.channels = {}
        self.image_metadata_channels = {}
        self.image_metadata_validator = Draft202012Validator(json.loads(schema_bytes("image_metadata.json")))
        self.outcome = "manual_close"
        self.episode = None
        self.thread = threading.Thread(target=self._write, name="mcap-writer")

    def start(self):
        try:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)
            if self.final_path.exists():
                raise FileExistsError(self.final_path)
            self.stream = self.path.open("xb")
            self.writer = Writer(self.stream, compression=CompressionType.NONE)
            self.writer.start()
            schemas = {}
            for topic in self.config.topics:
                identity = (topic.schema_name, topic.schema_encoding, topic.schema_data)
                if identity not in schemas:
                    schemas[identity] = self.writer.register_schema(*identity)
                self.channels[topic.key] = self.writer.register_channel(
                    topic=topic.key,
                    message_encoding="protobuf"
                    if topic.input == "shm_image"
                    else "json",
                    schema_id=schemas[identity],
                )
            metadata_schema = self.writer.register_schema('image_metadata', 'jsonschema', schema_bytes('image_metadata.json'))
            for topic in self.config.topics:
                if topic.input == 'shm_image':
                    key = topic.key + '/metadata'
                    if key in self.channels:
                        raise ValueError('image metadata topic is reserved: ' + key)
                    self.image_metadata_channels[topic.key] = self.writer.register_channel(topic=key, message_encoding='json', schema_id=metadata_schema)
            with self.condition:
                self.accepting = True
            self.receiver.start()
            if not self.receive_ready.wait(5):
                raise RuntimeError("ZeroMQ receiver startup timed out")
            if self.error:
                raise RuntimeError(self.error)
            self.thread.start()
        except BaseException as exc:
            self.fail(f"start failed: {exc}")
            self._disconnect()
            self._finish()
            raise

    def receive(self, topic, data, received_ns=None, *, metadata=None):
        received_ns = time.time_ns() if received_ns is None else received_ns
        try:
            metadata_bytes = b'' if metadata is None else json.dumps(metadata, allow_nan=False).encode()
            with self.condition:
                if not self.accepting:
                    return
                terminal = None
                if topic.key == END_TOPIC.decode():
                    if len(data) > MAX_CONTROL_BYTES:
                        raise ValueError('terminal result exceeds size limit')
                    terminal = decode(bytes(data), validate_end)
                if terminal is not None and terminal['event_id'] in self.terminal_event_ids:
                    return
                size = len(data) + len(metadata_bytes)
                if not size:
                    raise ValueError("empty payload")
                if self.queued_bytes + size > QUEUE_BYTES:
                    raise ValueError("64 MiB recording queue overflow")
                if terminal is not None:
                    if len(self.terminal_event_ids) >= MAX_TERMINAL_EVENTS:
                        raise ValueError('terminal event capacity exceeded')
                    self.terminal_event_ids.add(terminal['event_id'])
                self.queue.append((topic, bytes(data), received_ns, metadata_bytes))
                self.queued_bytes += size
                self.peak_queued_bytes = max(self.peak_queued_bytes, self.queued_bytes)
                self.counts[topic.key]["accepted"] += 1
                self.condition.notify()
        except Exception as exc:
            self.fail(f"{topic.key}: {exc}")

    def fail(self, message):
        with self.condition:
            if self.error is None:
                self.error = message
            self.accepting = False
            self.stopping = True
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.accepting = False
            self.stopping = True
            self.condition.notify_all()
        if self.thread.ident is not None:
            self.thread.join()
        return self.summary()

    def _receive(self):
        try:
            with zmq.Context() as context:
                sockets = []
                poller = zmq.Poller()
                topics = {topic.key.encode(): topic for topic in self.config.topics}
                readers = {topic.key: FrameReader(topic.key) for topic in self.config.topics if topic.input == 'shm_image'}
                image_sockets = set()
                try:
                    connections = {}
                    for topic in self.config.topics:
                        shm_image = topic.input == 'shm_image'
                        endpoint = topic.endpoint or self.config.transport['image_endpoint' if shm_image else 'telemetry_endpoint']
                        connections.setdefault((endpoint, shm_image), []).append(topic)
                    socket_topics = {}
                    for (endpoint, shm_image), selected in connections.items():
                        socket = context.socket(zmq.SUB)
                        sockets.append(socket)
                        socket_topics[socket] = {topic.key.encode(): topic for topic in selected}
                        socket.setsockopt(zmq.LINGER, 0)
                        socket.setsockopt(zmq.RCVHWM, IMAGE_HWM if shm_image else TELEMETRY_HWM)
                        socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES if shm_image else QUEUE_BYTES)
                        if shm_image:
                            image_sockets.add(socket)
                        for topic in selected:
                            socket.setsockopt(zmq.SUBSCRIBE, topic_prefix(topic.key) if shm_image else topic.key.encode())
                        socket.connect(endpoint)
                        poller.register(socket, zmq.POLLIN)
                    self.receive_ready.set()
                    while not self.receive_stop.is_set():
                        for socket, _ in poller.poll(POLL_TIMEOUT_MS):
                            if socket in image_sockets:
                                message = socket.recv()
                                received_ns = time.time_ns()
                                if socket.getsockopt(zmq.RCVMORE):
                                    raise ValueError('expected a single image descriptor message')
                                key = message.split(b'\0', 1)[0]
                                topic = topics.get(key)
                                if topic is None or topic.input != 'shm_image':
                                    continue
                                reader = readers[topic.key]
                                result = reader.read(message)
                                if result is None:
                                    self.skipped_images += 1
                                    continue
                                metadata, pixels = result
                                if any(metadata[name] != getattr(topic, name) for name in ('width', 'height', 'encoding', 'step')):
                                    raise ValueError('SHM image layout differs from recorder configuration')
                                self.receive(topic, pixels, received_ns, metadata=metadata)
                                continue
                            key = socket.recv(copy=False)
                            if len(key) > MAX_TOPIC_BYTES:
                                raise ValueError('topic exceeds 4096 bytes')
                            if not socket.getsockopt(zmq.RCVMORE):
                                raise ValueError('expected two message parts: topic and payload')
                            payload = socket.recv(copy=False)
                            if socket.getsockopt(zmq.RCVMORE):
                                raise ValueError('expected two message parts: topic and payload')
                            topic = socket_topics[socket].get(bytes(key))
                            if topic is not None and topic.input == 'json':
                                self.receive(topic, payload)
                finally:
                    for reader in readers.values():
                        reader.close()
                    for socket in sockets:
                        socket.close()
        except Exception as exc:
            self.fail(f"ZeroMQ receive failed: {exc}")
        finally:
            self.receive_ready.set()

    def _disconnect(self):
        self.receive_stop.set()
        if self.receiver.ident is not None:
            self.receiver.join()

    def _write(self):
        disconnected = False
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.queue or self.stopping)
                    stopping = self.stopping
                if stopping and not disconnected:
                    self._disconnect()
                    disconnected = True
                with self.condition:
                    if not self.queue:
                        if self.stopping:
                            break
                        continue
                    topic, data, received_ns, metadata_bytes = self.queue.popleft()
                    self.queued_bytes -= len(data) + len(metadata_bytes)
                try:
                    encoded, published_ns = topic.encode(data, received_ns)
                except Exception as exc:
                    self.fail(f"{topic.key}: invalid message: {exc}")
                    continue
                metadata = None
                if metadata_bytes:
                    metadata = dict(schema_version=1, image_topic=topic.key, image_log_time_ns=received_ns,
                                    image_record_sequence=self.counts[topic.key]['written'] % 2**32,
                                    metadata=json.loads(metadata_bytes))
                    self.image_metadata_validator.validate(metadata)
                self.writer.add_message(
                    channel_id=self.channels[topic.key],
                    data=encoded,
                    log_time=received_ns,
                    publish_time=published_ns,
                    sequence=self.counts[topic.key]["written"] % 2**32,
                )
                if metadata is not None:
                    self.writer.add_message(channel_id=self.image_metadata_channels[topic.key],
                                            data=json.dumps(metadata, allow_nan=False).encode(), log_time=received_ns,
                                            publish_time=received_ns, sequence=metadata['image_record_sequence'])
                self.counts[topic.key]["written"] += 1
        except Exception as exc:
            self.fail(f"write failed: {exc}")
        finally:
            self._disconnect()
            self._finish()

    def _finish(self):
        try:
            if self.writer is not None:
                health = dict(schema_version=1, episode=self.episode, launch_id=os.environ.get('BULLET_LAUNCH_ID'), complete=self.error is None, reason=self.error or 'closed',
                              outcome=self.outcome, skipped_images=self.skipped_images,
                              peak_queue_bytes=self.peak_queued_bytes, counts=self.counts)
                self.writer.add_metadata('recording_health', {key: json.dumps(value) for key, value in health.items()})
                self.writer.finish()
            if self.stream is not None:
                self.stream.flush()
                os.fsync(self.stream.fileno())
        except Exception as exc:
            self.fail(f"finalization failed: {exc}")
        finally:
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception as exc:
                    self.fail(f"file close failed: {exc}")
        if self.error is None:
            try:
                # link() publishes the completed file without replacing an existing name.
                os.link(self.path, self.final_path)
                self.path.unlink()
                self.path = self.final_path
            except Exception as exc:
                self.fail(f"completion failed: {exc}")
        with self.condition:
            self.queue.clear()
            self.queued_bytes = 0
        try:
            if self.error:
                self.on_error(self.summary())
        finally:
            self.done.set()

    def summary(self):
        status = f"INCOMPLETE: {self.error}" if self.error else "Closed successfully"
        lines = [
            f"{status}\nFile: {self.path}",
            f"Peak queue: {self.peak_queued_bytes} bytes",
            f"Skipped SHM images: {self.skipped_images}",
        ]
        for key, counts in self.counts.items():
            lines.append(
                f"  {key}: accepted={counts['accepted']}, written={counts['written']}"
            )
        return "\n".join(lines)


class Recorder:
    def __init__(self, on_error=print):
        self.config = None
        self.recording = None
        self.on_error = on_error

    @property
    def active(self):
        return self.recording is not None and not self.recording.done.is_set()

    def init(self, path):
        if self.active:
            raise ValueError("close the recording before init")
        config = Config.load(path)
        self.config = config
        return config.summary()

    def start(self):
        if self.active:
            raise ValueError("already recording")
        if self.config is None:
            raise ValueError("run init <yaml> first")
        self.recording = Recording(self.config, self.on_error)
        self.recording.start()
        return f"Recording: {self.recording.path}"

    def record_terminal(self, result):
        validate_end(result)
        if not self.active:
            return
        topic = next((t for t in self.config.topics if t.key == END_TOPIC.decode()), None)
        if topic is not None:
            self.recording.receive(topic, json.dumps(result, separators=(',', ':'), allow_nan=False).encode())

    def close(self):
        return self.recording.close() if self.recording else "No recording is open"
