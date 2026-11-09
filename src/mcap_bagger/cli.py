"""Click argument parsing, independent of application execution."""

from dataclasses import dataclass

import click


@dataclass(frozen=True)
class CommandData:
    action: str
    subcommand: str | None
    config: str | None
    host: str = "0.0.0.0"
    port: int = 9090


class CLI:
    """Parse argv into command data without running application operations."""

    def parse(self, argv):
        command = click.Command(
            "mcap-bagger",
            help="Record opaque ZeroMQ and SHM channels.",
            params=[
                click.Argument(["subcommand"], type=click.Choice(["version", "run", "server", "player"]), required=True),
                click.Option(["--host"], default="0.0.0.0"),
                click.Option(["--port"], default=9090, type=click.IntRange(1, 65535)),
                click.Option(["--config"], help="Load channel declarations from YAML."),
            ],
        )
        with command.make_context("mcap-bagger", list(argv)) as context:
            values = context.params
            if values["subcommand"] in ("run", "server") and not values["config"]:
                raise click.UsageError(f"{values['subcommand']} requires --config", context)
            if values["subcommand"] != "server" and any(context.get_parameter_source(key) == click.core.ParameterSource.COMMANDLINE for key in ("host", "port")):
                raise click.UsageError("--host and --port require server", context)
            if values["subcommand"] == "player" and values["config"] is not None:
                raise click.UsageError("player uses the Load button, not --config", context)
            action = values["subcommand"]
            return CommandData(action=action, **values)
