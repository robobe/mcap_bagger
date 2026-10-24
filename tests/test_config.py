import json

import pytest
import yaml
from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
from google.protobuf.descriptor_pb2 import FileDescriptorSet

from mcap_bagger.config import Config


def test_image_padding_and_descriptor(config_files):
    path, data = config_files
    data["topics"][0].update(step=8, encoding="bgr8")
    path.write_text(yaml.safe_dump(data))
    topic = Config.load(path).topics[0]
    pixels = bytes(range(16))
    payload, timestamp = topic.encode(pixels, 1234567890)
    image = RawImage.FromString(payload)
    assert image.data == pixels
    assert (image.width, image.height, image.step, image.encoding) == (2, 2, 8, "bgr8")
    assert image.timestamp.ToNanoseconds() == timestamp == 1234567890
    descriptors = FileDescriptorSet.FromString(topic.schema_data)
    assert len(descriptors.file) == 2
    assert any(
        file.name == "google/protobuf/timestamp.proto" for file in descriptors.file
    )
    with pytest.raises(ValueError, match="expected 16"):
        topic.encode(pixels[:-1], timestamp)


@pytest.mark.parametrize(
    "change",
    [
        {"width": 0},
        {"height": True},
        {"step": 5},
        {"encoding": "jpeg"},
        {"width": 2**32},
        {"width": 4096, "height": 8192, "step": 12288},
        {"frame_id": 123},
        {"key": "camera/*"},
        {"key": ""},
        {"typo": 1},
    ],
)
def test_invalid_image_config(config_files, change):
    path, data = config_files
    data["topics"][0].update(change)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(Exception):
        Config.load(path)


def test_duplicate_topic_and_unsafe_project(config_files):
    path, data = config_files
    data["topics"].append(data["topics"][0])
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="duplicate"):
        Config.load(path)
    data["topics"].pop()
    data["project"] = "../escape"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="project"):
        Config.load(path)


def test_schema_no_remote_fetch(config_files):
    path, _ = config_files
    (path.parent / "metadata.json").write_text(
        '{"$ref":"https://example.org/schema.json"}'
    )
    with pytest.raises(ValueError, match="internal references"):
        Config.load(path)


def test_json_receive_and_invalid_data(config):
    topic = config.topics[1]
    assert topic.encode(b'{"number":42}', 77) == (b'{"number":42}', 77)
    for payload in (
        b"42",
        b'{"number":-1}',
        b'{"number":true}',
        b'{"number":NaN}',
        b'{"other":42}',
        b"not-json",
        '{"number":42}'.encode("utf-16"),
    ):
        with pytest.raises(Exception):
            topic.encode(payload, 77)


def test_json_publisher_time(config_files):
    path, data = config_files
    data["topics"][1]["timestamp_source"] = "publisher"
    path.write_text(yaml.safe_dump(data))
    schema = {"type": "object", "properties": {"timestamp_ns": {"type": "integer"}}}
    (path.parent / "metadata.json").write_text(json.dumps(schema))
    topic = Config.load(path).topics[1]
    assert topic.encode(b'{"timestamp_ns":123}', 999)[1] == 123
    for value in (
        {},
        {"timestamp_ns": 0},
        {"timestamp_ns": True},
        {"timestamp_ns": 2**64},
    ):
        with pytest.raises(Exception):
            topic.encode(json.dumps(value).encode(), 999)


def test_json_number_overflow_is_rejected(config_files):
    path, _ = config_files
    (path.parent / "metadata.json").write_text(
        '{"type":"object", "properties":{"number":{"type":"number"}}}'
    )
    topic = Config.load(path).topics[1]
    with pytest.raises(ValueError, match="finite"):
        topic.encode(b'{"number":1e400}', 77)


@pytest.mark.parametrize('endpoint', ['ipc://@old', 'tcp://0.0.0.0:5555', 'tcp://127.0.0.1:0', 'tcp://127.0.0.1:65536', 'tcp://127.0.0.1:5555\0'])
def test_invalid_transport_endpoint(config_files, endpoint):
    path, _ = config_files
    profile = path.parent/'transport.json'
    value = json.loads(profile.read_text())
    value['image_endpoint'] = endpoint
    profile.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        Config.load(path)


def test_packaged_schema_and_old_image_input(config_files):
    path, data = config_files
    data['topics'][1]['schema'] = 'bullet_msgs:pose.json'
    path.write_text(yaml.safe_dump(data))
    assert Config.load(path).topics[1].schema_name == 'pose'
    data['topics'][0]['input'] = 'raw_image'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match='shm_image'):
        Config.load(path)


@pytest.mark.parametrize('endpoint', ['ipc://invalid', 'tcp://0.0.0.0:5558',
                                      'tcp://127.0.0.1:0', '', None])
def test_invalid_json_topic_endpoint(config_files, endpoint):
    path, data = config_files
    data['topics'][1]['endpoint'] = endpoint
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match='loopback'):
        Config.load(path)


def test_json_endpoint_conflicts_with_image_and_control(config_files):
    path, data = config_files
    transport = json.loads((path.parent / 'transport.json').read_text())
    for key in ('image_endpoint', 'control_endpoint'):
        data['topics'][1]['endpoint'] = transport[key]
        path.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError, match='differ'):
            Config.load(path)


def test_only_shared_render_health_allowed_on_image_endpoint(config_files):
    path,data=config_files
    data['topics'].append(dict(key='analysis/render/health',input='json',schema='bullet_msgs:render_health.json',timestamp_source='receive',endpoint=data['transport_config']))
    # Resolve the actual endpoint from the transport file rather than a path.
    transport=json.loads((path.parent/data['transport_config']).read_text())
    data['topics'][-1]['endpoint']=transport['image_endpoint']
    path.write_text(yaml.safe_dump(data))
    assert Config.load(path).topics[-1].key=='analysis/render/health'
    data['topics'][-1]['schema']='bullet_msgs:mission.json'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError,match='JSON endpoint'):
        Config.load(path)
