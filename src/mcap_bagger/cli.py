"""Click argument parsing, independent of application execution."""

from dataclasses import dataclass

import click


@dataclass(frozen=True)
class CommandData:
    action: str
    subcommand: str | None
    config: str | None


class CLI:
    """Parse argv into command data without running application operations."""

    def parse(self, argv):
        command = click.Command(
            "mcap-bagger",
            help="Record opaque ZeroMQ and SHM channels.",
            params=[
                click.Argument(["subcommand"], type=click.Choice(["version", "run"]), required=True),
                click.Option(["--config"], help="Load channel declarations from YAML."),
            ],
        )
        with command.make_context("mcap-bagger", list(argv)) as context:
            values = context.params
            if values["subcommand"] == "run" and not values["config"]:
                raise click.UsageError("run requires --config", context)
            action = values["subcommand"]
            return CommandData(action=action, **values)
