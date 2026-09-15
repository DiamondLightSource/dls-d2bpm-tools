"""The domain model behind the D2 firmware programmer.

Nothing here touches Qt or the network: a `Device`, the `Artifact` a source
hands back, and the command line that flashes a board.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

__all__ = [
    "PROGRESS_RE",
    "TARGET",
    "Artifact",
    "Device",
    "FirmwareSource",
    "LogFn",
    "build_command",
    "latest_release",
]

#: The only MCU target currently supported.
TARGET = "f405"

#: Progress lines emitted by ``d2afe-cli.py`` while sending an image.
PROGRESS_RE = re.compile(r"Sent (\d+) bytes of (\d+) bytes")

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")

#: Somewhere to report slow work, so it can be shown in the GUI log.
LogFn = Callable[[str], None]


class Device(Enum):
    """A board that can be flashed."""

    D2AFE = "D2AFE"
    D2PTD = "D2PTD"

    @property
    def subdir(self) -> str:
        """The per-device directory name, used by both firmware sources.

        It names the directory inside a release on disk, and — not by
        coincidence, since the same CI job produces both — the directory
        inside the GitLab job artifact archive.
        """
        return f"{self.value.lower()}-firmware-{TARGET}"


#: Only D2AFE publishes ``d2afe-cli.py``, but the script drives both boards,
#: so a D2PTD flash borrows it from the D2AFE side of the release.
SCRIPT_DEVICE = Device.D2AFE


@dataclass(frozen=True)
class Artifact:
    """A firmware file a source can provide, and where it lands locally.

    `path` is known before any download happens, so the GUI can show the
    command it is about to run without touching the network. `ensure` is what
    actually makes the bytes appear, and may be slow.
    """

    name: str
    path: Path
    ensure: Callable[[LogFn | None], Path]

    def obtain(self, log: LogFn | None = None) -> Path:
        """Make sure the file exists locally and return its path."""
        return self.ensure(log)


class FirmwareSource(Protocol):
    """Somewhere firmware releases can be listed and fetched from."""

    @property
    def description(self) -> str:
        """One line naming where this source reads from, for the UI."""
        ...

    def list_releases(self, device: Device) -> list[str]:
        """Releases that provide firmware for `device`, oldest first."""
        ...

    def artifact(self, release: str, device: Device, suffix: str) -> Artifact:
        """The single ``*{suffix}`` file `release` provides for `device`.

        Raises:
            FileNotFoundError: if there is no such file, or more than one, so
                that the choice would be ambiguous.
        """
        ...


def latest_release(releases: Iterable[str]) -> str:
    """Pick the release to preselect from a list ordered oldest first.

    Prefers the newest numbered release, so that a one-off tag like
    ``hmc1119_1`` never becomes the default. Falls back to the newest release
    of any name, or ``""`` if there are none.
    """
    ordered = list(releases)
    versioned = [r for r in ordered if _VERSION_RE.match(r)]
    if versioned:
        return versioned[-1]
    return ordered[-1] if ordered else ""


def build_command(
    script: Path | str,
    binary: Path | str,
    *,
    python: Path | str | None = None,
    d2afe_address: str,
    ip: str,
    port: str,
    via_ptg: bool = False,
) -> list[str]:
    """Assemble the ``d2afe-cli.py`` command line that flashes a board.

    `python` runs the script through an interpreter rather than relying on its
    executable bit, which a freshly downloaded file will not have.
    """
    cmd = [str(python), str(script)] if python else [str(script)]
    if via_ptg:
        cmd.append("--via-ptg")
    cmd += [
        "--d2afe-address",
        d2afe_address,
        "--address",
        f"{ip}:{port}",
        "program",
        "--target",
        TARGET,
        "--filepath",
        str(binary),
        "--no-ver-check",
    ]
    return cmd


def format_command(cmd: Sequence[str]) -> str:
    """Render a command for display."""
    return " ".join(cmd)
