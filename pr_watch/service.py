from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .config import config_bool, default_state_dir, load_config, set_config_value, state_db_path
from .github import current_user, poll_once
from .host_adapter import sync_once as host_sync_once
from .models import SessionInfo
from .notifications import normalize_notification_mode
from .sessions import discover_sessions
from .state import StateStore


DEFAULT_LAUNCHD_LABEL = "com.pr-watch.service"
DEFAULT_TARGET = "macos-launchd"
DEFAULT_LOCK_STALE_SECONDS = 60 * 30


@dataclass(frozen=True)
class LaunchdCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ServiceInstallResult:
    status: str
    label: str
    domain: str
    plist_path: Path
    message: str = ""
    plist: str = ""


@dataclass(frozen=True)
class ServiceStatusResult:
    label: str
    domain: str
    plist_path: Path
    plist_exists: bool
    loaded: bool
    message: str = ""


@dataclass(frozen=True)
class RepoRunResult:
    repo: str
    status: str
    event_count: int = 0
    message: str = ""


@dataclass(frozen=True)
class ServiceRunResult:
    status: str
    event_count: int = 0
    repo_results: List[RepoRunResult] = field(default_factory=list)
    message: str = ""


class SubprocessLaunchdRunner:
    def run(self, command: List[str]) -> LaunchdCommandResult:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        return LaunchdCommandResult(completed.returncode, completed.stdout, completed.stderr)


class RecordingLaunchdRunner:
    def __init__(self, results: Optional[Iterable[LaunchdCommandResult]] = None):
        self.commands: List[List[str]] = []
        self.results = list(results or [])

    def run(self, command: List[str]) -> LaunchdCommandResult:
        self.commands.append(command)
        if self.results:
            return self.results.pop(0)
        return LaunchdCommandResult(0, "", "")


def build_launchd_plist(
    label: str,
    python_executable: str,
    state_dir: str,
    interval_seconds: int,
    stdout_path: str,
    stderr_path: str,
    fixture: Optional[str] = None,
    user: Optional[str] = None,
    notification_mode: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    working_directory: Optional[str] = None,
    host_sync: bool = False,
    host: str = "all",
    conductor_db_path: Optional[str] = None,
    trigger_confirmed: bool = False,
    notify_prompt_confirmed: bool = False,
    notify_prompt_session_state: str = "idle",
) -> str:
    program_arguments = [
        python_executable,
        "-m",
        "pr_watch",
        "--state-dir",
        str(Path(state_dir).expanduser()),
        "service",
        "run-once",
    ]
    if fixture:
        program_arguments.extend(["--fixture", str(Path(fixture).expanduser())])
    if user:
        program_arguments.extend(["--user", user])
    if notification_mode:
        program_arguments.extend(["--notification-mode", normalize_notification_mode(notification_mode)])
    if timeout_seconds is not None:
        program_arguments.extend(["--timeout", str(timeout_seconds)])
    if host_sync:
        program_arguments.append("--host-sync")
        program_arguments.extend(["--host", host])
        if conductor_db_path:
            program_arguments.extend(["--conductor-db", str(Path(conductor_db_path).expanduser())])
        if trigger_confirmed:
            program_arguments.append("--trigger-confirmed")
        if notify_prompt_confirmed:
            program_arguments.append("--notify-prompt-confirmed")
        if notify_prompt_confirmed or notify_prompt_session_state != "idle":
            program_arguments.extend(["--notify-prompt-session-state", notify_prompt_session_state])

    payload = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "StartInterval": int(interval_seconds),
        "RunAtLoad": True,
        "StandardOutPath": str(Path(stdout_path).expanduser()),
        "StandardErrorPath": str(Path(stderr_path).expanduser()),
    }
    if working_directory:
        payload["WorkingDirectory"] = str(Path(working_directory).expanduser())
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def install_launchd_service(
    state_dir: Optional[str] = None,
    interval_seconds: int = 120,
    notification_mode: str = "auto",
    target: str = DEFAULT_TARGET,
    label: str = DEFAULT_LAUNCHD_LABEL,
    python_executable: Optional[str] = None,
    plist_path: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    runner: Optional[object] = None,
    dry_run: bool = False,
    fixture: Optional[str] = None,
    user: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    host_sync: bool = False,
    host: str = "all",
    conductor_db_path: Optional[str] = None,
    trigger_confirmed: bool = False,
    notify_prompt_confirmed: bool = False,
    notify_prompt_session_state: str = "idle",
) -> ServiceInstallResult:
    _validate_target(target)
    if interval_seconds < 1:
        raise ValueError("interval must be at least 1 second")
    mode = normalize_notification_mode(notification_mode)
    state_path = _state_dir_path(state_dir)
    logs = Path(log_dir).expanduser() if log_dir else state_path / "logs"
    plist = Path(plist_path).expanduser() if plist_path else _default_plist_path(label)
    domain = _launchd_domain()
    stdout_path = logs / "service.out.log"
    stderr_path = logs / "service.err.log"
    plist_text = build_launchd_plist(
        label=label,
        python_executable=python_executable or sys.executable,
        state_dir=str(state_path),
        interval_seconds=interval_seconds,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        fixture=fixture,
        user=user,
        notification_mode=mode,
        timeout_seconds=timeout_seconds,
        working_directory=str(_package_root()),
        host_sync=host_sync,
        host=host,
        conductor_db_path=conductor_db_path,
        trigger_confirmed=trigger_confirmed,
        notify_prompt_confirmed=notify_prompt_confirmed,
        notify_prompt_session_state=notify_prompt_session_state,
    )

    if dry_run:
        return ServiceInstallResult("dry_run", label, domain, plist, plist=plist_text)

    set_config_value("poll_interval_seconds", str(interval_seconds), str(state_path))
    set_config_value("notification_mode", mode, str(state_path))
    logs.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_text, encoding="utf-8")

    command_runner = runner or SubprocessLaunchdRunner()
    command_runner.run(["launchctl", "bootout", domain, str(plist)])
    installed = command_runner.run(["launchctl", "bootstrap", domain, str(plist)])
    if installed.returncode != 0:
        message = installed.stderr.strip() or installed.stdout.strip()
        return ServiceInstallResult("failed", label, domain, plist, message=message, plist=plist_text)
    return ServiceInstallResult("installed", label, domain, plist, plist=plist_text)


