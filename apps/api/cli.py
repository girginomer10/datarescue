from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.api.config import Settings
from apps.api.store import EventStore

CONTAINED_EXIT_CODE = 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="datarescue")
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    guard = subcommands.add_parser(
        "guard", help="Block a command while an asset has an unresolved drift case"
    )
    guard.add_argument("--asset", required=True, help="Exact DataHub asset URN")
    guard.add_argument("--database-path", type=Path, help="Override the event-store path")
    guard.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand != "guard":  # pragma: no cover - argparse enforces this
        return 2
    settings = Settings(database_path=args.database_path) if args.database_path else Settings()
    store = EventStore(settings.resolved_database_path)
    blocked = store.blocked_asset(args.asset)
    if blocked:
        print(
            f"DataRescue blocked downstream execution: {args.asset} is unsafe "
            f"in {blocked.state.value} case {blocked.id}",
            file=sys.stderr,
        )
        return CONTAINED_EXIT_CODE
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("datarescue guard requires a command after --", file=sys.stderr)
        return 2
    try:
        completed = subprocess.run(command, check=False)
    except OSError as error:
        print(f"Unable to start guarded command: {error}", file=sys.stderr)
        return 127
    return completed.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
