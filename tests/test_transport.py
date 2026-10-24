"""Actual TCP signaling, SHM images, exact topics and independent MCAP readback."""
import json
import os
import time

import pytest
import zmq
import yaml
from mcap.reader import make_reader
from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
from bullet_msgs.shm import FrameWriter, topic_prefix

from mcap_bagger.config import Config
from mcap_bagger.recorder import Recording


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'timed out waiting for TCP delivery'
        time.sleep(0.01)


def test_real_transport_late_publisher_and_exact_topics(config):
    run = Recording(config, on_error=lambda message: None)
    run.start()
    try:
        with FrameWriter(2, 2) as writer, zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            publisher.setsockopt(zmq.LINGER, 0)
            publisher.bind(config.transport['image_endpoint'])
            assert publisher.poll(5000)
            assert publisher.recv() == b'\x01' + topic_prefix('camera/front/image')
            publisher.send(writer.write('camera/front/image/extra', bytes(12)))
            message = writer.write('camera/front/image', bytes(range(12)))
            publisher.send(message)
            wait_for(lambda: run.counts['camera/front/image']['written'] == 1 or run.done.is_set())
            publisher.send(message)
            wait_for(lambda: run.skipped_images == 1 or run.done.is_set())
    finally:
        run.close()
    assert run.error is None
    with run.path.open('rb') as stream:
        messages = list(make_reader(stream).iter_messages())
        assert len(messages) == 2
        metadata = next(json.loads(m.data) for _, c, m in messages if c.topic.endswith('/metadata'))
        image = next(m for _, c, m in messages if not c.topic.endswith('/metadata'))
        assert metadata['image_record_sequence'] == image.sequence
        assert metadata['image_log_time_ns'] == image.log_time
        assert metadata['metadata']['sequence'] == 2
        messages = [(s, c, m) for s, c, m in messages if not c.topic.endswith('/metadata')]
        assert RawImage.FromString(messages[0][2].data).data == bytes(range(12))


@pytest.mark.parametrize('parts', [[topic_prefix('camera/front/image')],
                                  [topic_prefix('camera/front/image'), b'', b'extra']])
def test_malformed_descriptor_stops_recording(config, parts):
    run = Recording(config, on_error=lambda message: None)
    run.start()
    try:
        with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            publisher.setsockopt(zmq.LINGER, 0)
            publisher.bind(config.transport['image_endpoint'])
            assert publisher.poll(5000)
            publisher.recv()
            publisher.send_multipart(parts)
            assert run.done.wait(5)
    finally:
        run.close()
    assert run.error and run.path.suffix == '.partial'


def test_reused_and_missing_slots_skip_without_failing(config):
    run = Recording(config, on_error=lambda message: None)
    run.start()
    try:
        with FrameWriter(2, 2) as writer, zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            publisher.setsockopt(zmq.LINGER, 0)
            publisher.bind(config.transport['image_endpoint'])
            assert publisher.poll(5000)
            publisher.recv()
            stale = writer.write('camera/front/image', bytes(12))
            for _ in range(3):
                current = writer.write('camera/front/image', bytes(range(12)))
            publisher.send(stale)
            wait_for(lambda: run.skipped_images == 1)
            publisher.send(current)
            wait_for(lambda: run.counts['camera/front/image']['written'] == 1)
            writer.close()
            with FrameWriter(2, 2) as other:
                missing = other.write('camera/front/image', bytes(12))
            publisher.send(missing)
            wait_for(lambda: run.skipped_images == 2)
    finally:
        run.close()
    assert run.error is None


def test_wrong_layout_fails_recording(config):
    run = Recording(config, on_error=lambda message: None)
    run.start()
    try:
        with FrameWriter(3, 2) as writer, zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            publisher.setsockopt(zmq.LINGER, 0)
            publisher.bind(config.transport['image_endpoint'])
            assert publisher.poll(5000)
            publisher.recv()
            publisher.send(writer.write('camera/front/image', bytes(18)))
            assert run.done.wait(5)
    finally:
        run.close()
    assert 'layout' in run.error


@pytest.fixture
def pose_config(config_files):
    path, data = config_files
    data['topics'][1]['key'] = 'render/drone_1/pose'
    data['topics'][1]['schema'] = 'bullet_msgs:pose.json'
    path.write_text(yaml.safe_dump(data))
    return Config.load(path)