def uninstall_launchd_service(
    target: str = DEFAULT_TARGET,
    label: str = DEFAULT_LAUNCHD_LABEL,
    plist_path: Optional[Path] = None,
    runner: Optional[object] = None,
) -> ServiceInstallResult:
    _validate_target(target)
    plist = Path(plist_path).expanduser() if plist_path else _default_plist_path(label)
    domain = _launchd_domain()
    command_runner = runner or SubprocessLaunchdRunner()
    removed = command_runner.run(["launchctl", "bootout", domain, str(plist)])
    if plist.exists():
        plist.unlink()
    message = "" if removed.returncode == 0 else removed.stderr.strip() or removed.stdout.strip()
    return ServiceInstallResult("uninstalled", label, domain, plist, message=message)


def service_status(
    target: str = DEFAULT_TARGET,
    label: str = DEFAULT_LAUNCHD_LABEL,
    plist_path: Optional[Path] = None,
    runner: Optional[object] = None,
) -> ServiceStatusResult:
    _validate_target(target)
    plist = Path(plist_path).expanduser() if plist_path else _default_plist_path(label)
    domain = _launchd_domain()
    command_runner = runner or SubprocessLaunchdRunner()
    status = command_runner.run(["launchctl", "print", f"{domain}/{label}"])
    loaded = status.returncode == 0
    message = status.stdout.strip() or status.stderr.strip()
    return ServiceStatusResult(label, domain, plist, plist.exists(), loaded, message)


def run_service_once(
    state_dir: Optional[str] = None,
    current_user_login: Optional[str] = None,
    fixture: Optional[str] = None,
    notification_mode: Optional[str] = None,
    include_drafts: Optional[bool] = None,
    timeout_seconds: Optional[int] = None,
    sessions: Optional[Iterable[SessionInfo]] = None,
    notification_host: Optional[str] = None,
    host_sync: bool = False,
    host: str = "all",
    conductor_db_path: Optional[str | Path] = None,
    trigger_confirmed: bool = False,
    notify_prompt_confirmed: bool = False,
    notify_prompt_session_state: str = "idle",
) -> ServiceRunResult:
    state_path = _state_dir_path(state_dir)
    with single_worker_lock(state_path) as acquired:
        if not acquired:
            return ServiceRunResult("locked", message="another pr-watch service run is already active")
        return _run_service_once_locked(
            state_path=state_path,
            current_user_login=current_user_login,
            fixture=fixture,
            notification_mode=notification_mode,
            include_drafts=include_drafts,
            timeout_seconds=timeout_seconds,
            sessions=sessions,
            notification_host=notification_host,
            host_sync=host_sync,
            host=host,
            conductor_db_path=conductor_db_path,
            trigger_confirmed=trigger_confirmed,
            notify_prompt_confirmed=notify_prompt_confirmed,
            notify_prompt_session_state=notify_prompt_session_state,
        )


