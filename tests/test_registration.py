from pathlib import Path
import subprocess

import pytest
import yaml
from mcap.reader import make_reader

from mcap_bagger.registration import RegistrationConfig, register_channels


def test_registration_readback(tmp_path):
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['output_dir'] = 'output'
    del data['channels'][0]['compress']
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    config = RegistrationConfig.load(path)
    assert config.output_dir == tmp_path / 'output'
    assert config.channels[0].compress == 'raw'
    output, ids = register_channels(config)
    with output.open('rb') as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        assert not summary.schemas
        assert set(ids) == {c.topic for c in summary.channels.values()}
        assert len(ids) == 3
        assert not list(reader.iter_messages())
        image = next(c for c in summary.channels.values() if c.topic == 'camera/front/image')
        assert image.schema_id == 0 and image.message_encoding == ''
        assert image.metadata['compress'] == 'raw'
    assert not list(config.output_dir.glob('*.partial'))


@pytest.mark.parametrize('change', [
    {'shm': 'false'}, {'compress': 'png'}, {'unknown': 1},
    {'endpoint': 'bad'}, {'topic': ''},
])
def test_invalid_channels(tmp_path, change):
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['channels'][0].update(change)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        RegistrationConfig.load(path)


def test_run_requires_config():
    result = subprocess.run(['mcap-bagger', 'run'], capture_output=True, text=True)
    assert result.returncode == 2
    assert 'requires --config' in result.stderr


def test_run_creates_mcap_and_reports_invalid_config(tmp_path):
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['output_dir'] = 'output'
    data['channels'][0]['compress'] = 'jpeg'
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    import time
    process = subprocess.Popen(['mcap-bagger', 'run', '--config', str(path)])
    try:
        deadline = time.monotonic() + 5
        while not list((tmp_path / 'output').glob('*.partial')):
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        time.sleep(.1)
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert len(list((tmp_path / 'output').glob('*.mcap'))) == 1
    data['channels'].append(data['channels'][0])
    path.write_text(yaml.safe_dump(data))
    result = subprocess.run(['mcap-bagger', 'run', '--config', str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'duplicate topic' in result.stderr
    assert len(list((tmp_path / 'output').glob('*.mcap'))) == 1


def test_registration_failure_keeps_partial_file(tmp_path, monkeypatch):
    from mcap_bagger import registration

    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['output_dir'] = 'output'
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))

    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(registration.Writer, 'finish', fail)
    with pytest.raises(OSError, match='disk full'):
        register_channels(RegistrationConfig.load(path))
    assert not list((tmp_path / 'output').glob('*.mcap'))
    assert len(list((tmp_path / 'output').glob('*.partial'))) == 1


@pytest.mark.parametrize('interval', [-1, True, '1', float('inf'), float('nan'), None])
def test_invalid_sync_interval(tmp_path, interval):
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    data['sync_interval_s'] = interval
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match='sync_interval_s'):
        RegistrationConfig.load(path)


@pytest.mark.parametrize('value', [0, .5, 'omitted'])
def test_sync_interval_default_and_positive(tmp_path, value):
    data = yaml.safe_load(Path('config/recorder.yaml').read_text())
    if value == 'omitted':
        data.pop('sync_interval_s')
    else:
        data['sync_interval_s'] = value
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(data))
    assert RegistrationConfig.load(path).sync_interval_s == (0 if value == 'omitted' else value)
