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
