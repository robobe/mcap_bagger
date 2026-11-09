import json
import os
import select
import signal
import subprocess
import sys
import time
from importlib.metadata import version

import pytest
import zmq
from mcap.reader import make_reader


def test_version_command_exits_without_starting_recording(tmp_path):
    result = subprocess.run(
        ["mcap-bagger", "version"], cwd=tmp_path,
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == version("mcap-bagger") + "\n"
    assert result.stderr == ""
    assert not list(tmp_path.iterdir())


def test_cli_eof_and_commands(config_files):
    path, _ = config_files
    result = subprocess.run(
        ["mcap-bagger"],
        input=f'init "{path}"\nstart\nclose\n',
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Closed successfully" in result.stdout
    assert len(list((path.parent / "recordings").glob("*.mcap"))) == 1
    assert not list((path.parent / "recordings").glob("*.partial"))


def test_sigterm_finishes_recording(config_files):
    path, _ = config_files
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-c",
            "from mcap_bagger.main import main; raise SystemExit(main())",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        process.stdin.write(f'init "{path}"\nstart\n'.encode())
        process.stdin.flush()
        output = b""
        deadline = time.monotonic() + 10
        while b"Recording:" not in output:
            assert time.monotonic() < deadline
            if select.select([process.stdout], [], [], 0.1)[0]:
                chunk = os.read(process.stdout.fileno(), 4096)
                assert chunk, output
                output += chunk
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert b"Closed successfully" in stdout
        files = list((path.parent / "recordings").glob("*.mcap"))
        assert len(files) == 1
        with files[0].open("rb") as stream:
            assert make_reader(stream).get_summary() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize("shutdown", [signal.SIGINT, signal.SIGTERM])
def test_controlled_cli_without_stdin_finalizes(config_files, shutdown):
    path, _ = config_files
    transport = json.loads((path.parent / "transport.json").read_text())
    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    publisher.bind(transport["telemetry_endpoint"])
    process = subprocess.Popen(
        ["mcap-bagger", "--config", str(path)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        output = b""
        deadline = time.monotonic() + 10
        while b"Waiting for physics" not in output:
            assert time.monotonic() < deadline
            assert process.poll() is None
            if select.select([process.stdout], [], [], 0.1)[0]:
                output += os.read(process.stdout.fileno(), 4096)
        assert not list((path.parent / "recordings").glob("*"))
        value = dict(schema_version=1, physics_id="physics", controller_id=None,
                     episode_id="run", state="running", reason="started", simulation_time_s=0)
        while not list((path.parent / "recordings").glob("*.partial")):
            assert time.monotonic() < deadline
            publisher.send_multipart([b"simulation/state", json.dumps(value).encode()])
            time.sleep(0.02)
        process.send_signal(shutdown)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert b"Closed successfully" in stdout
        files = list((path.parent / "recordings").glob("*.mcap"))
        assert len(files) == 1
        assert not list((path.parent / "recordings").glob("*.partial"))
        with files[0].open("rb") as stream:
            assert make_reader(stream).get_summary() is not None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        publisher.close(0)
        context.term()


def test_click_helper_returns_command_data():
    from pathlib import Path
    from mcap_bagger.cli import CLI

    cli = CLI()
    assert cli.parse([]).action == 'interactive'
    command = cli.parse(['--config', 'profile.yaml', '--ready-file', 'ready',
                         '--output-dir', 'recordings'])
    assert command.action == 'record'
    assert command.subcommand is None
    assert command.config == 'profile.yaml'
    assert command.ready_file == Path('ready')
    assert command.output_dir == 'recordings'
    command = cli.parse(['version'])
    assert command.action == command.subcommand == 'version'


@pytest.mark.parametrize('arguments,code', [(['--help'], 0), (['unknown'], 2),
                                          (['--config'], 2), (['--unknown'], 2)])
def test_click_help_and_invalid_arguments(arguments, code):
    result = subprocess.run(['mcap-bagger', *arguments], text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == code
    if code == 0:
        assert 'Usage:' in result.stdout
    else:
        assert 'Error:' in result.stderr
    assert 'mcap-bagger>' not in result.stdout
