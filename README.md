[![CI](https://github.com/DiamondLightSource/dls-d2bpm-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-d2bpm-tools/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-d2bpm-tools/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-d2bpm-tools)
[![PyPI](https://img.shields.io/pypi/v/dls-d2bpm-tools.svg)](https://pypi.org/project/dls-d2bpm-tools)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls-d2bpm-tools

Tools to facilitate development and maintenance of D2 BPM software.

Right now that means the **flash GUI**: a small Qt front end over the
`d2afe-cli.py` programming script. It lists the published firmware releases,
lets you pick a release, board address and network endpoint, shows you exactly
the command it is about to run, then runs it and tracks its progress. A second
tab opens a **console** on the same endpoint, for talking to a board directly.

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/dls-d2bpm-tools>
PyPI            | `pip install dls-d2bpm-tools`
Docker          | `docker run ghcr.io/diamondlightsource/dls-d2bpm-tools:latest`
Releases        | <https://github.com/DiamondLightSource/dls-d2bpm-tools/releases>

## Running the flash GUI

```
# from a checkout, without installing anything
uv run dls-d2bpm-tools d2afe-flash

# from PyPI, in an automatically managed Python environment
uvx dls-d2bpm-tools d2afe-flash
```

Once the package is installed `dls-d2bpm-tools` is on the `PATH`, and the GUI
is its `d2afe-flash` subcommand.

`uvx` installs PySide6 and its bundled Qt libraries automatically; a separate
Qt or Python package installation is not needed. On Linux, the host still needs
an X11 display and Qt's system libraries (OpenGL, GLib, XCB, fontconfig and D-Bus).
These are normally present on a desktop, but minimal installations may need
them installed once. On Debian/Ubuntu:

```sh
sudo apt-get install -y libegl1 libgl1 libglib2.0-0t64 libxkbcommon-x11-0 libdbus-1-3 \
    libfontconfig1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-xkb1
```

Other distributions use different package names; see
[Qt's Linux requirements](https://doc.qt.io/qt-6.8/linux-requirements.html).
Older Debian/Ubuntu versions call the GLib package `libglib2.0-0`.
These OS libraries cannot be installed by `uvx` or declared as Python dependencies.
Both container images include them. The devcontainer also forwards `$DISPLAY`
from the host; running the release container's GUI requires forwarding the display
and its authentication, for example on a Linux X11 host:

```sh
docker run --rm --network host -e DISPLAY -e XAUTHORITY=/tmp/Xauthority \
    -v /tmp/.X11-unix:/tmp/.X11-unix:ro \
    -v "${XAUTHORITY:-$HOME/.Xauthority}:/tmp/Xauthority:ro" \
    ghcr.io/diamondlightsource/dls-d2bpm-tools:latest d2afe-flash
```

## Where firmware comes from

Two sources, switchable in the GUI:

**GitLab releases** (default) reads
<https://gitlab.diamond.ac.uk/diagnostics/d2afe-firmware/-/releases> over the
API and downloads the assets it needs, caching them under
`~/.cache/dls-d2bpm-tools/firmware/<repository-id>/<tag>/`. No token is needed on the DLS
network; set `GITLAB_TOKEN` if that ever changes. Use this from anywhere,
including machines with no `/dls_sw` mount.

HTTPS trusts the operating system's certificate store (via `truststore`),
whichever interpreter runs the tool. This matters under `uvx`: a uv-managed
Python looks for a CA bundle in `/etc/ssl`, which does not exist on RHEL 8,
leaving it with no trust store at all, and without this every release lookup
would fail as if GitLab were unreachable.

To use another GitLab project or server, override the full project URL:

```sh
uvx dls-d2bpm-tools d2afe-flash \
    --firmware-repo https://gitlab.example.org/team/d2afe-firmware
```

You can also edit **Repository URL** in the GUI and press **Apply** or Enter.
The field starts with the command-line URL (or the default). Applying it selects
the GitLab source and loads releases in the background, with the same filesystem
fallback on failure. Invalid URLs are reported beside the field without replacing
the current source. Changes apply to this session only.

Nested namespaces, a trailing `.git`, and links ending in `/-/releases` are
accepted. The project must publish the same release asset layout as the default
firmware repository. Authentication still uses `GITLAB_TOKEN`, and filesystem
fallback still uses `--firmware-base`. Each repository has its own cache so
identically named releases cannot reuse another repository's firmware. Older
downloads in the previous cache layout will be downloaded again once.

**Filesystem** reads the CI build area at
`/dls_sw/work/ci-builds/d2afe-firmware`, the original behaviour. Pick the
starting source with `--source filesystem`, and point it elsewhere with
`--firmware-base`.

Both serve the same artifacts — the same CI job writes to `/dls_sw` and
attaches the release assets — so the choice is really about what the machine
can reach.

Release discovery runs in the background. If GitLab cannot be reached or its
release list cannot be read, the GUI automatically switches to the filesystem
source and explains why. If that source has no releases, Flash stays disabled
until files are available or both artifact paths are overridden. Select GitLab
again to retry it.

Downloads happen on the worker thread when you press Flash. The command preview
shows where a file *will* be cached before it is fetched. Flash captures the
selected files and connection settings immediately; editing the form during a
download cannot change the operation already in progress.

## How it picks a release

A release is only offered if it actually carries firmware for the selected
device: some tags ship one board only. The newest numbered release is
preselected, so a one-off tag like `hmc1119_1` never becomes the default.

Only the D2AFE build publishes `d2afe-cli.py`, but that script flashes both
boards, so a D2PTD flash borrows it from the D2AFE side of the same release.

Each of the binary and the script can be overridden with an explicit path if
you want to flash something from your own working copy. Anything that can't be
resolved after source fallback is reported in the command
preview, with the Flash button disabled, rather than in a dialog.

> Releases created before the firmware CI was fixed to keep `*.py` in its build
> artifacts — 0.9.2 and earlier, and hmc1119_1 — link to a `d2afe-cli.py` that
> isn't in the archive, and will say so. Use a script override or the
> filesystem source for those. Releases from 0.9.3-beta.1 onwards carry the
> script.

## The console tab

The Console tab opens a session on the same IP and port used for flashing, so
you can talk to a board between flashes without leaving the app or unplugging
anything.

It is a **raw TCP** connection, not telnet, and that distinction matters. The
endpoint is a serial device server in TCP server mode, where the socket is a
pipe onto the RS485 bus. A telnet client opens by sending IAC negotiation
bytes, which every board on the bus would see as noise, and would in turn eat
parts of their replies as if they were negotiation. If you want to reach the
same port from a shell, use `nc <ip> <port>`, not `telnet`.

Two consequences of the bus being shared, both surfaced in the tab:

- **Everything is a broadcast.** What you send reaches every board on that
  port, and their replies come back interleaved. The board address used for
  flashing means nothing here.
- **Only one thing can hold the port.** Pressing Flash disconnects the console
  first and says so, and the Connect button is disabled while a flash runs.

Lines are terminated with CRLF by default, since that is what the D2AFE console
expects; CR and LF are selectable.

**Hex view** shows what arrives as bytes — `4F 4B 0D 0A` rather than `OK` — so
terminators, padding and anything unprintable are visible. It applies to what
arrives next, not what is already on screen, and the switch is marked in the
log so a hex dump is not mistaken for the board talking nonsense.

**Send hex** reads what you type as hex bytes and sends them exactly: `02 41 03`
sends three bytes and appends *nothing*, so the line ending selector is
disabled while it is on — if you want a terminator, type it (`0D 0A`). Hex can
be written however you have it to hand: `0d0a`, `0D 0A`, `0x0d,0x0a` and
`\x0d\x0a` all mean the same two bytes. Anything that isn't hex is reported in
the log and left in the input to correct. Local echo is on by default,
because half-duplex RS485 will not echo your keystrokes back to you.

## Development

```
uv sync                             # create the venv
uv run pytest                       # tests (the GUI ones run offscreen)
uv run ruff check src tests         # lint
uv run pyright src tests            # types
tox -p                              # everything CI runs
```

`dls_d2bpm_tools.firmware` holds the domain model (devices, artifacts, the
command line) and `dls_d2bpm_tools.sources` the two firmware sources. Neither
imports Qt, and the tests stub the one HTTP call, so the suite needs no display
and no network.
