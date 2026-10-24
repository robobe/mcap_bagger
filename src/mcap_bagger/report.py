"""Offline flight diagnostics. Reads MCAP without changing controller settings."""
import argparse
from bisect import bisect_left
import csv
import html
import json
from math import acos, degrees
from pathlib import Path
import struct
import zlib

from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
from mcap.reader import make_reader

WINDOW_NS = 2_000_000_000
MAX_FRAMES = 5
MAX_ALIGNMENT_S = .02
MISSION = 'app/drone_1/mission'
TRUTH = 'analysis/physics/ground_truth'
SENSORS = 'analysis/physics/sensors'
METADATA = 'camera/front/image/metadata'
SIGNALS = [MISSION, TRUTH, SENSORS, 'tracking/front/bbox', 'tracking/front/los',
           'flight/drone_1/rc', 'flight/drone_1/feedback', 'simulation/wind_state', 'simulation/state', 'analysis/render/health', METADATA]
CONFIGS = ['analysis/physics/config', 'analysis/app/config', 'flight/drone_1/config']


def flatten(value, prefix=''):
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(flatten(item, f'{prefix}.{key}' if prefix else key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            result.update(flatten(item, f'{prefix}.{index}'))
    else:
        result[prefix] = value
    return result


def write_png(path, image):
    if image.encoding not in ('rgb8', 'bgr8') or image.step < image.width * 3 or len(image.data) != image.step * image.height:
        raise ValueError('unsupported recorded image layout')
    rows = []
    for y in range(image.height):
        row = image.data[y * image.step:y * image.step + image.width * 3]
        if image.encoding == 'bgr8':
            rgb = bytearray(row)
            rgb[0::3], rgb[2::3] = row[2::3], row[0::3]
            row = bytes(rgb)
        rows.append(b'\0' + row)
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', image.width, image.height, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))


def make_report(path, output, export_csv=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    event = last = None
    configs, health = {}, None
    # Only read small JSON channels first; never load a flight's image data into RAM.
    with Path(path).open('rb') as file:
        reader = make_reader(file)
        summary = reader.get_summary()
        if summary is None:
            raise ValueError('MCAP has no index; recover/index the partial file before analysis')
        channels = {c.topic: summary.statistics.channel_message_counts.get(cid, 0) for cid, c in summary.channels.items()}
        for item in reader.iter_metadata():
            if item.name == 'recording_health':
                health = {key: json.loads(value) for key, value in item.metadata.items()}
        episode = None if health is None else health.get('episode')
        def foreign(value):
            return episode is not None and any(value.get(key) is not None and value[key] != expected for key, expected in episode.items())
        for _, channel, message in reader.iter_messages(topics=[MISSION] + CONFIGS):
            value = json.loads(message.data)
            if channel.topic in CONFIGS:
                configs[channel.topic] = value
                continue
            if foreign(value):
                continue
            last = value
            d = value.get('diagnostics', {})
            if event is None and d.get('vision_reason') == 'clipped_target':
                event = dict(log_time_ns=message.log_time, mission=value, configurations=dict(configs))
        anchor = event['log_time_ns'] if event else summary.statistics.message_end_time
        rows, identities = [], []
        for _, channel, message in reader.iter_messages(topics=SIGNALS, starting_at=max(0, anchor - WINDOW_NS), ending_at=anchor + WINDOW_NS):
            value = json.loads(message.data)
            identity = value.get('source', value.get('metadata', {}).get('source', value))
            if foreign(identity):
                continue
            rows.append(dict(topic=channel.topic, log_time_ns=message.log_time, value=value))
            if channel.topic == METADATA:
                identities.append(value)
        d = {} if event is None else event['mission']['diagnostics']
        def exact(item):
            metadata = item['metadata']
            source = metadata.get('source', {})
            return (metadata['sequence'] == d.get('processed_image_sequence')
                    and source.get('stream_id') == d.get('processed_image_stream_id')
                    and source.get('episode_id') == event['mission']['episode_id']
                    and source.get('physics_id') == event['mission']['physics_id']
                    and d.get('processed_image_timestamp_s') is not None
                    and source.get('measurement_us') == round(d['processed_image_timestamp_s'] * 1e6)) if event else False
        selected = sorted(identities, key=lambda v: (not exact(v), abs(v['image_log_time_ns'] - anchor)))[:MAX_FRAMES]
        image_keys = {(v['image_topic'], v['image_log_time_ns'], v['image_record_sequence']): v for v in selected}
        frames = []
        for _, channel, message in reader.iter_messages(topics=['camera/front/image'], starting_at=max(0, anchor - WINDOW_NS), ending_at=anchor + WINDOW_NS):
            identity = image_keys.get((channel.topic, message.log_time, message.sequence))
            if identity is None:
                continue
            image = RawImage.FromString(message.data)
            name = f'frame_{message.log_time}_{message.sequence}.png'
            write_png(output / name, image)
            frames.append(dict(file=name, exact_rejected_frame=exact(identity), **identity))
    truths = sorted((r['value'] for r in rows if r['topic'] == TRUTH), key=lambda v: v['source']['simulation_time_s'])
    times = [v['source']['simulation_time_s'] for v in truths]
    errors = []
    for row in rows:
        if row['topic'] != SENSORS or not times:
            continue
        sample = row['value']
        t = sample['source']['simulation_time_s']
        index = bisect_left(times, t)
        candidates = [truths[i] for i in (index-1, index) if 0 <= i < len(truths) and truths[i]['source']['episode_id'] == sample['source']['episode_id']]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda v: abs(v['source']['simulation_time_s'] - t))
        if abs(nearest['source']['simulation_time_s'] - t) > MAX_ALIGNMENT_S:
            continue
        truth = nearest['ground_truth']
        baro, attitude = sample['sensors']['barometer'], sample['attitude_estimate']
        error = dict(simulation_time_s=t, altitude_error_m=None if baro is None else baro['altitude_m'] - truth['position_m'][2], attitude_error_deg=None)
        if attitude is not None:
            dot = abs(sum(a*b for a,b in zip(attitude['orientation_xyzw'], truth['orientation_xyzw'])))
            error['attitude_error_deg'] = degrees(2 * acos(min(1., dot)))
        errors.append(error)
    result = dict(recording=str(Path(path).resolve()), first_clipping=event, final_mission=last,
                  recorder_health=health, recording_complete=None if health is None else health['complete'] and not str(path).endswith('.partial'), topic_counts=channels, configurations=configs if event is None else event['configurations'],
                  frames=frames, sensor_errors=errors, window_s=WINDOW_NS / 1e9,
                  missing_analysis_topics=[t for t in [TRUTH, SENSORS, METADATA, 'simulation/wind_state', 'analysis/physics/config', 'analysis/app/config'] if not channels.get(t)],
                  telemetry=rows)
    (output / 'report.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    title = 'First image-edge rejection' if event else 'No image-edge rejection recorded; final flight window'
    sections = [f'<h1>{title}</h1>', '<p>Exact frame links use recorded identity, never nearest image timestamps. Sensor errors use truth within 20 ms.</p>']
    for label, value in [('Recording health', health), ('Missing analysis topics', result['missing_analysis_topics']), ('First rejection', event), ('Final mission', last), ('Configuration', configs)]:
        sections.append(f'<h2>{label}</h2><pre>{html.escape(json.dumps(value, indent=2))}</pre>')
    sections.append('<h2>Signals near rejection or final sample</h2>')
    for topic in [TRUTH, SENSORS, 'flight/drone_1/rc', 'flight/drone_1/feedback', 'simulation/wind_state', 'analysis/render/health']:
        candidates = [row for row in rows if row['topic'] == topic]
        if candidates:
            sample = min(candidates, key=lambda row: abs(row['log_time_ns'] - anchor))
            offset = (sample['log_time_ns'] - anchor) / 1e9
            sections.append(f'<h3>{html.escape(topic)} ({offset:+.3f} s arrival offset)</h3><pre>{html.escape(json.dumps(sample["value"], indent=2))}</pre>')
    sections.append('<h2>Sensor error samples</h2><pre>' + html.escape(json.dumps(errors[::max(1,len(errors)//10)],indent=2)) + '</pre>')
    for frame in frames:
        sections.append(f'<p>{"Exact rejected frame" if frame["exact_rejected_frame"] else "Nearby frame"}: sequence {frame["metadata"]["sequence"]}</p><img width="640" src="{frame["file"]}">')
    sections.append('<p>Detailed commands, wind, sensor errors and data ages are in report.json.</p>')
    (output / 'report.html').write_text('<!doctype html><meta charset="utf-8"><title>Flight analysis</title><style>body{font:16px sans-serif;max-width:1100px;margin:30px auto}pre{white-space:pre-wrap;background:#eee;padding:12px}img{max-width:100%}</style>' + ''.join(sections))
    if export_csv:
        flat_rows = [flatten(row) for row in rows]
        keys = sorted({key for row in flat_rows for key in row})
        with (output / 'telemetry.csv').open('w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=keys)
            writer.writeheader()
            writer.writerows(flat_rows)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--csv', action='store_true', help='export telemetry from the analysis window')
    args = parser.parse_args()
    result = make_report(args.recording, args.output, args.csv)
    print(f"Report: {args.output / 'report.html'}; clipping recorded: {result['first_clipping'] is not None}")


if __name__ == '__main__':
    main()
