import json
from pathlib import Path

import pytest
import yaml
from uuid import uuid4

from mcap_bagger.config import Config


@pytest.fixture
def config_files(tmp_path):
    identity = uuid4().hex
    (tmp_path / "transport.json").write_text(json.dumps(tcp_profile()))
    (tmp_path / "metadata.json").write_text(
        Path("examples/schemas/metadata.json").read_text()
    )
    data = {
        "project": "test",
        "output_dir": "recordings",
        "transport_config": "transport.json",
        "topics": [
            {
                "key": "camera/front/image",
                "input": "shm_image",
                "width": 2,
                "height": 2,
                "encoding": "rgb8",
                "step": 6,
                "frame_id": "camera",
            },
            {
                "key": "camera/front/metadata",
                "input": "json",
                "schema": "metadata.json",
                "timestamp_source": "receive",
            },
        ],
    }
    path = tmp_path / "recorder.yaml"
    path.write_text(yaml.safe_dump(data))
    return path, data


@pytest.fixture
def config(config_files):
    return Config.load(config_files[0])


def tcp_profile():
    import socket
    with socket.socket() as telemetry, socket.socket() as images, socket.socket() as control:
        telemetry.bind(('127.0.0.1', 0))
        images.bind(('127.0.0.1', 0))
        control.bind(('127.0.0.1', 0))
        return {'telemetry_endpoint': f'tcp://127.0.0.1:{telemetry.getsockname()[1]}',
                'image_endpoint': f'tcp://127.0.0.1:{images.getsockname()[1]}',
                'control_endpoint': f'tcp://127.0.0.1:{control.getsockname()[1]}'}
