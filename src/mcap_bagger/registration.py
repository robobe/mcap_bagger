"""Validate opaque channel configuration and create a registration-only MCAP."""

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from mcap.writer import CompressionType, Writer

def _fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError('expected a configuration mapping')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f'missing fields: {sorted(missing)}; unknown fields: {sorted(unknown)}')


def validate_endpoint(endpoint):
    match = re.fullmatch(r'tcp://127\.0\.0\.1:([1-9][0-9]{0,4})', endpoint) if isinstance(endpoint, str) else None
    if not match or not 1 <= int(match[1]) <= 65535:
        raise ValueError('expected loopback TCP endpoint tcp://127.0.0.1:<port 1-65535>')


@dataclass(frozen=True)
class Channel:
    topic: str
    endpoint: str
    shm: bool
    compress: str | None = None


@dataclass(frozen=True)
class RegistrationConfig:
    project: str
    output_dir: Path
    channels: tuple[Channel, ...]

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = yaml.safe_load(path.read_text())
        _fields(data, ('project', 'output_dir', 'channels'))
        if not isinstance(data['project'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', data['project']):
            raise ValueError('project must be 1–64 letters, digits, underscores, or hyphens')
        if not isinstance(data['output_dir'], str) or not data['output_dir'].strip():
            raise ValueError('output_dir must be a nonempty path')
        if not isinstance(data['channels'], list) or not data['channels']:
            raise ValueError('channels must be a nonempty list')
        channels = []
        topics = set()
        for item in data['channels']:
            _fields(item, ('topic', 'endpoint', 'shm'), ('compress',))
            topic = item['topic']
            if not isinstance(topic, str) or not topic.strip() or '\0' in topic or '*' in topic or len(topic.encode()) > 4096:
                raise ValueError('topic must be a nonempty exact topic of at most 4096 bytes')
            if topic in topics:
                raise ValueError('duplicate topic: ' + topic)
            topics.add(topic)
            validate_endpoint(item['endpoint'])
            if type(item['shm']) is not bool:
                raise ValueError('shm must be a boolean')
            if not item['shm'] and 'compress' in item:
                raise ValueError('compress is only allowed for SHM channels')
            compress = item.get('compress', 'raw') if item['shm'] else None
            if item['shm'] and compress not in ('raw', 'jpeg'):
                raise ValueError('compress must be raw or jpeg')
            channels.append(Channel(topic, item['endpoint'], item['shm'], compress))
        return cls(data['project'], (path.parent / data['output_dir']).resolve(), tuple(channels))


def register_channels(config):
    """Return the completed path and registered topic IDs, without receiving data."""
    config.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    final = config.output_dir / f'{config.project}_{stamp}.mcap'
    partial = final.with_suffix('.mcap.partial')
    channels = {}
    with partial.open('xb') as stream:
        writer = Writer(stream, compression=CompressionType.NONE)
        writer.start()
        for channel in config.channels:
            metadata = {'endpoint': channel.endpoint, 'shm': str(channel.shm).lower()}
            if channel.compress is not None:
                metadata['compress'] = channel.compress
            channels[channel.topic] = writer.register_channel(
                topic=channel.topic, schema_id=0, message_encoding='', metadata=metadata)
        writer.finish()
        stream.flush()
        os.fsync(stream.fileno())
    os.link(partial, final)
    partial.unlink()
    return final, channels
