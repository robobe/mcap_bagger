# Repository Guidelines

## Project Structure & Module Organization

`src/mcap_bagger/` owns configuration, CLI and bounded recording workers.
`examples/` contains the numbered SHM-image demo and commented profiles; `tests/`
contains configuration, lifecycle, real TCP/SHM and MCAP readback checks.
Sibling `bullet_msgs` owns the shared transport contracts and SHM reader.

The application's main entry point and command selector must be `main.py`.
It passes `argv` to a helper class in the CLI module and receives structured
command data containing the action, subcommand, and parsed arguments. `main.py`
selects and runs the appropriate application operation from that data, delegating
application behavior to the appropriate modules. The CLI class uses Click only
to parse and validate arguments and provide help; it must not dispatch commands
or execute application operations.

## Plans

Save each finalized implementation plan as a Markdown file in `docs/plans/`,
creating the directory when needed. Use a descriptive filename and update the
saved plan when its scope or decisions change.

## Build, Test, and Development Commands

Use Python 3.12 and uv: `uv sync --locked --extra demo`,
`uv run --locked --extra demo pytest -q`, and `uv run --locked mcap-bagger`.
Do not install system tools automatically. Keep uv.lock aligned with pyproject.toml.

## Coding Style & Naming Conventions

No language, formatter, or linter has been selected. Follow the standard conventions of the language introduced, use consistent indentation within each file, and choose descriptive names. Add formatter or linter configuration alongside the first relevant source files. Keep changes focused on the requested behavior; avoid speculative abstractions and dependencies.

- Use Loguru for application logging.
- Use Click for CLI commands, options, and argument parsing.
- Every CLI application must implement a `version` command that prints its
  current application version and exits successfully without starting services.
  Read the version from package metadata rather than duplicating it in code.

## SOLID Design

Apply SOLID principles when adding or changing code:

- **Single responsibility:** Keep configuration validation, transport reception,
  recording lifecycle, and MCAP writing in focused components with clear ownership.
- **Open/closed:** Reuse existing extension points when adding behavior. Add new
  extension points only when a concrete requirement needs them.
- **Liskov substitution:** Any replacement implementation must preserve its
  callers' contracts, including validation, errors, ordering, and cleanup.
- **Interface segregation:** Keep interfaces small and focused on what their
  callers need. Avoid forcing unrelated operations into a shared interface.
- **Dependency inversion:** Keep recording policy separate from transport and
  storage details. Pass dependencies explicitly where substitution is needed;
  prefer simple callables or existing interfaces over new frameworks.

Use these principles to simplify changes. Do not add speculative abstractions,
class hierarchies, or dependencies solely to satisfy a design pattern.

## Testing Guidelines

No testing framework or coverage threshold is configured. Add automated tests with the first executable behavior and document how to run them. Name tests after the behavior they verify, following the chosen framework's conventions. Cover normal operation, invalid inputs, and failure handling where applicable. Keep fixtures small and reproducible.

## Commit & Pull Request Guidelines

This directory has no Git history from which to infer commit conventions. Use short, imperative commit subjects, such as `Add recording configuration`. Keep commits focused. Pull requests should explain the problem, describe the change, link relevant issues, and list validation performed. Explicitly identify checks that were not run.

## Data & Configuration

Keep credentials, machine-specific settings, generated output, and large recordings out of version control. Provide sanitized configuration examples and add appropriate ignore rules when introducing generated files.
