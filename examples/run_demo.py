"""Publish configured synthetic streams and record them for ten seconds."""
import json
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

import click
import zmq
from loguru import logger
from PIL import Image, ImageDraw

from mcap_bagger.registration import RegistrationConfig
from mcap_bagger.shm import FramePublisher, topic_prefix


def make_frame(number):
    image = Image.new('RGB', (640, 480), (number % 256, 64, 128))
    ImageDraw.Draw(image).text((300, 220), str(number), fill='white')
    return image.tobytes()


def run_demo(config_path, duration=10, publish_only=False):
    config = RegistrationConfig.load(config_path)
    images = [c for c in config.channels if c.shm]
    ordinary = {c.topic: c for c in config.channels if not c.shm}
    if len(images) != 1 or set(ordinary) != {'camera_info', 'imu'} or len(config.channels) != 3:
        raise ValueError('demo requires one SHM image channel, camera_info, and imu')
    image = images[0]
    channels = {c.topic: c for c in config.channels}
    rates = {image.topic: 30, 'imu': 100, 'camera_info': 1}
    counts = dict.fromkeys(rates, 0)
    skipped = dict.fromkeys(rates, 0)
    before = set(config.output_dir.glob('*.mcap')) if not publish_only else set()
    publisher = FramePublisher()
    process = None
    try:
        with zmq.Context() as context:
            sockets = {}
            try:
                for channel in config.channels:
                    if channel.endpoint not in sockets:
                        socket = context.socket(zmq.XPUB)
                        sockets[channel.endpoint] = socket
                        socket.setsockopt(zmq.LINGER, 0)
                        socket.setsockopt(zmq.XPUB_VERBOSE, 1)
                        socket.bind(channel.endpoint)
                if not publish_only:
                    process = subprocess.Popen([sys.executable, '-m', 'mcap_bagger.main',
                                                'run', '--config', str(config_path)], stdin=subprocess.DEVNULL)
                    pending = {(c.endpoint, topic_prefix(c.topic) if c.shm else c.topic.encode()) for c in config.channels}
                    deadline = time.monotonic() + 5
                    while pending:
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError('recorder subscriptions did not become ready')
                        for endpoint, socket in sockets.items():
                            if socket.poll(10):
                                event = socket.recv()
                                if event[:1] == b'\x01':
                                    pending.discard((endpoint, event[1:]))
                logger.info('Publishing for {} seconds{}', duration, ' without launching a recorder' if publish_only else '')
                start = time.monotonic()
                next_tick = dict.fromkeys(rates, 0)
                while time.monotonic() - start < duration:
                    if process is not None and process.poll() is not None:
                        raise RuntimeError('recorder exited during publication')
                    elapsed = time.monotonic() - start
                    for topic, rate in rates.items():
                        if elapsed < next_tick[topic] / rate:
                            continue
                        tick = int(elapsed * rate)
                        skipped[topic] += max(0, tick - next_tick[topic])
                        next_tick[topic] = tick + 1
                        if topic == image.topic:
                            notification = publisher.write(topic, make_frame(tick))
                            if notification is None:
                                skipped[topic] += 1
                                continue
                            sockets[image.endpoint].send(notification)
                        else:
                            payload = dict(sequence=tick, timestamp_ns=time.time_ns())
                            if topic == 'imu':
                                payload.update(acceleration_m_s2=[0, 0, 9.81], angular_velocity_rad_s=[0, 0, .1])
                            else:
                                payload.update(width=640, height=480, encoding='rgb8', step=1920, frame_id='front_camera')
                            sockets[channels[topic].endpoint].send_multipart([topic.encode(), json.dumps(payload).encode()])
                        counts[topic] += 1
                    time.sleep(.001)
                if process is not None:
                    time.sleep(.2)  # Allow final notifications to arrive before stopping reception.
                    process.terminate()
                    if process.wait(timeout=10) != 0:
                        raise RuntimeError('recorder failed')
            finally:
                for socket in sockets.values():
                    socket.close()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        publisher.close()
    logger.info('Published {}, skipped {}', counts, skipped)
    if publish_only:
        return None
    created = set(config.output_dir.glob('*.mcap')) - before
    if len(created) != 1:
        raise RuntimeError('cannot uniquely identify the new recording')
    path = created.pop()
    return path


class CLI:
    def parse(self, argv):
        command = click.Command('run_demo', params=[
            click.Option(['--publish-only'], is_flag=True, help='Publish for ten seconds without launching a recorder.'),
            click.Option(['--config'], required=True, type=click.Path(exists=True, path_type=Path)),
            click.Option(['--version'], is_flag=True, is_eager=True, expose_value=False,
                         callback=self.show_version)])
        # Version command is also accepted to match the application CLI convention.
        if argv == ['version']:
            click.echo(version('mcap-bagger'))
            raise click.exceptions.Exit(0)
        with command.make_context('run_demo', argv) as context:
            return context.params

    def show_version(self, ctx, param, value):
        if value:
            click.echo(version('mcap-bagger'))
            ctx.exit()


def main(argv=None):
    try:
        args = CLI().parse(sys.argv[1:] if argv is None else argv)
        path = run_demo(args['config'], publish_only=args['publish_only'])
        if path is not None:
            click.echo(path)
        return 0
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except Exception as exc:
        logger.error('{}', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
