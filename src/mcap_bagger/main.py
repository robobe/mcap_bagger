"""Standalone opaque ZeroMQ and SHM recording."""

import signal
import sys
from importlib.metadata import version
from pathlib import Path

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
    if args.action == "default-config":
        config = Path(__file__).with_name('config') / 'recorder.yaml'
        if not config.is_file():
            # Editable installs keep the example configuration outside the package.
            config = Path(__file__).resolve().parents[2] / 'config' / 'recorder.yaml'
        click.echo(config.read_text(), nl=False)
        return 0
    if args.action == "player":
        from .player import open_player
        from loguru import logger
        try:
            open_player()
            return 0
        except Exception as exc:
            logger.error("{}", exc)
            return 1
    if args.action == "server":
        import uvicorn
        from .server import create_app
        from .registration import RegistrationConfig
        uvicorn.run(create_app(RegistrationConfig.load(args.config)), host=args.host, port=args.port, workers=1)
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
