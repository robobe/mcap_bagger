"""Click argument parsing, independent of application execution."""

from dataclasses import dataclass
from pathlib import Path

import click


@dataclass(frozen=True)
class CommandData:
    action: str
    subcommand: str | None
    config: str | None
    ready_file: Path | None
    output_dir: str | None


class CLI:
    """Parse argv into command data without running application operations."""

    def parse(self, argv):
        command = click.Command(
            "mcap-bagger",
            help="Manual command loop or simulation-controlled recorder service.",
            params=[
                click.Argument(["subcommand"], type=click.Choice(["version"]), required=False),
                click.Option(["--config"], help="Load YAML and wait for simulation runs."),
                click.Option(["--ready-file"], type=click.Path(path_type=Path),
                             help="Write readiness marker after control subscriptions are installed."),
                click.Option(["--output-dir"],
                             help="MCAP directory override; requires --config, relative to the working directory."),
            ],
        )
        with command.make_context("mcap-bagger", list(argv)) as context:
            values = context.params
            if values["subcommand"] != "version" and values["output_dir"] is not None and not values["config"]:
                raise click.UsageError("--output-dir requires --config", context)
            if values["subcommand"] == "version":
                action = "version"
            elif values["config"]:
                action = "record"
            else:
                action = "interactive"
            return CommandData(action=action, **values)