def pose_message():
    return dict(schema_version=2, stream_id='stream', drone_id='drone_1',
                episode_id='episode', sequence=0, publisher_drops=0, step_index=0,
                simulation_time_s=0, position_m=[0, 0, 3], orientation_xyzw=[0, 0, 0, 1],
                linear_velocity_m_s=[1.25, -2.5, 0.75])


def test_pose_velocity_tcp_to_mcap_readback(pose_config):
    run = Recording(pose_config, on_error=lambda message: None)
    packet = pose_message()
    run.start()
    try:
        with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            publisher.setsockopt(zmq.LINGER, 0)
            publisher.bind(pose_config.transport['telemetry_endpoint'])
            assert publisher.poll(5000)
            assert publisher.recv() == b'\x01render/drone_1/pose'
            publisher.send_multipart([b'render/drone_1/pose', json.dumps(packet).encode()])
            wait_for(lambda: run.counts['render/drone_1/pose']['written'] == 1 or run.done.is_set())
    finally:
        run.close()
    assert run.error is None
    with run.path.open('rb') as stream:
        messages = list(make_reader(stream).iter_messages())
        assert len(messages) == 1
        schema, channel, message = messages[0]
        assert channel.topic == 'render/drone_1/pose'
        assert json.loads(message.data) == packet
        embedded = json.loads(schema.data)
        assert [v['properties']['schema_version']['const'] for v in embedded['oneOf']] == [2, 3]
        assert all('linear_velocity_m_s' in v['required'] for v in embedded['oneOf'])


@pytest.mark.parametrize('velocity', [[0, 0], [True, 0, 0], [float('nan'), 0, 0]])
def test_invalid_pose_velocity_keeps_partial_file(pose_config, velocity):
    run = Recording(pose_config, on_error=lambda message: None)
    packet = pose_message()
    packet['linear_velocity_m_s'] = velocity
    run.start()
    try:
        with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
            publisher.setsockopt(zmq.LINGER, 0)
            publisher.bind(pose_config.transport['telemetry_endpoint'])
            assert publisher.poll(5000)
            publisher.recv()
            publisher.send_multipart([b'render/drone_1/pose', json.dumps(packet).encode()])
            assert run.done.wait(5)
    finally:
        run.close()
    assert run.error and run.path.suffix == '.partial'
    assert run.counts['render/drone_1/pose']['written'] == 0


@pytest.mark.parametrize('invalid', [False, True])
def test_independent_bbox_endpoint_exact_topic_and_schema(config_files, invalid):
    path, data = config_files
    with zmq.Context() as context, context.socket(zmq.XPUB) as publisher:
        publisher.setsockopt(zmq.LINGER, 0)
        publisher.bind_to_random_port('tcp://127.0.0.1')
        data['topics'].append(dict(key='tracking/front/bbox', input='json',
            endpoint=publisher.getsockopt_string(zmq.LAST_ENDPOINT),
            schema='bullet_msgs:bbox.json', timestamp_source='receive'))
        path.write_text(yaml.safe_dump(data))
        run = Recording(Config.load(path), on_error=lambda message: None)
        run.start()
        try:
            assert publisher.poll(5000)
            assert publisher.recv() == b'\x01tracking/front/bbox'
            packet = dict(schema_version=1, image_topic='camera/front/image',
                          session='a' * 32, sequence=1, capture_ns=123,
                          bbox=dict(x=1, y=2, width=0 if invalid else 3, height=4))
            publisher.send_multipart([b'tracking/front/bbox/other', b'invalid JSON'])
            publisher.send_multipart([b'tracking/front/bbox', json.dumps(packet).encode()])
            wait_for(lambda: run.counts['tracking/front/bbox']['written'] == 1 or run.done.is_set())
        finally:
            run.close()
    if invalid:
        assert run.error and run.path.suffix == '.partial'
        assert run.counts['tracking/front/bbox']['written'] == 0
    else:
        assert run.error is None
        with run.path.open('rb') as stream:
            messages = list(make_reader(stream).iter_messages())
        assert len(messages) == 1
        schema, channel, message = messages[0]
        assert channel.topic == 'tracking/front/bbox'
        assert schema.name == 'bbox'
        assert json.loads(message.data) == packet
