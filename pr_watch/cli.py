from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .config import config_bool, load_config, set_config_value, state_db_path
from .delivery import DEFAULT_BUSY_POLICY, approve_event, notify_prompt_event
from .github import current_user, daemon_loop, poll_once
from .host_integration import (
    build_codex_mcp_add_command,
    build_codex_mcp_get_command,
    build_mcp_launch_config,
    install_mcp_hosts,
    resolve_codex_hosts,
)
from .host_adapter import status as host_status
from .host_adapter import sync_once as host_sync_once
from .notifications import VALID_NOTIFICATION_MODES, notify_event
from .service import (
    DEFAULT_LAUNCHD_LABEL,
    install_launchd_service,
    run_service_once,
    service_status,
    uninstall_launchd_service,
)
from .setup import detect_current_repo
from .sessions import discover_sessions
from .state import StateStore
from .util import normalize_repo_full_name
from .workflow import confirm_binding_for_event as workflow_confirm_binding_for_event
from .workflow import create_explicit_binding


INIT_PROFILE_NOTIFICATION_MODES = {
    "terminal": "desktop",
    "conductor": "in_app",
    "app": "in_app",
    "auto": "auto",
}
SERVICE_NOTIFICATION_MODES = VALID_NOTIFICATION_MODES - {"browser"}


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
        if args.command == "confirm-binding":
            store = StateStore(state_db_path(args.state_dir))
            config = load_config(args.state_dir)
            result = workflow_confirm_binding_for_event(
                store,
                args.event_id,
                session_id=args.session_id,
                mirror_now=args.mirror_now,
                trigger=args.trigger,
                host=args.host,
                conductor_db_path=args.conductor_db,
                session_state=args.session_state,
                busy_policy=args.busy_policy or config.get("busy_policy", DEFAULT_BUSY_POLICY),
            )
            binding = result["binding"]
            print(f"{result['action']}: {args.event_id}")
            print(f"binding: {binding.agent}:{binding.session_id}")
            host_sync = result.get("host_sync")
            if host_sync:
                for item in host_sync.host_results:
                    target = f"\ttarget={item.target_id}" if item.target_id else ""
                    event = item.event_id or "-"
                    print(f"{item.host}\t{event}\t{item.action}{target}")
                    if item.message:
                        print(f"  {item.message}")
            trigger_result = result.get("trigger")
            if trigger_result:
                print(f"trigger\t{trigger_result.event_id}\t{trigger_result.action}")
                if trigger_result.message:
                    print(trigger_result.message)
            return 0
        if args.command == "daemon":
            store = StateStore(state_db_path(args.state_dir))
            user = args.user or current_user()
            config = load_config(args.state_dir)
            include_drafts = (
                args.include_drafts
                if args.include_drafts is not None
                else config_bool(config, "include_drafts", default=False)
            )
            notification_mode = args.notification_mode or config.get("notification_mode", "none")
            sessions = discover_sessions()
            if args.once:
                items = poll_once(
                    store,
                    user,
                    repo=args.repo,
                    fixture=args.fixture,
                    sessions=sessions,
                    include_drafts=include_drafts,
                    notification_mode=notification_mode,
                )
                print(f"recorded {len(items)} actionable event(s)")
                return 0
            interval = int(args.interval or config.get("poll_interval_seconds", "120"))
            daemon_loop(
                store,
                user,
                repo=args.repo,
                fixture=args.fixture,
                sessions=sessions,
                interval_seconds=interval,
                include_drafts=include_drafts,
                notification_mode=notification_mode,
            )
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
        if args.command == "notify":
            store = StateStore(state_db_path(args.state_dir))
            config = load_config(args.state_dir)
            mode = args.mode or config.get("notification_mode", "desktop")
            result = notify_event(store, args.event_id, mode=mode, force=args.force)
            print(f"{result.action}: {args.event_id}")
            if result.channels:
                print("channels:", ", ".join(result.channels))
            if result.message:
                print(result.message)
            return 0 if result.action != "failed" else 2
        if args.command == "notify-prompt":
            store = StateStore(state_db_path(args.state_dir))
            result = notify_prompt_event(
                store,
                args.event_id,
                session_state=args.session_state,
            )
            print(f"{result.action}: {args.event_id}")
            if result.command:
                print("command:", " ".join(result.command[:-1]), "<prompt>")
            if result.message:
                print(result.message)
            return 0 if result.action != "notify_prompt_failed" else 2
        if args.command == "notifications":
            store = StateStore(state_db_path(args.state_dir))
            print_notifications(store, include_done=args.all)
            return 0
        if args.command == "watch":
            return handle_watch(args)
        if args.command == "setup":
            return handle_setup(args)
        if args.command == "service":
            return handle_service(args)
        if args.command == "host":
            return handle_host(args)
        if args.command == "config":
            path = set_config_value(args.key, args.value, args.state_dir)
            print(f"updated {path}")
            return 0
        if args.command == "init":
            mode = INIT_PROFILE_NOTIFICATION_MODES[args.profile]
            path = set_config_value("notification_mode", mode, args.state_dir)
            print(f"initialized {path}")
            print(f"profile: {args.profile}")
            print(f"notification_mode: {mode}")
            return 0
        if args.command == "install-mcp":
            state_dir = str(state_db_path(args.state_dir).parent)
            if args.dry_run:
                launch = build_mcp_launch_config(
                    python_executable=args.python_executable,
                    state_dir=state_dir,
                )
                hosts = resolve_codex_hosts(
                    args.target,
                    codex_binary=args.codex_bin,
                    conductor_codex_binary=args.conductor_codex_bin,
                )
                if not hosts:
                    print("No Codex host binaries found.")
                    return 1
                for host in hosts:
                    print(f"{host.host}: {host.binary}")
                    print("  check:", " ".join(build_codex_mcp_get_command(host.binary)))
                    print("  add:", " ".join(build_codex_mcp_add_command(host.binary, launch)))
                return 0
            results = install_mcp_hosts(
                target=args.target,
                python_executable=args.python_executable,
                state_dir=state_dir,
                codex_binary=args.codex_bin,
                conductor_codex_binary=args.conductor_codex_bin,
                replace=not args.no_replace,
            )
            if not results:
                print("No Codex host binaries found.")
                return 1
            failed = False
            for result in results:
                print(f"{result.host}: {result.status}")
                if result.binary:
                    print(f"  binary: {result.binary}")
                if result.message:
                    print(f"  {result.message}")
                failed = failed or result.status == "failed"
            return 1 if failed else 0
        if args.command == "doctor":
            return doctor(args.state_dir)
        if args.command == "mcp":
            from .mcp_server import run_server

            run_server(args.state_dir)
            return 0
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
    daemon.add_argument(
        "--include-drafts",
        action="store_true",
        default=None,
        help="also watch draft pull requests; default is config include_drafts=false",
    )
    daemon.add_argument(
        "--notification-mode",
        choices=sorted(VALID_NOTIFICATION_MODES),
        help="send independent notifications for recorded events; default is config notification_mode=auto",
    )

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

    confirm = subparsers.add_parser(
        "confirm-binding",
        help="confirm an inferred or rebind PR/session binding without approving delivery",
    )
    confirm.add_argument("event_id")
    confirm.add_argument("--session-id", help="confirm or reassign to this session for the same PR and role")
    confirm.add_argument("--no-mirror", dest="mirror_now", action="store_false", default=True)
    confirm.add_argument("--host", choices=["conductor"], default="conductor")
    confirm.add_argument("--conductor-db", help="override Conductor SQLite DB path for immediate mirror")
    confirm.add_argument("--trigger", action="store_true", help="also approve/queue/resume after confirming")
    confirm.add_argument("--session-state", choices=["idle", "working", "unknown"], default="unknown")
    confirm.add_argument(
        "--busy-policy",
        choices=["run_if_idle_queue_if_busy", "always_queue", "notify_only", "drop_if_busy", "ask_when_busy"],
    )

    approve = subparsers.add_parser("approve", help="approve delivery for an inbox event")
    approve.add_argument("event_id")
    approve.add_argument("--session-state", choices=["idle", "working", "unknown"], default="unknown")
    approve.add_argument(
        "--busy-policy",
        choices=["run_if_idle_queue_if_busy", "always_queue", "notify_only", "drop_if_busy", "ask_when_busy"],
    )

    subparsers.add_parser("queue", help="show queued resume commands")

    notify = subparsers.add_parser("notify", help="send an independent notification for an inbox event")
    notify.add_argument("event_id")
    notify.add_argument("--mode", choices=sorted(VALID_NOTIFICATION_MODES))
    notify.add_argument("--force", action="store_true", help="send again even if this event/channel was notified")

    notify_prompt = subparsers.add_parser(
        "notify-prompt",
        help="send a safe notification-only prompt to the bound session",
    )
    notify_prompt.add_argument("event_id")
    notify_prompt.add_argument("--session-state", choices=["idle", "working", "unknown"], default="unknown")

    notifications = subparsers.add_parser("notifications", help="show in-app notification inbox and failures")
    notifications.add_argument("--all", action="store_true", help="include sent desktop notifications")

    watch = subparsers.add_parser("watch", help="manage repositories polled by the background service")
    watch_sub = watch.add_subparsers(dest="watch_command")
    watch_add = watch_sub.add_parser("add", help="watch a repository")
    watch_add.add_argument("repo", help="repository as owner/name")
    watch_remove = watch_sub.add_parser("remove", help="stop watching a repository")
    watch_remove.add_argument("repo", help="repository as owner/name")
    watch_sub.add_parser("list", help="list watched repositories")
    watch_sub.add_parser("clear", help="remove all watched repositories")
    watch.set_defaults(command="watch")

    setup = subparsers.add_parser("setup", help="guided setup for background repository watching")
    setup_source = setup.add_mutually_exclusive_group()
    setup_source.add_argument("--current-repo", action="store_true", help="watch the current git repository")
    setup_source.add_argument("--repo", help="repository as owner/name")
    setup.add_argument("--interactive", action="store_true", help="prompt for setup choices")
    setup.add_argument("--install-service", action="store_true", help="install or update the background service")
    setup.add_argument("--interval", type=int, default=120, help="launchd StartInterval in seconds")
    setup.add_argument(
        "--notification-mode",
        choices=sorted(SERVICE_NOTIFICATION_MODES),
        default="auto",
        help="notification mode used by service run-once",
    )
    setup.add_argument("--target", choices=["macos-launchd"], default="macos-launchd")
    setup.add_argument("--python", dest="python_executable", default=sys.executable)
    setup.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    setup.add_argument("--plist-path", help="override LaunchAgent plist path")
    setup.add_argument("--log-dir", help="override service log directory")
    setup.add_argument("--dry-run", action="store_true", help="print planned changes without applying them")
    setup.add_argument("--fixture", help="fixture path for local testing")
    setup.add_argument("--user", help="GitHub login for local testing; defaults to gh api user")
    setup.add_argument("--timeout", type=int, help="optional run-once time budget in seconds")
    setup.add_argument("--host-sync", action="store_true", help="run host sync after each service poll")
    setup.add_argument("--host", choices=["all", "conductor", "codex-app"], default="all")
    setup.add_argument("--conductor-db", help="override Conductor SQLite DB path for host sync")
    setup.add_argument(
        "--trigger-confirmed",
        action="store_true",
        help="auto-trigger only confirmed high-confidence bindings during host sync",
    )
    setup.add_argument(
        "--notify-prompt-confirmed",
        action="store_true",
        help="soft-trigger safe notify-only prompts for confirmed high-confidence bindings during host sync",
    )
    setup.add_argument(
        "--notify-prompt-session-state",
        choices=["idle", "working", "unknown"],
        default="idle",
        help="session state used for service notify prompts; idle sends, unknown/working queues",
    )
    setup.set_defaults(command="setup")

    service = subparsers.add_parser("service", help="manage the user-level background watcher service")
    service_sub = service.add_subparsers(dest="service_command")
    service_install = service_sub.add_parser("install", help="install the macOS launchd one-shot watcher")
    service_install.add_argument("--interval", type=int, default=120, help="launchd StartInterval in seconds")
    service_install.add_argument(
        "--notification-mode",
        choices=sorted(SERVICE_NOTIFICATION_MODES),
        default="auto",
        help="notification mode used by service run-once",
    )
    service_install.add_argument("--target", choices=["macos-launchd"], default="macos-launchd")
    service_install.add_argument("--python", dest="python_executable", default=sys.executable)
    service_install.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    service_install.add_argument("--plist-path", help="override LaunchAgent plist path")
    service_install.add_argument("--log-dir", help="override service log directory")
    service_install.add_argument("--dry-run", action="store_true", help="print the plist without installing")
    service_install.add_argument("--fixture", help="fixture path for local testing")
    service_install.add_argument("--user", help="GitHub login for local testing; defaults to gh api user")
    service_install.add_argument("--timeout", type=int, help="optional run-once time budget in seconds")
    service_install.add_argument("--host-sync", action="store_true", help="run host sync after each service poll")
    service_install.add_argument("--host", choices=["all", "conductor", "codex-app"], default="all")
    service_install.add_argument("--conductor-db", help="override Conductor SQLite DB path for host sync")
    service_install.add_argument(
        "--trigger-confirmed",
        action="store_true",
        help="auto-trigger only confirmed high-confidence bindings during host sync",
    )
    service_install.add_argument(
        "--notify-prompt-confirmed",
        action="store_true",
        help="soft-trigger safe notify-only prompts for confirmed high-confidence bindings during host sync",
    )
    service_install.add_argument(
        "--notify-prompt-session-state",
        choices=["idle", "working", "unknown"],
        default="idle",
        help="session state used for service notify prompts; idle sends, unknown/working queues",
    )

    service_status_parser = service_sub.add_parser("status", help="show launchd service status")
    service_status_parser.add_argument("--target", choices=["macos-launchd"], default="macos-launchd")
    service_status_parser.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    service_status_parser.add_argument("--plist-path", help="override LaunchAgent plist path")

    service_uninstall = service_sub.add_parser("uninstall", help="uninstall the launchd watcher")
    service_uninstall.add_argument("--target", choices=["macos-launchd"], default="macos-launchd")
    service_uninstall.add_argument("--label", default=DEFAULT_LAUNCHD_LABEL)
    service_uninstall.add_argument("--plist-path", help="override LaunchAgent plist path")

    service_run_once = service_sub.add_parser("run-once", help="poll all watched repositories once and exit")
    service_run_once.add_argument("--fixture", help="JSON fixture for local replay/testing")
    service_run_once.add_argument("--user", help="current GitHub login; defaults to gh api user")
    service_run_once.add_argument(
        "--notification-mode",
        choices=sorted(VALID_NOTIFICATION_MODES),
        help="override configured notification mode for this run",
    )
    service_run_once.add_argument(
        "--include-drafts",
        action="store_true",
        default=None,
        help="also watch draft pull requests; default is config include_drafts=false",
    )
    service_run_once.add_argument("--timeout", type=int, help="optional time budget in seconds")
    service_run_once.add_argument("--host-sync", action="store_true", help="run host sync after polling")
    service_run_once.add_argument("--host", choices=["all", "conductor", "codex-app"], default="all")
    service_run_once.add_argument("--conductor-db", help="override Conductor SQLite DB path for host sync")
    service_run_once.add_argument(
        "--trigger-confirmed",
        action="store_true",
        help="auto-trigger only confirmed high-confidence bindings during host sync",
    )
    service_run_once.add_argument(
        "--notify-prompt-confirmed",
        action="store_true",
        help="soft-trigger safe notify-only prompts for confirmed high-confidence bindings during host sync",
    )
    service_run_once.add_argument(
        "--notify-prompt-session-state",
        choices=["idle", "working", "unknown"],
        default="idle",
        help="session state used for service notify prompts; idle sends, unknown/working queues",
    )
    service.set_defaults(command="service")

    host = subparsers.add_parser("host", help="sync pending events into local host surfaces")
    host_sub = host.add_subparsers(dest="host_command")
    host_status_parser = host_sub.add_parser("status", help="show host bridge support and diagnostics")
    host_status_parser.add_argument("--conductor-db", help="override Conductor SQLite DB path")
    host_sync = host_sub.add_parser("sync-once", help="mirror pending events to host surfaces once")
    host_sync.add_argument("--host", choices=["all", "conductor", "codex-app"], default="all")
    host_sync.add_argument("--conductor-db", help="override Conductor SQLite DB path")
    host_sync.add_argument(
        "--trigger-confirmed",
        action="store_true",
        help="approve/queue/resume only confirmed high-confidence bindings",
    )
    host_sync.add_argument(
        "--notify-prompt-confirmed",
        action="store_true",
        help="soft-trigger safe notify-only prompts for confirmed high-confidence bindings",
    )
    host_sync.add_argument("--session-state", choices=["idle", "working", "unknown"], default="unknown")
    host_sync.add_argument(
        "--busy-policy",
        choices=["run_if_idle_queue_if_busy", "always_queue", "notify_only", "drop_if_busy", "ask_when_busy"],
    )
    host.set_defaults(command="host")

    init = subparsers.add_parser("init", help="initialize pr-watch defaults for a host profile")
    init.add_argument("--profile", choices=sorted(INIT_PROFILE_NOTIFICATION_MODES), default="auto")

    install_mcp = subparsers.add_parser(
        "install-mcp",
        help="register the PR Watch MCP server with Codex App and/or Conductor",
    )
    install_mcp.add_argument("--target", choices=["codex-app", "codex", "app", "conductor", "all"], default="all")
    install_mcp.add_argument("--python", dest="python_executable", default=sys.executable)
    install_mcp.add_argument("--codex-bin", help="Codex App/CLI binary; defaults to PATH codex or ~/bin/codex")
    install_mcp.add_argument(
        "--conductor-codex-bin",
        help="Conductor bundled codex binary; defaults to ~/Library/Application Support/com.conductor.app/bin/codex",
    )
    install_mcp.add_argument("--no-replace", action="store_true", help="do not replace an existing pr-watch MCP entry")
    install_mcp.add_argument("--dry-run", action="store_true", help="print the commands without changing Codex config")

    config = subparsers.add_parser("config", help="configure pr-watch")
    config_sub = config.add_subparsers(dest="config_command")
    config_set = config_sub.add_parser("set", help="set a config value")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config.set_defaults(command="config")

    subparsers.add_parser("doctor", help="check local dependencies and state")
    subparsers.add_parser("mcp", help="run the PR watcher MCP server over stdio")
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


