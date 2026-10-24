"""Directory overrides preserve YAML bases and precede recorder readiness."""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from mcap_bagger import cli
from mcap_bagger.config import Config
from mcap_bagger.recorder import Recorder


def test_override_changes_only_output_directory(config_files,tmp_path,monkeypatch):
    path,_=config_files
    original=Config.load(path)
    cwd=tmp_path/'working'
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    recorder=Recorder()
    recorder.init(path,output_dir='custom')
    assert recorder.config.output_dir==cwd/'custom'
    assert recorder.config.transport_path==original.transport_path
    assert recorder.config.transport==original.transport
    assert recorder.config.topics==original.topics
    assert recorder.config.project==original.project
    assert not (cwd/'custom').exists()
    recorder.init(path,output_dir='~/flight-test-output')
    assert recorder.config.output_dir==Path.home()/'flight-test-output'
    recorder.init(path)
    assert recorder.config.output_dir==original.output_dir


@pytest.mark.parametrize('value',['',' ',False,5])
def test_invalid_override_rejected(config_files,value):
    recorder=Recorder()
    with pytest.raises(ValueError,match='nonempty path'):
        recorder.init(config_files[0],output_dir=value)
    assert recorder.config is None


def test_directory_is_created_and_probe_is_cleaned(tmp_path):
    output=tmp_path/'new/recordings'
    cli._prepare_output_directory(output)
    assert output.is_dir() and list(output.iterdir())==[]
    cli._prepare_output_directory(output)
    assert list(output.iterdir())==[]


def test_existing_file_is_not_a_directory(tmp_path):
    output=tmp_path/'file'
    output.write_text('keep')
    with pytest.raises(ValueError,match=str(output)):
        cli._prepare_output_directory(output)
    assert output.read_text()=='keep'


@pytest.mark.parametrize('failure',['create','write'])
def test_permission_or_write_error_aborts_before_readiness(config_files,tmp_path,monkeypatch,capsys,failure):
    output=tmp_path/'blocked'
    ready=tmp_path/'ready'
    controlled=Mock()
    monkeypatch.setattr(cli,'ControlledRecorder',controlled)
    monkeypatch.setattr('sys.argv',['mcap-bagger','--config',str(config_files[0]),'--output-dir',str(output),'--ready-file',str(ready)])
    real_probe=cli.NamedTemporaryFile
    if failure=='create':
        def probe(**kwargs):
            raise PermissionError('denied')
    else:
        @contextmanager
        def probe(**kwargs):
            with real_probe(**kwargs):
                class BrokenWrite:
                    def write(self,data):
                        raise OSError('disk full')
                yield BrokenWrite()
    monkeypatch.setattr(cli,'NamedTemporaryFile',probe)
    assert cli.main()==1
    controlled.assert_not_called()
    assert not ready.exists()
    assert list(output.iterdir())==[]
    assert str(output) in capsys.readouterr().err


def test_override_requires_controlled_config(monkeypatch):
    monkeypatch.setattr('sys.argv',['mcap-bagger','--output-dir','somewhere'])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code==2


def test_two_controlled_episodes_use_custom_directory(config_files,tmp_path):
    import json
    import signal
    import subprocess
    import sys
    import time
    import zmq
    from bullet_msgs.shm import FrameWriter,topic_prefix
    from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
    from mcap.reader import make_reader

    profile,data=config_files
    original=Config.load(profile)
    output=tmp_path/'separate destination/nested'
    ready=tmp_path/'recorder.ready'
    with zmq.Context() as context, context.socket(zmq.PUB) as telemetry, context.socket(zmq.XPUB) as images, FrameWriter(2,2) as pixels:
        telemetry.setsockopt(zmq.LINGER,0)
        images.setsockopt(zmq.LINGER,0)
        images.setsockopt(zmq.XPUB_VERBOSE,1)
        telemetry.bind(original.transport['telemetry_endpoint'])
        images.bind(original.transport['image_endpoint'])
        process=subprocess.Popen([sys.executable,'-u','-m','mcap_bagger.cli','--config',str(profile),
                                  '--output-dir',str(output),'--ready-file',str(ready)],
                                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        def until(predicate,action=lambda:None):
            deadline=time.monotonic()+5
            while not predicate():
                assert process.poll() is None,'recorder exited before episode completed'
                assert time.monotonic()<deadline,'recording operation timed out'
                action()
                time.sleep(.01)
        try:
            until(ready.exists)
            assert output.is_dir() and not list(output.iterdir())
            for number,episode in enumerate(('first','second'),1):
                state=dict(schema_version=1,physics_id='physics',episode_id=episode,controller_id=None,state='running',reason='started',simulation_time_s=0.)
                send_state=lambda:telemetry.send_multipart([b'simulation/state',json.dumps(state).encode()])
                until(lambda:len(list(output.glob('*.partial')))==1,send_state)
                subscribed=False
                deadline=time.monotonic()+5
                while not subscribed:
                    assert time.monotonic()<deadline
                    if not images.poll(100):
                        continue
                    subscribed=images.recv()==b'\x01'+topic_prefix('camera/front/image')
                for _ in range(3):
                    images.send(pixels.write('camera/front/image',bytes([number])*12))
                    telemetry.send_multipart([b'camera/front/metadata',json.dumps({'number':number}).encode()])
                    time.sleep(.02)
                state.update(state='stopped',reason='stop')
                until(lambda:len(list(output.glob('*.mcap')))==number and not list(output.glob('*.partial')),send_state)
            process.send_signal(signal.SIGTERM)
            text,_=process.communicate(timeout=5)
            assert process.returncode==0,text
            assert f'Output: {output}' in text
            assert not ready.exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert not original.output_dir.exists()
    assert not list(output.glob('.mcap-write-check-*'))
    files=sorted(output.glob('*.mcap'))
    assert len(files)==2 and files[0].name!=files[1].name
    for number,path in enumerate(files,1):
        assert path.name.startswith(data['project']+'_')
        with path.open('rb') as file:
            reader=make_reader(file)
            assert reader.get_summary() is not None
            frames=[RawImage.FromString(message.data) for _,_,message in reader.iter_messages(topics=['camera/front/image'])]
            assert len(frames)==3 and all(frame.data==bytes([number])*12 for frame in frames)
            health=next(item for item in reader.iter_metadata() if item.name=='recording_health')
            assert json.loads(health.metadata['complete']) is True
            assert json.loads(health.metadata['episode'])['episode_id']==('first' if number==1 else 'second')
