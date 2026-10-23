"""Manual command loop or simulation-controlled recorder service."""

import argparse
import shlex
import signal
import sys

from .control import ControlledRecorder
from .recorder import Recorder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="load YAML and wait for simulation runs")
    args = parser.parse_args()
    service = None
    recorder = Recorder(
        on_error=lambda message: print(message, file=sys.stderr, flush=True)
    )

    def terminate(signum, frame):
        raise KeyboardInterrupt

    previous_interrupt = signal.getsignal(signal.SIGINT)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        if args.config:
            print(recorder.init(args.config), flush=True)
            service = ControlledRecorder(recorder, report=lambda message: print(message, flush=True))
            print("Waiting for physics running state; Stop/Reset closes recording", flush=True)
            while True:
                service.poll()
        print("Commands: init <yaml>, start, close, quit")
        while True:
            try:
                parts = shlex.split(input("mcap-bagger> "))
                if not parts:
                    continue
                if parts[0] == "init" and len(parts) == 2:
                    print(recorder.init(parts[1]))
                elif parts == ["start"]:
                    print(recorder.start())
                elif parts == ["close"]:
                    print(recorder.close())
                elif parts == ["quit"]:
                    break
                else:
                    print("Usage: init <yaml> | start | close | quit")
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception as exc:
                print(f"Error: {exc}", file=sys.stderr, flush=True)
    except (EOFError, KeyboardInterrupt):
        print()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        # Ignore repeated shutdown signals while accepted data is drained.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if service is not None:
            service.close()
        else:
            print(recorder.close())
        signal.signal(signal.SIGTERM, previous)
        signal.signal(signal.SIGINT, previous_interrupt)
    return 1 if recorder.recording and recorder.recording.error else 0
