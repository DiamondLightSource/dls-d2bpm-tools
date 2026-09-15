import subprocess
import sys

from dls_d2bpm_tools import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "dls_d2bpm_tools", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__
