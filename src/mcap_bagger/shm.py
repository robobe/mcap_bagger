"""Version-1 descriptor framing and three-slot advisory-lock SHM transport."""
import fcntl
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
import time
from uuid import uuid4

HEADER = struct.Struct('<8s6I4Q64s')
MAGIC = b'BMSHM01\0'
VERSION = 1
SLOTS = 3
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 1024
MAX_TOPIC_BYTES = 4096
MAX_MESSAGE_BYTES = MAX_TOPIC_BYTES + 1 + 4 + MAX_METADATA_BYTES
ENCODINGS = {'rgb8': 1, 'bgr8': 2}
assert HEADER.size == 128


def topic_prefix(topic):
    if not isinstance(topic, str) or not topic or '\0' in topic or '*' in topic:
        raise ValueError('expected an exact nonempty topic')
    result = topic.encode()
    if len(result) > MAX_TOPIC_BYTES:
        raise ValueError('topic exceeds 4096 bytes')
    return result + b'\0'


def slot_name(session, slot):
    return f'/bullet_msgs_{session}_s{slot}'


def _layout(width, height, encoding, step):
    if (any(type(v) is not int or not 0 < v < 2**32 for v in (width, height, step))
            or not isinstance(encoding, str) or encoding not in ENCODINGS or step < width * 3 or step * height > MAX_IMAGE_BYTES):
        raise ValueError('invalid image dimensions, encoding, stride or size')


def _reject_constant(value):
    raise ValueError(f'invalid JSON constant: {value}')


def validate_descriptor(metadata):
    required = {'version', 'session', 'slot', 'shm_name', 'width', 'height', 'encoding',
                'step', 'bytes', 'sequence', 'capture_ns'}
    if not isinstance(metadata, dict) or set(metadata) not in (required, required | {'source'}):
        raise ValueError('invalid image descriptor fields')
    for key in ('version', 'slot', 'width', 'height', 'step', 'bytes', 'sequence', 'capture_ns'):
        if type(metadata[key]) is not int:
            raise ValueError(f'{key} must be an integer')
    if metadata['version'] not in (1, 2) or metadata['slot'] not in range(SLOTS):
        raise ValueError('unsupported descriptor version or slot')
    if not isinstance(metadata['session'], str) or not re.fullmatch('[0-9a-f]{32}', metadata['session']):
        raise ValueError('invalid publisher session')
    if metadata['shm_name'] != slot_name(metadata['session'], metadata['slot']):
        raise ValueError('invalid SHM name')
    _layout(metadata['width'], metadata['height'], metadata['encoding'], metadata['step'])
    if metadata['bytes'] != metadata['step'] * metadata['height']:
        raise ValueError('invalid descriptor byte count')
    if not 1 <= metadata['sequence'] < 2**64 or not 0 <= metadata['capture_ns'] < 2**64:
        raise ValueError('invalid frame sequence or capture timestamp')
    if metadata['version'] == 2:
        source = metadata.get('source')
        if not isinstance(source, dict) or type(source.get('measurement_us')) is not int or source['measurement_us'] < 0:
            raise ValueError('invalid source measurement')
        if metadata['capture_ns'] != metadata['source']['measurement_us'] * 1000:
            raise ValueError('capture timestamp differs from source measurement')
    elif 'source' in metadata:
        raise ValueError('source requires descriptor version 2')
    return metadata