def _run_service_once_locked(
    state_path: Path,
    current_user_login: Optional[str],
    fixture: Optional[str],
    notification_mode: Optional[str],
    include_drafts: Optional[bool],
    timeout_seconds: Optional[int],
    sessions: Optional[Iterable[SessionInfo]],
    notification_host: Optional[str],
    host_sync: bool,
    host: str,
    conductor_db_path: Optional[str | Path],
    trigger_confirmed: bool,
    notify_prompt_confirmed: bool,
    notify_prompt_session_state: str,
) -> ServiceRunResult:
    store = StateStore(state_db_path(str(state_path)))
    repos = store.list_watch_repos()
    if not repos:
        return ServiceRunResult("completed", message="no watched repositories configured")

    config = load_config(str(state_path))
    should_include_drafts = (
        include_drafts if include_drafts is not None else config_bool(config, "include_drafts", default=False)
    )
    mode = notification_mode or config.get("notification_mode", "none")
    user = current_user_login or current_user()
    session_list = list(sessions) if sessions is not None else discover_sessions()
    started = time.monotonic()
    repo_results: List[RepoRunResult] = []
    event_count = 0

    for repo in repos:
        if timeout_seconds is not None and time.monotonic() - started >= timeout_seconds:
            repo_results.append(RepoRunResult(repo, "timed_out", message="service run timeout reached"))
            break
        try:
            items = poll_once(
                store,
                user,
                repo=repo,
                fixture=fixture,
                sessions=session_list,
                include_drafts=should_include_drafts,
                notification_mode=mode,
                notification_host=notification_host,
            )
        except Exception as exc:
            repo_results.append(RepoRunResult(repo, "failed", message=str(exc)))
            continue
        event_count += len(items)
        repo_results.append(RepoRunResult(repo, "completed", event_count=len(items)))

    host_message = ""
    host_failed = False
    if host_sync:
        sync_result = host_sync_once(
            store,
            hosts=[host],
            conductor_db_path=conductor_db_path,
            trigger_confirmed=trigger_confirmed,
            notify_prompt_confirmed=notify_prompt_confirmed,
            session_state=notify_prompt_session_state,
        )
        host_failed = any(
            item.action in {"failed", "missing", "schema_mismatch", "unavailable"}
            for item in sync_result.host_results
        ) or any(
            item.action in {"failed", "notify_prompt_failed"}
            for item in [*sync_result.trigger_results, *sync_result.notify_prompt_results]
        )
        host_message = _host_sync_message(
            sync_result.host_results,
            len(sync_result.trigger_results),
            len(sync_result.notify_prompt_results),
        )

    if any(item.status == "failed" for item in repo_results):
        status = "completed_with_errors"
    elif any(item.status == "timed_out" for item in repo_results):
        status = "timed_out"
    elif host_failed:
        status = "completed_with_errors"
    else:
        status = "completed"
    return ServiceRunResult(status, event_count=event_count, repo_results=repo_results, message=host_message)


def _host_sync_message(host_results: list[object], trigger_count: int, notify_prompt_count: int = 0) -> str:
    if not host_results and not trigger_count and not notify_prompt_count:
        return "host sync: no eligible pending host events"
    mirrored = sum(1 for item in host_results if getattr(item, "action", "") == "mirrored")
    confirmations = sum(1 for item in host_results if getattr(item, "action", "") == "confirmation_requested")
    already = sum(1 for item in host_results if getattr(item, "action", "") == "already_synced")
    already_confirmations = sum(
        1 for item in host_results if getattr(item, "action", "") == "confirmation_already_requested"
    )
    confirmation_prompts = sum(
        1 for item in host_results if str(getattr(item, "action", "")).startswith("confirmation_prompt_")
    )
    failed = sum(
        1
        for item in host_results
        if getattr(item, "action", "") in {"failed", "missing", "schema_mismatch", "unavailable"}
        or getattr(item, "action", "") == "confirmation_prompt_failed"
    )
    return (
        f"host sync: mirrored={mirrored} confirmation_requested={confirmations} "
        f"already_synced={already} confirmation_already_requested={already_confirmations} "
        f"confirmation_prompted={confirmation_prompts} failed={failed} "
        f"triggered={trigger_count} notify_prompted={notify_prompt_count}"
    )


@contextmanager
def single_worker_lock(
    state_dir: str | Path,
    stale_after_seconds: int = DEFAULT_LOCK_STALE_SECONDS,
):
    state_path = Path(state_dir).expanduser()
    state_path.mkdir(parents=True, exist_ok=True)
    lock_path = state_path / "service.lock"
    fd: Optional[int] = None
    acquired = False
    try:
        fd = _create_lock(lock_path, stale_after_seconds)
        acquired = fd is not None
        yield acquired
    finally:
        if fd is not None:
            os.close(fd)
        if acquired:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def _create_lock(lock_path: Path, stale_after_seconds: int) -> Optional[int]:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if _lock_is_stale(lock_path, stale_after_seconds):
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            return _create_lock(lock_path, stale_after_seconds)
        return None
    os.write(fd, f"pid={os.getpid()} started={int(time.time())}\n".encode("utf-8"))
    return fd


def _lock_is_stale(lock_path: Path, stale_after_seconds: int) -> bool:
    pid = _lock_pid(lock_path)
    if pid is not None and _pid_is_alive(pid):
        return False
    if pid is not None:
        return True
    if stale_after_seconds <= 0:
        return False
    try:
        return time.time() - lock_path.stat().st_mtime > stale_after_seconds
    except FileNotFoundError:
        return False


def _lock_pid(lock_path: Path) -> Optional[int]:
    try:
        text = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for part in text.split():
        if part.startswith("pid="):
            try:
                return int(part.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _state_dir_path(state_dir: Optional[str]) -> Path:
    return Path(state_dir).expanduser() if state_dir else default_state_dir()


def _default_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _validate_target(target: str) -> None:
    if target != DEFAULT_TARGET:
        raise ValueError("target must be macos-launchd")
