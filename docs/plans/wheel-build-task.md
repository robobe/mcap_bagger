# Wheel build task

Add a VS Code build task running `uv build --wheel --out-dir dist`, using the existing Hatchling
backend. Include the example `config/` directory under `mcap_bagger/config/` in
the wheel; keep the web HTML at its existing package-relative location.
Document the task, output location, and packaged resources in the README.

Build the wheel and compare all source HTML and example configuration files
against the archive contents. Run the existing test suite to check regressions.
