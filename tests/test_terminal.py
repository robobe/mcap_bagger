import json
import time

import pytest
import yaml
from bullet_msgs.control import STATE_TOPIC
from bullet_msgs.scenario import END_TOPIC
from mcap.reader import make_reader
from mcap_bagger.config import Config
from test_control import listener, state as legacy_state, send, until

def state(status="running", **changes):
    value = legacy_state(status=status)
    value.update(changes)
    return value



def terminal():
    source = dict(physics_id='physics', episode_id='first', stream_id='stream', step_index=2,
                  simulation_time_s=.1, measurement_us=1_000_000)
    collision = dict(schema_version=1, event_id='hit', controller_id='godot', issued_us=1_010_000,
                     source=source, target_id='red_truck', drone_position_m=[50., 0., 5.])
    return dict(schema_version=1, event_id='hit', collision=collision,
                final_source=dict(source, step_index=3, simulation_time_s=.12, measurement_us=1_020_000),
                outcome='success', reason='target_hit', ended_us=1_030_000)


def configured(config_files):
    path, data = config_files
    data['topics'].append(dict(key='scenario/ended', input='json', schema='bullet_msgs:scenario_end.json', timestamp_source='receive'))
    data['topics'].append(dict(key='render/drone_1/collision', input='json', schema='bullet_msgs:collision.json', timestamp_source='receive',
                               endpoint=json.loads((path.parent / 'transport.json').read_text())['control_endpoint']))
    path.write_text(yaml.safe_dump(data))
    return Config.load(path)


@pytest.mark.parametrize('publish_event', [False, True])
def test_terminal_saved_before_stop_and_deduplicated(config_files, publish_event):
    config = configured(config_files)
    with listener(config) as (service, telemetry, commands, now):
        until(service, lambda: service.recorder.active,
              lambda: send(telemetry, STATE_TOPIC, state(schema_version=2, terminal_result=None)))
        recording = service.recorder.recording
        result = terminal()
        if publish_event:
            for _ in range(3):
                send(telemetry, END_TOPIC, result)
        stopped = state(status='stopped', schema_version=2, reason='target_hit', simulation_time_s=.12, terminal_result=result)
        until(service, lambda: service.terminal_deadline is not None,
              lambda: send(telemetry, STATE_TOPIC, stopped))
        assert service.recorder.active
        # A final app diagnostic can still enter the same bag during the drain.
        recording.receive(config.topics[1], b'{"number":99}')
        send(telemetry, END_TOPIC, result)
        send(telemetry, STATE_TOPIC, stopped)
        now[0] += .249
        service.poll()
        assert service.recorder.active
        now[0] += .002
        until(service, lambda: not service.recorder.active)
        assert recording.error is None
        with recording.path.open('rb') as stream:
            messages = [(c.topic, json.loads(m.data)) for _, c, m in make_reader(stream).iter_messages()]
        assert [value for key, value in messages if key == 'scenario/ended'] == [result]
        assert ('camera/front/metadata', {'number': 99}) in messages
        send(telemetry, STATE_TOPIC, stopped)
        service.poll()
        assert service.recorder.recording is recording
        assert len(list(config.output_dir.glob('*.mcap'))) == 1


def test_late_terminal_never_opens_bag(config_files):
    config = configured(config_files)
    with listener(config) as (service, telemetry, commands, now):
        value = state(status='stopped', schema_version=2, reason='target_hit', simulation_time_s=.12, terminal_result=terminal())
        until(service, lambda: service.episode is not None, lambda: send(telemetry, STATE_TOPIC, value))
        send(telemetry, END_TOPIC, terminal())
        service.poll()
        assert service.recorder.recording is None
