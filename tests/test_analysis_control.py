"""Preflight config cache and late fault diagnostics survive the stop drain."""
from dataclasses import replace
import json

from jsonschema import Draft202012Validator
from mcap.reader import make_reader
from bullet_msgs import schema_bytes
from bullet_msgs.control import STATE_TOPIC
from mcap_bagger.config import Topic
from test_control import listener, send, state, until
from test_analysis_recording import mission_topic


def test_preflight_snapshot_and_late_fault_are_recorded(config):
    schema=json.loads(schema_bytes('analysis_config.json'))
    definition=Topic(key='analysis/physics/config',input='json',schema_name='analysis_config',schema_encoding='jsonschema',schema_data=json.dumps(schema).encode(),validator=Draft202012Validator(schema))
    mission=mission_topic()
    config=replace(config,topics=(*config.topics,definition,mission))
    source=dict(physics_id='physics',episode_id='initial',stream_id='s',step_index=0,simulation_time_s=0.,measurement_us=1)
    packet=dict(schema_version=1,source=source,role='physics',settings={'wind':{'seed':2}},revisions={})
    with listener(config) as (service,telemetry,commands,now):
        until(service,lambda:definition.key in service.cached_snapshots,
              lambda:send(telemetry,definition.key.encode(),packet))
        until(service,lambda:service.recorder.active,lambda:send(telemetry,STATE_TOPIC,state()))
        run=service.recorder.recording
        assert run.counts[definition.key]['accepted']>=1
        send(telemetry,STATE_TOPIC,state(status='stopped'))
        until(service,lambda:service.terminal_deadline is not None)
        assert service.recorder.active
        fault=dict(schema_version=1,physics_id='physics',episode_id='first',app_id='a',sequence=1,measurement_us=1000,simulation_time_s=1.,phase='FAULT',reason='los_image_deadline',relative_altitude_m=2.,vertical_speed_m_s=0.,hover_remaining_s=0.,target_remaining_s=0.,navigator_state='FAULT',diagnostics={'vision_reason':'clipped_target'})
        until(service,lambda:run.outcome.startswith('mission_fault:'),lambda:send(telemetry,mission.key.encode(),fault))
        until(service,lambda:not service.recorder.active)
        with run.path.open('rb') as file:
            reader=make_reader(file)
            assert any(c.topic==mission.key and json.loads(m.data)['phase']=='FAULT' for _,c,m in reader.iter_messages())
            health=next(m for m in reader.iter_metadata() if m.name=='recording_health')
            assert json.loads(health.metadata['outcome'])=='mission_fault:los_image_deadline'


def test_cli_readiness_marker_and_signal_cleanup(config_files,tmp_path):
    import signal
    import subprocess
    import sys
    import time
    marker=tmp_path/'recorder.ready'
    process=subprocess.Popen([sys.executable,'-m','mcap_bagger.cli','--config',str(config_files[0]),'--ready-file',str(marker)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    try:
        deadline=time.monotonic()+5
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic()<deadline
            time.sleep(.01)
        process.send_signal(signal.SIGTERM)
        stdout,_=process.communicate(timeout=5)
        assert process.returncode==0,stdout
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
