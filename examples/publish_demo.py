"""Publish SHM numbered RGB images and matching JSON metadata at 30 FPS."""

import argparse
import json
import time

import zmq
from bullet_msgs import load_transport
from bullet_msgs.shm import FrameWriter
from PIL import Image, ImageDraw, ImageFont


def make_frame(number: int) -> bytes:
    image = Image.new("RGB", (640, 480), (24, 32, 48))
    draw = ImageDraw.Draw(image)
    draw.text(
        (320, 240),
        str(number),
        font=ImageFont.load_default(size=96),
        anchor="mm",
        fill="white",
    )
    return image.tobytes()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport-config", required=True, help="ZeroMQ TCP transport JSON profile"
    )
    args = parser.parse_args()
    profile = load_transport(args.transport_config)
    with FrameWriter(640, 480) as writer, zmq.Context() as context, context.socket(zmq.PUB) as images, context.socket(zmq.PUB) as metadata:
        for socket in (images, metadata):
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.SNDHWM, 8)
        images.bind(profile['image_endpoint'])
        metadata.bind(profile['telemetry_endpoint'])
        print("Publishing 640×480 RGB8 at 30 FPS; Ctrl+C to stop", flush=True)
        number = 0
        deadline = time.monotonic()
        try:
            while True:
                message = writer.write('camera/front/image', make_frame(number))
                if message is not None:
                    images.send(message, flags=zmq.DONTWAIT)
                metadata.send_multipart([b'camera/front/metadata', json.dumps({'number': number}).encode()], flags=zmq.DONTWAIT)
                number += 1
                deadline += 1 / 30
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    deadline = time.monotonic()
        except KeyboardInterrupt:
            print(f"Published {number} frames and metadata messages")


if __name__ == "__main__":
    main()
