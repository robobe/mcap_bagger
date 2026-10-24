"""Image identity pairing, final health and offline clipping report readback."""
from dataclasses import replace
import json
from pathlib import Path
import time

from jsonschema import Draft202012Validator
from mcap.reader import make_reader
from bullet_msgs import schema_bytes
from mcap_bagger.config import Topic
from mcap_bagger.recorder import Recording
from mcap_bagger.report import make_report


def mission_topic():
    schema=json.loads(schema_bytes('mission.json'))
    return Topic(key='app/drone_1/mission',input='json',schema_name='mission',schema_encoding='jsonschema',schema_data=json.dumps(schema).encode(),validator=Draft202012Validator(schema))


def test_report_matches_exact_rejected_frame_and_handles_legacy(config,tmp_path):
    topic=mission_topic()
    def analysis_topic(name,key):
        schema=json.loads(schema_bytes(name+'.json'))
        return Topic(key=key,input='json',schema_name=name,schema_encoding='jsonschema',schema_data=json.dumps(schema).encode(),validator=Draft202012Validator(schema))
    truth_topic=analysis_topic('analysis_ground_truth','analysis/physics/ground_truth')
    sensors_topic=analysis_topic('analysis_sensors','analysis/physics/sensors')
    run=Recording(replace(config,topics=(*config.topics,topic,truth_topic,sensors_topic)))
    run.start()
    run.episode = dict(physics_id='p', episode_id='e')
    source=dict(physics_id='p',episode_id='e',stream_id='s',step_index=1,simulation_time_s=1.,measurement_us=1000)
    stamp=1_000_000_000_000_000_000
    metadata=dict(session='session',sequence=42,capture_ns=1_000_000,source=source)
    status=dict(schema_version=1,physics_id='p',episode_id='e',app_id='a',sequence=1,measurement_us=1200,simulation_time_s=1.,phase='LOS_APPROACH',reason='recovering',relative_altitude_m=2.,vertical_speed_m_s=0.,hover_remaining_s=0.,target_remaining_s=0.,navigator_state='RECOVERY',diagnostics=dict(vision_reason='clipped_target',processed_image_sequence=42,processed_image_stream_id='s',processed_image_timestamp_s=.001,processed_bbox=[0,1,1,1],edge_margins_px=[0,1,1,0],touched_edges=['left','bottom']))
    try:
        run.receive(config.topics[0],bytes(range(12)),stamp,metadata=metadata)
        # A neighboring frame is closer in log time than the actual exposure.
        run.receive(config.topics[0],bytes(12),stamp+20_000_000,metadata=dict(metadata,session='replacement',sequence=42,capture_ns=2_000_000,source=dict(source,measurement_us=2000,step_index=2)))
        truth=dict(episode_id='e',step_index=1,simulation_time_s=1.,drone_id='drone_1',position_m=[0.,0.,2.],orientation_xyzw=[0.,0.,0.,1.],linear_velocity_m_s=[0.]*3,angular_velocity_rad_s=[0.]*3,rotor_speeds_rad_s=[0.]*4)
        run.receive(truth_topic,json.dumps(dict(schema_version=1,source=source,publisher_drops=0,ground_truth=truth)).encode(),stamp+25_000_000)
        sample=dict(episode_id='e',step_index=1,simulation_time_s=1.)
        sensors=dict(imu=None,barometer=dict(sample,pressure_pa=101000.,altitude_m=3.),rangefinder=None)
        run.receive(sensors_topic,json.dumps(dict(schema_version=1,source=source,publisher_drops=0,sensors=sensors,attitude_estimate=dict(sample,orientation_xyzw=[0.,0.,0.,1.]))).encode(),stamp+26_000_000)
        run.receive(topic,json.dumps(dict(status,episode_id='old')).encode(),stamp+29_000_000)
        run.receive(topic,json.dumps(status).encode(),stamp+30_000_000)
    finally:
        run.close()
    assert run.error is None
    with run.path.open('rb') as f:
        reader=make_reader(f)
        health=next(item for item in reader.iter_metadata() if item.name=='recording_health')
        value={k:json.loads(v) for k,v in health.metadata.items()}
        Draft202012Validator(json.loads(schema_bytes('recording_health.json'))).validate(value)
        assert value['complete'] and value['counts'][config.topics[0].key]['written']==2
        messages=list(reader.iter_messages())
        metadata_messages=[m for _,c,m in messages if c.topic.endswith('/metadata')]
        images=[m for _,c,m in messages if c.topic==config.topics[0].key]
        for record,image in zip(metadata_messages,images):
            value=json.loads(record.data)
            Draft202012Validator(json.loads(schema_bytes('image_metadata.json'))).validate(value)
            assert (value['image_log_time_ns'],value['image_record_sequence'])==(image.log_time,image.sequence)
    output=tmp_path/'report'
    report=make_report(run.path,output,True)
    assert report['first_clipping']['mission']['episode_id']=='e'
    exact=[f for f in report['frames'] if f['exact_rejected_frame']]
    assert len(exact)==1 and exact[0]['metadata']['sequence']==42
    assert exact[0]['image_log_time_ns']==stamp
    assert (output/exact[0]['file']).read_bytes().startswith(b'\x89PNG')
    assert (output/'telemetry.csv').exists()
    assert report['first_clipping']['mission']['diagnostics']['touched_edges']==['left','bottom']
    assert report['sensor_errors'][0]['altitude_error_m']==1.
    assert report['sensor_errors'][0]['attitude_error_deg']==0.
    legacy=Recording(config)
    legacy.start()
    legacy.receive(config.topics[0],bytes(12))
    legacy.close()
    result=make_report(legacy.path,tmp_path/'legacy')
    assert result['first_clipping'] is None and result['frames']==[]


def test_recording_health_marks_data_loss(config):
    run=Recording(config)
    run.start()
    run.fail('injected data loss')
    run.close()
    assert run.path.name.endswith('.partial')
    with run.path.open('rb') as f:
        health=next(item for item in make_reader(f).iter_metadata() if item.name=='recording_health')
        assert json.loads(health.metadata['complete']) is False
        assert json.loads(health.metadata['reason'])=='injected data loss'


def test_640x480_30hz_recording_overhead(config,tmp_path):
    image=replace(config.topics[0],width=640,height=480,step=1920)
    run=Recording(replace(config,topics=(image,)))
    pixels=bytes(640*480*3)
    run.start()
    enqueue=[]
    start=time.monotonic()
    try:
        for sequence in range(60):
            before=time.monotonic()
            run.receive(image,pixels,metadata=dict(session='bench',sequence=sequence,capture_ns=sequence))
            enqueue.append(time.monotonic()-before)
            time.sleep(max(0,start+(sequence+1)/30-time.monotonic()))
    finally:
        run.close()
    result=dict(frames=run.counts[image.key]['written'],duration_s=time.monotonic()-start,max_enqueue_ms=max(enqueue)*1000,peak_queue_bytes=run.peak_queued_bytes,error=run.error)
    Path('/tmp/bullet-recording-benchmark.json').write_text(json.dumps(result,indent=2))
    assert run.error is None and result['frames']==60
    assert run.peak_queued_bytes < 64*1024*1024
