from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loopcap",
        description="LoopCap, a Fedora GIF recorder inspired by LICEcap",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show the application version and exit",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="request the running app to stop recording without opening the window",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.version:
        from fedora_licecap import __version__

        print(__version__)
        return

    try:
        from fedora_licecap.app import run, send_control_command
    except Exception as exc:
        print("Unable to start the GTK prototype.")
        print(f"Startup error: {exc}")
        print("Create a local venv with system packages and try again:")
        print("  python3 -m venv --system-site-packages .venv")
        print("  . .venv/bin/activate")
        print("  python -m pip install -e . --no-build-isolation")
        print("  loopcap")
        print("If GTK bindings are missing on Fedora, install:")
        print("  sudo dnf install python3-gobject gtk4")
        sys.exit(1)

    if args.stop:
        ok, message = send_control_command("stop")
        print(message)
        sys.exit(0 if ok else 1)

    sys.exit(run())


if __name__ == "__main__":
    main()
