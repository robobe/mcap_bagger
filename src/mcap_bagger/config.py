"""Offline configuration validation and message conversion."""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from bullet_msgs import load_transport, schema_bytes, validate_endpoint
from bullet_msgs.scenario import COLLISION_TOPIC
import yaml
from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
from google.protobuf.descriptor_pb2 import FileDescriptorSet
from jsonschema import validators


@dataclass(frozen=True)
class Topic:
    key: str
    input: str
    schema_name: str
    schema_encoding: str
    schema_data: bytes
    width: int = 0
    height: int = 0
    encoding: str = ""
    step: int = 0
    frame_id: str = ""
    timestamp_source: str = "receive"
    validator: object = None
    endpoint: str = ""

    def encode(self, data: bytes, received_ns: int) -> tuple[bytes, int]:
        if self.input == "shm_image":
            if len(data) != self.step * self.height:
                raise ValueError(
                    f"expected {self.step * self.height} image bytes, got {len(data)}"
                )
            image = RawImage(
                width=self.width,
                height=self.height,
                encoding=self.encoding,
                step=self.step,
                frame_id=self.frame_id,
                data=data,
            )
            image.timestamp.FromNanoseconds(received_ns)
            return image.SerializeToString(), received_ns
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError("JSON telemetry must be an object")
        self.validator.validate(value)
        published_ns = received_ns
        if self.timestamp_source == "publisher":
            published_ns = value.get("timestamp_ns")
            if type(published_ns) is not int or not 0 < published_ns < 2**64:
                raise ValueError("timestamp_ns must be a positive uint64 integer")
        return data, published_ns


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON number exceeds finite floating-point range")
    return result


def _invalid_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _image_descriptor() -> bytes:
    result = FileDescriptorSet()
    seen = set()

    def add(descriptor):
        if descriptor.name not in seen:
            seen.add(descriptor.name)
            for dependency in descriptor.dependencies:
                add(dependency)
            descriptor.CopyToProto(result.file.add())

    add(RawImage.DESCRIPTOR.file)
    return result.SerializeToString()


def _check_refs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("$ref", "$dynamicRef") and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("JSON Schemas must use internal references only")
            _check_refs(item)
    elif isinstance(value, list):
        for item in value:
            _check_refs(item)


def _fields(value, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError("expected a configuration mapping")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(
            f"missing fields: {sorted(missing)}; unknown fields: {sorted(unknown)}"
        )


@dataclass(frozen=True)
class Config:
    project: str
    output_dir: Path
    transport_path: Path
    transport: dict
    topics: tuple[Topic, ...]

    @classmethod
    def load(cls, path: str | Path):
        path = Path(path).resolve()
        data = yaml.safe_load(path.read_text())
        _fields(data, ("project", "output_dir", "transport_config", "topics"))
        project = data["project"]
        if not isinstance(project, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", project
        ):
            raise ValueError(
                "project must be 1–64 letters, digits, underscores, or hyphens"
            )
        for field in ("output_dir", "transport_config"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise ValueError(f"{field} must be a nonempty path")
        transport_path = (path.parent / data["transport_config"]).resolve()
        transport = load_transport(transport_path)
        if not isinstance(data["topics"], list) or not data["topics"]:
            raise ValueError("topics must be a nonempty list")
        topics = []
        keys = set()
        for item in data["topics"]:
            if not isinstance(item, dict):
                raise ValueError("each topic must be a mapping")
            kind = item.get("input")
            if kind == "shm_image":
                _fields(
                    item,
                    ("key", "input", "width", "height", "encoding", "step", "frame_id"),
                )
                for field in ("width", "height", "step"):
                    if type(item[field]) is not int or not 0 < item[field] < 2**32:
                        raise ValueError(f"{field} must be a positive uint32 integer")
                if item["encoding"] not in ("rgb8", "bgr8"):
                    raise ValueError("image encoding must be rgb8 or bgr8")
                if item["step"] < item["width"] * 3:
                    raise ValueError("step must be at least width × 3")
                if item["step"] * item["height"] > 64 * 1024 * 1024:
                    raise ValueError("one image cannot exceed the 64 MiB queue budget")
                if not isinstance(item["frame_id"], str):
                    raise ValueError("frame_id must be a string")
                topic = Topic(
                    key=item["key"],
                    input=kind,
                    schema_name="foxglove.RawImage",
                    schema_encoding="protobuf",
                    schema_data=_image_descriptor(),
                    **{
                        field: item[field]
                        for field in ("width", "height", "encoding", "step", "frame_id")
                    },
                )
            elif kind == "json":
                _fields(item, ("key", "input", "schema", "timestamp_source"), ("endpoint",))
                if "endpoint" in item:
                    validate_endpoint(item["endpoint"])
                    collision_report = item['key'] == COLLISION_TOPIC.decode() and item['schema'] == 'bullet_msgs:collision.json'
                    render_health = item['key'] == 'analysis/render/health' and item['schema'] == 'bullet_msgs:render_health.json'
                    if (item["endpoint"] == transport["image_endpoint"] and not render_health) or (item["endpoint"] == transport["control_endpoint"] and not collision_report):
                        raise ValueError("JSON endpoint must differ from image and control endpoints")
                if item["timestamp_source"] not in ("receive", "publisher"):
                    raise ValueError("timestamp_source must be receive or publisher")
                if not isinstance(item["schema"], str) or not item["schema"]:
                    raise ValueError("schema must be a nonempty JSON Schema path")
                reference = item["schema"]
                if reference.startswith("bullet_msgs:"):
                    name = reference.removeprefix("bullet_msgs:")
                    schema_path = Path(name)
                    raw_schema = schema_bytes(name)
                else:
                    schema_path = (path.parent / reference).resolve()
                    raw_schema = schema_path.read_bytes()
                schema = json.loads(
                    raw_schema,
                    parse_constant=_invalid_constant,
                    parse_float=_finite_float,
                )
                _check_refs(schema)
                validator_type = validators.validator_for(schema)
                validator_type.check_schema(schema)
                topic = Topic(
                    key=item["key"],
                    input=kind,
                    schema_name=schema_path.stem,
                    schema_encoding="jsonschema",
                    schema_data=json.dumps(schema).encode(),
                    timestamp_source=item["timestamp_source"],
                    validator=validator_type(schema),
                    endpoint=item.get("endpoint", ""),
                )
            else:
                raise ValueError("input must be shm_image or json")
            if not isinstance(topic.key, str) or not topic.key or "*" in topic.key or "\0" in topic.key or len(topic.key.encode()) > 4096:
                raise ValueError("topic key must be an exact nonempty topic without wildcards")
            if topic.key in keys:
                raise ValueError(f"duplicate topic key: {topic.key}")
            keys.add(topic.key)
            topics.append(topic)
        return cls(
            project,
            (path.parent / data["output_dir"]).resolve(),
            transport_path,
            transport,
            tuple(topics),
        )

    def summary(self) -> str:
        lines = [
            f"Project: {self.project}",
            f"Output: {self.output_dir}",
            f"ZeroMQ TCP + SHM profile: {self.transport_path}",
        ]
        for topic in self.topics:
            if topic.input == "shm_image":
                detail = f"{topic.width}×{topic.height} {topic.encoding}, step={topic.step}, frame_id={topic.frame_id!r}, time=receive"
            else:
                detail = (
                    f"JSON schema={topic.schema_name}, time={topic.timestamp_source}, endpoint={topic.endpoint or self.transport['telemetry_endpoint']}"
                )
            lines.append(f"  {topic.key}: {detail}")
        return "\n".join(lines)
