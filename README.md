[![CI](https://github.com/DiamondLightSource/dls-d2bpm-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/dls-d2bpm-tools/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/dls-d2bpm-tools/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/dls-d2bpm-tools)
[![PyPI](https://img.shields.io/pypi/v/dls-d2bpm-tools.svg)](https://pypi.org/project/dls-d2bpm-tools)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# dls-d2bpm-tools

Tools to facilitate development and maintenance of D2 BPM software.

Right now that means the **flash GUI**: a small Qt front end over the
`d2afe-cli.py` programming script. It lists the published firmware releases,
lets you pick a release, board address and network endpoint, shows you exactly
the command it is about to run, then runs it and tracks its progress.

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/dls-d2bpm-tools>
PyPI            | `pip install dls-d2bpm-tools`
Docker          | `docker run ghcr.io/diamondlightsource/dls-d2bpm-tools:latest`
Releases        | <https://github.com/DiamondLightSource/dls-d2bpm-tools/releases>

## Running the flash GUI

```
# from a checkout, without installing anything
uv run dls-d2afe-flash-gui

# or via the main CLI
uv run dls-d2bpm-tools flash-gui
```

Once the package is installed both `dls-d2afe-flash-gui` and
`dls-d2bpm-tools flash-gui` are on the `PATH`.

The GUI needs an X11 display. The devcontainer forwards `$DISPLAY` from the
host and the image carries the Qt runtime libraries, so it works there too.

## Where firmware comes from

Two sources, switchable in the GUI:

**GitLab releases** (default) reads
<https://gitlab.diamond.ac.uk/diagnostics/d2afe-firmware/-/releases> over the
API and downloads the assets it needs, caching them under
`~/.cache/dls-d2bpm-tools/firmware/<tag>/`. No token is needed on the DLS
network; set `GITLAB_TOKEN` if that ever changes. Use this from anywhere,
including machines with no `/dls_sw` mount.

**Filesystem** reads the CI build area at
`/dls_sw/work/ci-builds/d2afe-firmware`, the original behaviour. Pick the
starting source with `--source filesystem`, and point it elsewhere with
`--firmware-base`.

Both serve the same artifacts — the same CI job writes to `/dls_sw` and
attaches the release assets — so the choice is really about what the machine
can reach.

Downloads happen on the worker thread when you press Flash, not while you are
editing the form, so the GUI never blocks on the network. The command preview
shows where a file *will* be cached before it is fetched.

## How it picks a release

A release is only offered if it actually carries firmware for the selected
device: some tags ship one board only. The newest numbered release is
preselected, so a one-off tag like `hmc1119_1` never becomes the default.

Only the D2AFE build publishes `d2afe-cli.py`, but that script flashes both
boards, so a D2PTD flash borrows it from the D2AFE side of the same release.

Each of the binary and the script can be overridden with an explicit path if
you want to flash something from your own working copy. Anything that can't be
resolved — including GitLab being unreachable — is reported in the command
preview, with the Flash button disabled, rather than in a dialog.

> Releases created before the firmware CI was fixed to keep `*.py` in its build
> artifacts (0.9.2, hmc1119_1 and 0.9.3b) link to a `d2afe-cli.py` that isn't in
> the archive, and will say so. Use a script override or the filesystem source
> for those.

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
