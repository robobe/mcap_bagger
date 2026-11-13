"""Independent message/image producers and a single MCAP file owner."""
import io
import json
import threading
import time

import zmq
from loguru import logger
from PIL import Image

from .writer import SharedQueues, McapWriter

from .shm import FrameReader, topic_prefix, MAX_MESSAGE_BYTES

QUEUE_BYTES = 64 * 1024 * 1024
QUEUE_ENTRIES = 4096


def parse_message_header(raw):
    def unique_fields(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError('duplicate message header key')
            fields[key] = value
        return fields

    def reject_constant(value):
        raise ValueError('invalid JSON constant in message header')

    header = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_fields,
                        parse_constant=reject_constant)
    if not isinstance(header, dict):
        raise ValueError('message header must be an object')
    for key, limit in (('publish_time_ns', 2**64), ('sequence', 2**32)):
        value = header.get(key)
        if type(value) is not int or not 0 <= value < limit:
            raise ValueError(f'invalid message header {key}')
    return header['publish_time_ns'], header['sequence']


class Pipeline:
    def __init__(self, config, filename=None):
        self.filename = filename
        self.ready = threading.Event()
        self.done = threading.Event()
        self.thread = None
        self.writer = None
        self.config = config
        self.shared = SharedQueues(QUEUE_BYTES, QUEUE_ENTRIES)
        self.condition = self.shared.condition
        self.queues = self.shared.queues
        self.sizes = self.shared.sizes
        self.stop = threading.Event()
        self.error = None
        self.skipped_images = 0
        self.producers = []

    def fail(self, exc):
        with self.condition:
            if self.error is None:
                self.error = exc
                logger.error('{}', exc)
            self.stop.set()
            self.condition.notify_all()

    def enqueue(self, index, item):
        self.shared.put(index, item)

    def receive(self, index):
        readers = {}
        try:
            with zmq.Context() as context:
                sockets = []
                poller = zmq.Poller()
                try:
                    endpoints = {}
                    for channel in self.config.channels:
                        if channel.shm == bool(index):
                            endpoints.setdefault(channel.endpoint, {})[channel.topic.encode()] = channel
                            if index:
                                readers[channel.topic] = FrameReader(channel.topic)
                    for endpoint, topics in endpoints.items():
                        socket = context.socket(zmq.SUB)
                        sockets.append(socket)
                        socket.setsockopt(zmq.LINGER, 0)
                        socket.setsockopt(zmq.RCVHWM, 8 if index else 64)
                        socket.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES if index else QUEUE_BYTES)
                        for key, channel in topics.items():
                            socket.setsockopt(zmq.SUBSCRIBE, topic_prefix(channel.topic) if index else key)
                        socket.connect(endpoint)
                        poller.register(socket, zmq.POLLIN)
                    routes = dict(zip(sockets, endpoints.values()))
                    while not self.stop.is_set():
                        for socket, _ in poller.poll(50):
                            parts = socket.recv_multipart()
                            received = time.time_ns()
                            if (index and len(parts) != 1) or (not index and len(parts) not in (2, 3)):
                                raise ValueError('invalid ZeroMQ message framing')
                            key = parts[0].split(b'\0', 1)[0] if index else parts[0]
                            channel = routes[socket].get(key)
                            if channel is None:
                                continue
                            if index:
                                result = readers[channel.topic].read(parts[0])
                                if result is None:
                                    self.skipped_images += 1
                                    continue
                                metadata, payload = result
                                if channel.compress == 'jpeg':
                                    image = Image.frombytes('RGB', (metadata['width'], metadata['height']), payload,
                                                            'raw', 'RGB' if metadata['encoding'] == 'rgb8' else 'BGR', metadata['step'])
                                    with io.BytesIO() as buffer:
                                        image.save(buffer, format='JPEG', quality=90)
                                        payload = buffer.getvalue()
                            else:
                                payload = parts[-1]
                            item = (channel.topic, received, payload)
                            if not index and len(parts) == 3:
                                item += parse_message_header(parts[1])
                            self.enqueue(index, item)
                finally:
                    for socket in sockets:
                        socket.close()
        except Exception as exc:
            self.fail(exc)
        finally:
            for reader in readers.values():
                reader.close()
            with self.condition:
                self.condition.notify_all()

    def write(self):
        writer = McapWriter(self.config, self.shared, self.filename)
        self.writer = writer
        try:
            writer.start()
            self.ready.set()
            logger.info("Recording started: {} ({} channels)", writer.path, len(self.config.channels))
            for index in range(2):
                thread = threading.Thread(target=self.receive, args=(index,),
                                          name=('message-receiver', 'image-handler')[index])
                self.producers.append(thread)
                thread.start()
            while not self.stop.wait(.05):
                if writer.done.is_set():
                    if writer.error is not None:
                        self.fail(writer.error)
                    break
        except BaseException as exc:
            self.fail(exc)
        finally:
            self.ready.set()
            self.stop.set()
            for thread in self.producers:
                if thread.ident is not None:
                    thread.join()
            if writer.thread.ident is not None:
                try:
                    writer.finish(success=self.error is None)
                except Exception as exc:
                    self.fail(exc)

    def start(self):
        def coordinate():
            try:
                self.write()
            finally:
                self.ready.set()
                self.done.set()
        self.thread = threading.Thread(target=coordinate, name='pipeline-coordinator')
        self.thread.start()
        self.ready.wait()
        if self.error:
            self.thread.join()
            raise self.error

    def finish(self):
        self.stop.set()
        self.thread.join()
        if self.error:
            raise self.error
        return self.writer.path

    def run(self):
        try:
            self.start()
            try:
                while not self.done.wait(.1):
                    pass
            except KeyboardInterrupt:
                self.stop.set()
            self.finish()
        except Exception as exc:
            self.fail(exc)
        return 1 if self.error else 0
