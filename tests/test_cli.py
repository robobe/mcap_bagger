import json
import os
import select
import signal
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

import pytest
import zmq
import yaml
from mcap.reader import make_reader


def test_default_config_redirects_to_file(tmp_path):
    expected = Path('config/recorder.yaml').read_bytes()
    output = tmp_path / 'recorder.yaml'
    with output.open('wb') as stream:
        result = subprocess.run(
            ['mcap-bagger', 'default-config'], cwd=tmp_path,
            stdout=stream, stderr=subprocess.PIPE, timeout=10,
        )
    assert result.returncode == 0
    assert result.stderr == b''
    assert output.read_bytes() == expected
    assert yaml.safe_load(output.read_text())['channels']


def test_default_config_rejects_config_option():
    from mcap_bagger.cli import CLI
    import click
    with pytest.raises(click.UsageError, match='does not accept --config'):
        CLI().parse(['default-config', '--config', 'other.yaml'])


def test_version_command_exits_without_starting_recording(tmp_path):
    result = subprocess.run(
        ["mcap-bagger", "version"], cwd=tmp_path,
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == version("mcap-bagger") + "\n"
    assert result.stderr == ""
    assert not list(tmp_path.iterdir())


def test_click_helper_returns_command_data():
    from mcap_bagger.cli import CLI
    command = CLI().parse(['run', '--config', 'profile.yaml'])
    assert command.action == command.subcommand == 'run'
    assert command.config == 'profile.yaml'


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
