from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .config import load_config, set_config_value, state_db_path
from .delivery import approve_event
from .github import current_user, daemon_loop, poll_once
from .sessions import discover_sessions
from .state import StateStore
from .workflow import create_explicit_binding


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == "bind":
            store = StateStore(state_db_path(args.state_dir))
            binding = create_explicit_binding(
                store,
                args.pr,
                role=args.role,
                agent=args.agent,
                session_id=args.session_id,
                cwd=args.cwd or str(Path.cwd()),
                branch=args.branch or current_branch(),
                host=args.host,
                repo=args.repo,
            )
            print(f"bound {binding.pr_url} to {binding.agent}:{binding.session_id} ({binding.role})")
            return 0
        if args.command == "daemon":
            store = StateStore(state_db_path(args.state_dir))
            user = args.user or current_user()
            sessions = discover_sessions()
            if args.once:
                items = poll_once(store, user, repo=args.repo, fixture=args.fixture, sessions=sessions)
                print(f"recorded {len(items)} actionable event(s)")
                return 0
            config = load_config(args.state_dir)
            interval = int(args.interval or config.get("poll_interval_seconds", "120"))
            daemon_loop(store, user, repo=args.repo, fixture=args.fixture, sessions=sessions, interval_seconds=interval)
            return 0
        if args.command == "inbox":
            store = StateStore(state_db_path(args.state_dir))
            print_inbox(store, include_done=args.all)
            return 0
        if args.command == "approve":
            store = StateStore(state_db_path(args.state_dir))
            config = load_config(args.state_dir)
            result = approve_event(
                store,
                args.event_id,
                session_state=args.session_state,
                busy_policy=args.busy_policy or config.get("busy_policy", "run_if_idle_queue_if_busy"),
            )
            print(f"{result.action}: {args.event_id}")
            if result.command:
                print("command:", " ".join(result.command[:-1]), "<prompt>")
            if result.message:
                print(result.message)
            return 0 if result.action != "failed" else 2
        if args.command == "queue":
            store = StateStore(state_db_path(args.state_dir))
            for item in store.list_queue():
                print(f"{item.queue_id}\t{item.event_id}\t{item.status}\t{' '.join(item.command[:-1])} <prompt>")
            return 0
        if args.command == "config":
            path = set_config_value(args.key, args.value, args.state_dir)
            print(f"updated {path}")
            return 0
        if args.command == "doctor":
            return doctor(args.state_dir)
    except Exception as exc:  # pragma: no cover - exercised by CLI users
        print(f"pr-watch: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pr-watch")
    parser.add_argument("--state-dir", help="state directory, defaults to ~/.pr-watch")
    subparsers = parser.add_subparsers(dest="command")

    daemon = subparsers.add_parser("daemon", help="poll GitHub and update local inbox")
    daemon.add_argument("--once", action="store_true", help="poll once and exit")
    daemon.add_argument("--repo", help="repository as owner/name")
    daemon.add_argument("--fixture", help="JSON fixture for local replay/testing")
    daemon.add_argument("--user", help="current GitHub login; defaults to gh api user")
    daemon.add_argument("--interval", type=int, help="poll interval in seconds")

    inbox = subparsers.add_parser("inbox", help="show pending PR events")
    inbox.add_argument("--all", action="store_true", help="include delivered and dismissed items")

    bind = subparsers.add_parser("bind", help="confirm a PR/session binding")
    bind.add_argument("pr", help="GitHub PR URL, or #123 with --repo owner/name")
    bind.add_argument("--repo", help="repository for #123 references")
    bind.add_argument("--role", choices=["author", "reviewer", "requested_reviewer", "commenter"], required=True)
    bind.add_argument("--agent", choices=["claude", "codex"], required=True)
    bind.add_argument("--session-id", required=True)
    bind.add_argument("--cwd", default="")
    bind.add_argument("--branch", default="")
    bind.add_argument("--host")

    approve = subparsers.add_parser("approve", help="approve delivery for an inbox event")
    approve.add_argument("event_id")
    approve.add_argument("--session-state", choices=["idle", "working", "unknown"], default="unknown")
    approve.add_argument(
        "--busy-policy",
        choices=["run_if_idle_queue_if_busy", "always_queue", "notify_only", "drop_if_busy", "ask_when_busy"],
    )

    subparsers.add_parser("queue", help="show queued resume commands")

    config = subparsers.add_parser("config", help="configure pr-watch")
    config_sub = config.add_subparsers(dest="config_command")
    config_set = config_sub.add_parser("set", help="set a config value")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config.set_defaults(command="config")

    subparsers.add_parser("doctor", help="check local dependencies and state")
    return parser


def print_inbox(store: StateStore, include_done: bool = False) -> None:
    items = store.list_events(include_done=include_done)
    if not items:
        print("Inbox is empty.")
        return
    for item in items:
        binding = item.binding_id or "-"
        print(
            f"{item.event_id}\t{item.status}\t{item.delivery_status}\t"
            f"{item.repo_owner}/{item.repo_name}#{item.pr_number}\t{item.role}\t"
            f"{item.event_type}\t{item.confidence}\tbinding={binding}\t{item.summary}"
        )
        if item.evidence:
            print("  evidence:", "; ".join(item.evidence))
        if item.recovery_command:
            print("  retry:", item.recovery_command)
        if item.error:
            print("  error:", item.error)


def current_branch() -> str:
    result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def doctor(state_dir: Optional[str]) -> int:
    config = load_config(state_dir)
    print(f"state: {state_db_path(state_dir)}")
    print(f"busy_policy: {config.get('busy_policy')}")
    for executable in ["gh", "claude", "codex"]:
        path = shutil.which(executable)
        print(f"{executable}: {path or 'not found'}")
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
        print(f"gh auth: {'ok' if result.returncode == 0 else 'not authenticated'}")
    print("conductor: optional, not required")
    return 0