def print_notifications(store: StateStore, include_done: bool = False) -> None:
    notifications = store.list_notifications(include_done=include_done)
    if not notifications:
        print("No notifications.")
        return
    for item in notifications:
        print(f"{item.notification_id}\t{item.status}\t{item.channel}\t{item.event_id}\t{item.title}")
        if item.error:
            print("  error:", item.error)


def handle_watch(args: argparse.Namespace) -> int:
    store = StateStore(state_db_path(args.state_dir))
    if args.watch_command == "add":
        repo = store.add_watch_repo(args.repo)
        print(f"watching {repo}")
        return 0
    if args.watch_command == "remove":
        repo = args.repo
        removed = store.remove_watch_repo(repo)
        print(f"removed {repo.lower()}" if removed else f"not watching {repo.lower()}")
        return 0 if removed else 1
    if args.watch_command == "list":
        repos = store.list_watch_repos()
        if not repos:
            print("No watched repositories.")
            return 0
        for repo in repos:
            print(repo)
        return 0
    if args.watch_command == "clear":
        count = store.clear_watch_repos()
        print(f"cleared {count} watched repository/repositories")
        return 0
    return 2


def handle_host(args: argparse.Namespace) -> int:
    if args.host_command == "status":
        result = host_status(conductor_db_path=args.conductor_db)
        print(f"conductor: {result.conductor.status}")
        print(f"conductor_db: {result.conductor.db_path}")
        if result.conductor.message:
            print(f"conductor_message: {result.conductor.message}")
        print(f"codex-app: {result.codex_app.status}")
        print(f"codex_app_message: {result.codex_app.message}")
        return 0
    if args.host_command == "sync-once":
        store = StateStore(state_db_path(args.state_dir))
        result = host_sync_once(
            store,
            hosts=[args.host],
            conductor_db_path=args.conductor_db,
            trigger_confirmed=args.trigger_confirmed,
            notify_prompt_confirmed=args.notify_prompt_confirmed,
            session_state=args.session_state,
            busy_policy=args.busy_policy or DEFAULT_BUSY_POLICY,
        )
        if args.host in {"all", "codex-app"}:
            print(f"codex-app: {result.codex_app.status}")
            print(f"codex_app_message: {result.codex_app.message}")
        for item in result.host_results:
            target = f"\ttarget={item.target_id}" if item.target_id else ""
            event = item.event_id or "-"
            print(f"{item.host}\t{event}\t{item.action}{target}")
            if item.message:
                print(f"  {item.message}")
        for item in result.trigger_results:
            print(f"trigger\t{item.event_id}\t{item.action}")
            if item.message:
                print(f"  {item.message}")
        for item in result.notify_prompt_results:
            print(f"notify-prompt\t{item.event_id}\t{item.action}")
            if item.message:
                print(f"  {item.message}")
        if not result.host_results and not result.trigger_results and not result.notify_prompt_results:
            print("No eligible pending host events.")
        failed = any(
            item.action in {"failed", "missing", "schema_mismatch", "unavailable"}
            for item in result.host_results
        ) or any(
            item.action in {"failed", "notify_prompt_failed"}
            for item in [*result.trigger_results, *result.notify_prompt_results]
        )
        return 1 if failed else 0
    print("pr-watch host: choose status or sync-once", file=sys.stderr)
    return 2


