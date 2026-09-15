import pytest

from dls_d2bpm_tools.firmware import (
    TARGET,
    Device,
    build_command,
    latest_release,
)


def test_subdir_names() -> None:
    assert Device.D2AFE.subdir == "d2afe-firmware-f405"
    assert Device.D2PTD.subdir == "d2ptd-firmware-f405"


@pytest.mark.parametrize(
    ("releases", "expected"),
    [
        # Oldest first, so the last numbered release wins.
        (["0.8.5", "0.9.0", "0.9.3b"], "0.9.3b"),
        # A one-off tag must never become the default...
        (["0.9.2", "hmc1119_1", "0.9.3b"], "0.9.3b"),
        (["0.9.2", "0.9.3b", "hmc1119_1"], "0.9.3b"),
        # ...unless there is nothing numbered to prefer.
        (["hmc1119_1", "legacy"], "legacy"),
        ([], ""),
    ],
)
def test_latest_release(releases: list[str], expected: str) -> None:
    assert latest_release(releases) == expected


def test_build_command() -> None:
    assert build_command(
        "/fw/cli.py",
        "/fw/app.bin",
        d2afe_address="2",
        ip="172.23.241.15",
        port="7003",
    ) == [
        "/fw/cli.py",
        "--d2afe-address",
        "2",
        "--address",
        "172.23.241.15:7003",
        "program",
        "--target",
        TARGET,
        "--filepath",
        "/fw/app.bin",
        "--no-ver-check",
    ]


def test_build_command_via_ptg() -> None:
    cmd = build_command(
        "/fw/cli.py",
        "/fw/app.bin",
        d2afe_address="5",
        ip="1.2.3.4",
        port="7003",
        via_ptg=True,
    )
    assert cmd[1] == "--via-ptg"
    assert "--d2afe-address" in cmd


def test_build_command_through_an_interpreter() -> None:
    """A downloaded script has no executable bit, so it needs a python."""
    cmd = build_command(
        "/cache/cli.py",
        "/cache/app.bin",
        python="/usr/bin/python3",
        d2afe_address="2",
        ip="1.2.3.4",
        port="7003",
    )
    assert cmd[:2] == ["/usr/bin/python3", "/cache/cli.py"]
