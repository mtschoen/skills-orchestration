#!/usr/bin/env python3
"""Command-line interface for cooperative project locks."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

from project_lock import (
    OWNER_PID_ENVIRONMENT_VARIABLES,
    SESSION_ID_ENVIRONMENT_VARIABLES,
    LockConflict,
    LockOwnershipError,
    acquire,
    inspect,
    list_locks,
    release,
    renew,
)

EXIT_LOCKED = 3


def parse_duration(value: str) -> timedelta:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    normalized = value.strip().lower()
    if not normalized:
        raise argparse.ArgumentTypeError("duration is required")
    suffix = normalized[-1]
    if suffix not in units:
        raise argparse.ArgumentTypeError("duration must end in s, m, h, or d")
    try:
        amount = float(normalized[:-1])
    except ValueError as error:
        raise argparse.ArgumentTypeError("duration must be numeric") from error
    if amount <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return timedelta(seconds=amount * units[suffix])


def print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def print_related(status: dict) -> None:
    related = status.get("related") or {}
    for entry in related.get("ancestors", []):
        print(f"  related: ancestor {entry['root']} held by {entry['lock']['owner']}")
    for entry in related.get("descendants", []):
        print(f"  related: nested {entry['root']} held by {entry['lock']['owner']}")


def print_status(status: dict) -> None:
    if not status["locked"]:
        print(f"FREE  {status['root']}")
        print_related(status)
        return
    metadata = status["lock"]
    overdue = " OVERDUE" if status["overdue"] else ""
    print(f"LOCKED{overdue}  {status['root']}")
    print(f"  owner:    {metadata['owner']}")
    print(f"  session:  {metadata.get('session') or '-'}")
    print(f"  reason:   {metadata['reason']}")
    print(f"  branch:   {metadata.get('branch') or '-'}")
    print(f"  expected: {metadata['expected_until']}")
    print(f"  lock id:  {metadata['lock_id']}")
    print(f"  owner pid: {metadata.get('owner_pid') or '-'} ({status.get('owner_process', '-')})")
    print(f"  advice:   {status['recommendation']}")
    print_related(status)


def command_acquire(arguments: argparse.Namespace) -> int:
    try:
        metadata = acquire(
            arguments.path,
            reason=arguments.reason,
            duration=arguments.duration,
            strategy=arguments.strategy,
            owner=arguments.owner,
            session=arguments.session,
            owner_pid=arguments.owner_pid,
        )
    except LockConflict as error:
        print(str(error), file=sys.stderr)
        print_status(error.status)
        return EXIT_LOCKED
    print_json(metadata) if arguments.json else print(
        f"LOCKED {metadata['root']}\nlock id: {metadata['lock_id']}\n"
        "Release this lock before leaving the project."
    )
    return 0


def command_check(arguments: argparse.Namespace) -> int:
    status = inspect(arguments.path, wait_threshold=arguments.wait_threshold)
    print_json(status) if arguments.json else print_status(status)
    return EXIT_LOCKED if status["locked"] else 0


def command_renew(arguments: argparse.Namespace) -> int:
    try:
        metadata = renew(arguments.path, lock_id=arguments.lock_id, duration=arguments.duration)
    except LockOwnershipError as error:
        print(str(error), file=sys.stderr)
        return EXIT_LOCKED
    print_json(metadata) if arguments.json else print(
        f"RENEWED {metadata['root']} until {metadata['expected_until']}"
    )
    return 0


def command_release(arguments: argparse.Namespace) -> int:
    try:
        released = release(
            arguments.path,
            lock_id=arguments.lock_id,
            force=arguments.force,
            expect_lock_id=arguments.expect_lock_id,
            reason=arguments.reason,
        )
    except LockOwnershipError as error:
        print(str(error), file=sys.stderr)
        return EXIT_LOCKED
    if released:
        print(f"RELEASED {arguments.path}")
    else:
        print(f"FREE {arguments.path}")
    return 0


def command_list(arguments: argparse.Namespace) -> int:
    locks = list_locks()
    if arguments.json:
        print_json(locks)
        return 0
    if not locks:
        print("No project locks.")
        return 0
    for status in locks:
        print_status(status)
        print()
    return 0


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def command_watch(arguments: argparse.Namespace) -> int:
    try:
        while True:
            clear_screen()
            print("PROJECT LOCKS  (Ctrl-C to exit)\n")
            command_list(argparse.Namespace(json=False))
            print(
                "\nOverride: project-lock.py release PATH --force"
                ' --expect-lock-id ID --reason "why"'
            )
            time.sleep(arguments.interval)
    except KeyboardInterrupt:
        return 0


def command_wait(arguments: argparse.Namespace) -> int:
    deadline = time.monotonic() + arguments.timeout.total_seconds()
    while True:
        status = inspect(arguments.path, wait_threshold=arguments.wait_threshold)
        if not status["locked"]:
            print(f"FREE {status['root']}")
            return 0
        if time.monotonic() >= deadline:
            print_status(status)
            return EXIT_LOCKED
        print(
            f"Waiting for {status['lock']['owner']}: {status['lock']['reason']}",
            file=sys.stderr,
        )
        time.sleep(arguments.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cooperative project locks for coding agents")
    subparsers = parser.add_subparsers(dest="command", required=True)

    acquire_parser = subparsers.add_parser("acquire", help="atomically lock a project")
    acquire_parser.add_argument("path", type=Path)
    acquire_parser.add_argument("--reason", required=True)
    acquire_parser.add_argument("--duration", required=True, type=parse_duration)
    acquire_parser.add_argument("--strategy", choices=("auto", "wait", "worktree"), default="auto")
    acquire_parser.add_argument("--owner")
    acquire_parser.add_argument(
        "--session",
        help=(
            "session id recorded on the lock, so a lock can be told apart from "
            "another session of the same owner. Defaults to the current "
            "session id read from a recognized harness environment variable "
            f"({', '.join(SESSION_ID_ENVIRONMENT_VARIABLES)}) when omitted."
        ),
    )
    acquire_parser.add_argument(
        "--owner-pid",
        type=int,
        help=(
            "pid of a durable process whose death means this lock is abandoned "
            "(e.g. the agent session). Defaults to a recognized harness "
            f"environment variable ({', '.join(OWNER_PID_ENVIRONMENT_VARIABLES)}) "
            "when omitted. Pass explicitly only for a process that outlives this "
            "command: this command's own pid dies immediately and would read as "
            "abandoned."
        ),
    )
    acquire_parser.add_argument("--json", action="store_true")
    acquire_parser.set_defaults(function=command_acquire)

    check_parser = subparsers.add_parser("check", help="inspect a project's lock")
    check_parser.add_argument("path", type=Path)
    check_parser.add_argument("--wait-threshold", type=parse_duration, default=parse_duration("5m"))
    check_parser.add_argument("--json", action="store_true")
    check_parser.set_defaults(function=command_check)

    renew_parser = subparsers.add_parser("renew", help="extend a lock estimate")
    renew_parser.add_argument("path", type=Path)
    renew_parser.add_argument("--lock-id", required=True)
    renew_parser.add_argument("--duration", required=True, type=parse_duration)
    renew_parser.add_argument("--json", action="store_true")
    renew_parser.set_defaults(function=command_renew)

    release_parser = subparsers.add_parser("release", help="release a lock")
    release_parser.add_argument("path", type=Path)
    release_parser.add_argument("--lock-id")
    release_parser.add_argument("--force", action="store_true")
    release_parser.add_argument(
        "--expect-lock-id",
        help="lock id verified as abandoned; required with --force on a readable lock",
    )
    release_parser.add_argument("--reason", help="audit reason; required with --force")
    release_parser.set_defaults(function=command_release)

    list_parser = subparsers.add_parser("list", help="list all registered locks")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(function=command_list)

    watch_parser = subparsers.add_parser("watch", help="refreshing terminal dashboard")
    watch_parser.add_argument("--interval", type=float, default=2.0)
    watch_parser.set_defaults(function=command_watch)

    wait_parser = subparsers.add_parser("wait", help="wait until a project is free")
    wait_parser.add_argument("path", type=Path)
    wait_parser.add_argument("--timeout", type=parse_duration, default=parse_duration("5m"))
    wait_parser.add_argument("--wait-threshold", type=parse_duration, default=parse_duration("5m"))
    wait_parser.add_argument("--interval", type=float, default=5.0)
    wait_parser.set_defaults(function=command_wait)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    return int(arguments.function(arguments))