def handle_setup(args: argparse.Namespace) -> int:
    repo = _setup_repo(args)
    install_service = args.install_service
    if args.interactive:
        repo, install_service = _prompt_setup(repo, install_service)

    if not repo:
        print("pr-watch setup: choose --current-repo, --repo owner/name, or --interactive", file=sys.stderr)
        return 2
    repo = normalize_repo_full_name(repo)

    if args.dry_run:
        print(f"would watch {repo}")
    else:
        store = StateStore(state_db_path(args.state_dir))
        repo = store.add_watch_repo(repo)
        print(f"watching {repo}")

    if not install_service:
        print("service install skipped; pass --install-service to enable background polling")
        return 0

    result = install_launchd_service(
        state_dir=args.state_dir,
        interval_seconds=args.interval,
        notification_mode=args.notification_mode,
        target=args.target,
        label=args.label,
        python_executable=args.python_executable,
        plist_path=Path(args.plist_path) if args.plist_path else None,
        log_dir=Path(args.log_dir) if args.log_dir else None,
        dry_run=args.dry_run,
        fixture=args.fixture,
        user=args.user,
        timeout_seconds=args.timeout,
        host_sync=args.host_sync,
        host=args.host,
        conductor_db_path=args.conductor_db,
        trigger_confirmed=args.trigger_confirmed,
        notify_prompt_confirmed=args.notify_prompt_confirmed,
        notify_prompt_session_state=args.notify_prompt_session_state,
    )
    print(f"{result.status}: {result.label}")
    print(f"domain: {result.domain}")
    print(f"plist: {result.plist_path}")
    if result.message:
        print(result.message)
    if args.dry_run:
        print(result.plist, end="")
    return 0 if result.status in {"installed", "dry_run"} else 1