def encode_descriptor(topic, metadata):
    validate_descriptor(metadata)
    raw = json.dumps(metadata, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError('descriptor exceeds 1024 bytes')
    return topic_prefix(topic) + struct.pack('<I', len(raw)) + raw


def decode_descriptor(topic, message):
    prefix = topic_prefix(topic)
    if len(message) > MAX_MESSAGE_BYTES or not message.startswith(prefix) or len(message) < len(prefix) + 4:
        raise ValueError('invalid image notification topic or length')
    length = struct.unpack_from('<I', message, len(prefix))[0]
    start = len(prefix) + 4
    if not 1 <= length <= MAX_METADATA_BYTES or len(message) != start + length:
        raise ValueError('invalid descriptor length')
    return validate_descriptor(json.loads(message[start:].decode('utf-8'), parse_constant=_reject_constant))


class _Slot:
    def __init__(self, metadata):
        self.fd = os.open('/dev/shm' + metadata['shm_name'],
                          os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(self.fd)
            self.size = HEADER.size + metadata['bytes']
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != self.size):
                raise ValueError('invalid SHM type, owner, permissions or size')
            self.memory = mmap.mmap(self.fd, self.size, access=mmap.ACCESS_READ)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        self.memory.close()
        os.close(self.fd)


class FrameReader:
    """One topic per reader. Returns owned pixel bytes or None for a normal skip."""
    def __init__(self, topic):
        topic_prefix(topic)
        self.topic = topic
        self.session = None
        self.sequence = 0
        self.mappings = {}
        self.skipped = 0

    def read(self, message):
        metadata = decode_descriptor(self.topic, message)
        if metadata['session'] != self.session:
            self.close()
            self.session = metadata['session']
            self.sequence = 0
        if metadata['sequence'] <= self.sequence:
            self.skipped += 1
            return None
        name = metadata['shm_name']
        if name not in self.mappings:
            try:
                self.mappings[name] = _Slot(metadata)
            except FileNotFoundError:
                self.skipped += 1
                return None
        slot = self.mappings[name]
        if slot.size != HEADER.size + metadata['bytes']:
            raise ValueError('SHM layout changed within a session')
        try:
            fcntl.flock(slot.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            self.skipped += 1
            return None
        try:
            header = HEADER.unpack_from(slot.memory)
            magic, version, index, width, height, encoding, step, size, sequence, capture_ns, complete, session = header
            if complete == 0:
                self.skipped += 1
                return None
            expected = (MAGIC, VERSION, metadata['slot'], metadata['width'], metadata['height'],
                        ENCODINGS[metadata['encoding']], metadata['step'], metadata['bytes'])
            if header[:8] != expected or complete != 1 or session != metadata['session'].encode().ljust(64, b'\0'):
                raise ValueError('invalid SHM header identity or layout')
            if sequence != metadata['sequence']:
                self.skipped += 1
                return None
            if capture_ns != metadata['capture_ns']:
                raise ValueError('SHM capture timestamp differs from descriptor')
            pixels = slot.memory[HEADER.size:]
        finally:
            fcntl.flock(slot.fd, fcntl.LOCK_UN)
        self.sequence = sequence
        return metadata, pixels

    def close(self):
        for mapping in self.mappings.values():
            mapping.close()
        self.mappings.clear()


class FramePublisher:
    """Own three slots and publish complete frames compatible with FrameReader."""
    def __init__(self, width=640, height=480):
        self.width, self.height = width, height
        self.step = width * 3
        _layout(width, height, 'rgb8', self.step)
        self.session = uuid4().hex
        self.sequence = 0
        self.slots = []
        try:
            for index in range(SLOTS):
                path = Path('/dev/shm' + slot_name(self.session, index))
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600)
                self.slots.append((path, fd))
                os.fchmod(fd, 0o600)
                os.ftruncate(fd, HEADER.size + self.step * height)
        except BaseException:
            self.close()
            raise

    def write(self, topic, pixels):
        if len(pixels) != self.step * self.height:
            raise ValueError('incorrect pixel byte count')
        self.sequence += 1
        index = (self.sequence - 1) % SLOTS
        path, fd = self.slots[index]
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        try:
            stamp = time.time_ns()
            metadata = dict(version=1, session=self.session, slot=index,
                            shm_name=slot_name(self.session, index), width=self.width,
                            height=self.height, encoding='rgb8', step=self.step,
                            bytes=len(pixels), sequence=self.sequence, capture_ns=stamp)
            descriptor = encode_descriptor(topic, metadata)
            with mmap.mmap(fd, HEADER.size + len(pixels)) as memory:
                memory[:HEADER.size] = bytes(HEADER.size)
                memory[HEADER.size:] = pixels
                memory[:HEADER.size] = HEADER.pack(MAGIC, VERSION, index, self.width,
                    self.height, ENCODINGS['rgb8'], self.step, len(pixels), self.sequence,
                    stamp, 1, self.session.encode().ljust(64, b'\0'))
            return descriptor
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def close(self):
        for path, fd in self.slots:
            os.close(fd)
            path.unlink(missing_ok=True)
        self.slots.clear()
