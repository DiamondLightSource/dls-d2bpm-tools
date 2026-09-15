"""Interface for ``python -m dls_d2bpm_tools``."""

from argparse import ArgumentParser
from collections.abc import Sequence

from . import __version__

__all__ = ["main"]


def main(args: Sequence[str] | None = None) -> int:
    """Argument parser for the CLI."""
    parser = ArgumentParser(description="Tools for the Diamond-II BPM system")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "flash-gui",
        help="Launch the D2AFE/D2PTD flashing GUI",
        add_help=False,  # -h after the subcommand belongs to the GUI parser
    )

    # Anything after the subcommand is the subcommand's to parse, so keep it
    # rather than rejecting flags this parser has never heard of.
    parsed, rest = parser.parse_known_args(args)

    if parsed.command == "flash-gui":
        from .flash_gui import main as flash_gui_main

        return flash_gui_main(rest)

    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