def _setup_repo(args: argparse.Namespace) -> Optional[str]:
    if args.current_repo:
        return detect_current_repo(Path.cwd())
    return args.repo


def _prompt_setup(repo: Optional[str], install_service: bool) -> tuple[Optional[str], bool]:
    detected_repo = repo
    if detected_repo is None:
        try:
            detected_repo = detect_current_repo(Path.cwd())
        except ValueError:
            detected_repo = None

    prompt = "Repository to watch"
    if detected_repo:
        prompt += f" [{detected_repo}]"
    prompt += ": "
    answer = input(prompt).strip()
    selected_repo = answer or detected_repo

    service_default = "y" if install_service else "n"
    service_answer = input(f"Install/update background service? [{service_default}] ").strip().lower()
    selected_install_service = install_service if not service_answer else service_answer in {"y", "yes"}
    return selected_repo, selected_install_service


def handle_service(args: argparse.Namespace) -> int:
    if args.service_command == "install":
        result = install_launchd_service(
            state_dir=args.state_dir,
            interval_seconds=args.interval,
            notification_mode=args.notification_mode,
            target=args.target,
            label=args.label,
            python_executable=args.python_executable,
            plist_path=Path(args.plist_path) if args.plist_path else None,
            log_dir=Path(args.log_dir) if args.log_dir else None,
            dry_run=args.dry_run,
            fixture=args.fixture,
            user=args.user,
            timeout_seconds=args.timeout,
            host_sync=args.host_sync,
            host=args.host,
            conductor_db_path=args.conductor_db,
            trigger_confirmed=args.trigger_confirmed,
            notify_prompt_confirmed=args.notify_prompt_confirmed,
            notify_prompt_session_state=args.notify_prompt_session_state,
        )
        print(f"{result.status}: {result.label}")
        print(f"domain: {result.domain}")
        print(f"plist: {result.plist_path}")
        if result.message:
            print(result.message)
        if args.dry_run:
            print(result.plist, end="")
        return 0 if result.status in {"installed", "dry_run"} else 1
    if args.service_command == "status":
        result = service_status(
            target=args.target,
            label=args.label,
            plist_path=Path(args.plist_path) if args.plist_path else None,
        )
        print(f"label: {result.label}")
        print(f"domain: {result.domain}")
        print(f"plist: {result.plist_path}")
        print(f"plist_exists: {str(result.plist_exists).lower()}")
        print(f"loaded: {str(result.loaded).lower()}")
        if result.message:
            print(result.message)
        return 0
    if args.service_command == "uninstall":
        result = uninstall_launchd_service(
            target=args.target,
            label=args.label,
            plist_path=Path(args.plist_path) if args.plist_path else None,
        )
        print(f"{result.status}: {result.label}")
        print(f"plist: {result.plist_path}")
        if result.message:
            print(result.message)
        return 0 if result.status == "uninstalled" else 1
    if args.service_command == "run-once":
        config = load_config(args.state_dir)
        include_drafts = (
            args.include_drafts
            if args.include_drafts is not None
            else config_bool(config, "include_drafts", default=False)
        )
        result = run_service_once(
            state_dir=args.state_dir,
            current_user_login=args.user,
            fixture=args.fixture,
            notification_mode=args.notification_mode,
            include_drafts=include_drafts,
            timeout_seconds=args.timeout,
            host_sync=args.host_sync,
            host=args.host,
            conductor_db_path=args.conductor_db,
            trigger_confirmed=args.trigger_confirmed,
            notify_prompt_confirmed=args.notify_prompt_confirmed,
            notify_prompt_session_state=args.notify_prompt_session_state,
        )
        print(f"{result.status}: recorded {result.event_count} actionable event(s)")
        if result.message:
            print(result.message)
        for repo_result in result.repo_results:
            line = f"{repo_result.repo}\t{repo_result.status}\t{repo_result.event_count}"
            if repo_result.message:
                line += f"\t{repo_result.message}"
            print(line)
        return 0 if result.status in {"completed", "locked"} else 1
    return 2


def current_branch() -> str:
    result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def doctor(state_dir: Optional[str]) -> int:
    config = load_config(state_dir)
    print(f"state: {state_db_path(state_dir)}")
    print(f"busy_policy: {config.get('busy_policy')}")
    print(f"include_drafts: {config.get('include_drafts')}")
    print(f"notification_mode: {config.get('notification_mode')}")
    for executable in ["gh", "claude", "codex", "osascript"]:
        path = shutil.which(executable)
        print(f"{executable}: {path or 'not found'}")
    if shutil.which("gh"):
        result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
        print(f"gh auth: {'ok' if result.returncode == 0 else 'not authenticated'}")
    print("conductor: optional, not required")
    return 0
