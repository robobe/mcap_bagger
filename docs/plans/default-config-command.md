# Default config command

Add `default-config` to CLI parsing and dispatch it from `main.py`. Print the
packaged example YAML unchanged to stdout without starting services, supporting
shell redirection. For editable installs, read the repository example config.
Reject `--config` for this command. Document redirection and relative output paths.

Test stdout redirected to a file from another working directory, invalid options,
and execution from the built wheel to verify packaged config access.
