# Repository Guidelines

## Project Structure & Module Organization

`src/mcap_bagger/` owns configuration, CLI and bounded recording workers.
`examples/` contains the numbered SHM-image demo and commented profiles; `tests/`
contains configuration, lifecycle, real TCP/SHM and MCAP readback checks.
Sibling `bullet_msgs` owns the shared transport contracts and SHM reader.

## Build, Test, and Development Commands

Use Python 3.12 and uv: `uv sync --locked --extra demo`,
`uv run --locked --extra demo pytest -q`, and `uv run --locked mcap-bagger`.
Do not install system tools automatically. Keep uv.lock aligned with pyproject.toml.

## Coding Style & Naming Conventions

No language, formatter, or linter has been selected. Follow the standard conventions of the language introduced, use consistent indentation within each file, and choose descriptive names. Add formatter or linter configuration alongside the first relevant source files. Keep changes focused on the requested behavior; avoid speculative abstractions and dependencies.

## Testing Guidelines

No testing framework or coverage threshold is configured. Add automated tests with the first executable behavior and document how to run them. Name tests after the behavior they verify, following the chosen framework's conventions. Cover normal operation, invalid inputs, and failure handling where applicable. Keep fixtures small and reproducible.

## Commit & Pull Request Guidelines

This directory has no Git history from which to infer commit conventions. Use short, imperative commit subjects, such as `Add recording configuration`. Keep commits focused. Pull requests should explain the problem, describe the change, link relevant issues, and list validation performed. Explicitly identify checks that were not run.

## Data & Configuration

Keep credentials, machine-specific settings, generated output, and large recordings out of version control. Provide sanitized configuration examples and add appropriate ignore rules when introducing generated files.
