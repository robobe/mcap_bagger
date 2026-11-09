"""Standalone opaque ZeroMQ and SHM recording."""

import signal
import sys
from importlib.metadata import version

import click

from .cli import CLI


def main(argv=None):
    try:
        args = CLI().parse(sys.argv[1:] if argv is None else argv)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code)
    if args.action == "version":
        print(version("mcap-bagger"))
        return 0
    if args.action == "run":
        from .registration import RegistrationConfig
        from .pipeline import Pipeline

        def terminate(signum, frame):
            raise KeyboardInterrupt

        previous = signal.signal(signal.SIGTERM, terminate)
        try:
            return Pipeline(RegistrationConfig.load(args.config)).run()
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)
            return 1
        finally:
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
