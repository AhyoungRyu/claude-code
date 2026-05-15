from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .models import Binding, InboxItem, NotificationResult
from .state import StateStore


VALID_NOTIFICATION_MODES = {"none", "desktop", "in_app", "both", "auto", "browser"}
APP_HOSTS = {"conductor", "codex-app", "codex_app", "mcp"}
HOST_ACTIVATION_BUNDLE_IDS = {
    "conductor": "com.conductor.app",
    "codex-app": "com.openai.codex",
    "codex_app": "com.openai.codex",
    "warp": "dev.warp.Warp-Stable",
    "terminal-warp": "dev.warp.Warp-Stable",
    "terminal": "com.apple.Terminal",
    "iterm": "com.googlecode.iterm2",
    "iterm2": "com.googlecode.iterm2",
}


@dataclass(frozen=True)
class NotificationSendResult:
    ok: bool
    error: str = ""


class DesktopNotifier:
    def send(
        self,
        title: str,
        message: str,
        event: InboxItem,
        activation_bundle_id: Optional[str] = None,
    ) -> NotificationSendResult:
        if platform.system() != "Darwin":
            return NotificationSendResult(False, "desktop notifications currently use macOS osascript")
        terminal_notifier = shutil.which("terminal-notifier")
        if terminal_notifier and activation_bundle_id:
            command = [
                terminal_notifier,
                "-title",
                title,
                "-message",
                message,
                "-group",
                event.event_id,
                "-activate",
                activation_bundle_id,
            ]
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            if result.returncode == 0:
                return NotificationSendResult(True)
            return NotificationSendResult(False, result.stderr.strip() or result.stdout.strip())

        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)}"
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return NotificationSendResult(True)
        return NotificationSendResult(False, result.stderr.strip() or result.stdout.strip())


class RecordingNotifier:
    def __init__(self, result: Optional[NotificationSendResult] = None):
        self.result = result or NotificationSendResult(True)
        self.messages: List[dict] = []

    def send(
        self,
        title: str,
        message: str,
        event: InboxItem,
        activation_bundle_id: Optional[str] = None,
    ) -> NotificationSendResult:
        self.messages.append(
            {
                "title": title,
                "message": message,
                "event_id": event.event_id,
                "activation_bundle_id": activation_bundle_id,
            }
        )
        return self.result


def notify_events(
    store: StateStore,
    events: Iterable[InboxItem],
    mode: str = "none",
    notifier: Optional[object] = None,
    force: bool = False,
    host: Optional[str] = None,
) -> List[NotificationResult]:
    return [
        notify_event(store, event.event_id, mode=mode, notifier=notifier, force=force, host=host)
        for event in events
    ]


def notify_event(
    store: StateStore,
    event_id: str,
    mode: str = "desktop",
    notifier: Optional[object] = None,
    force: bool = False,
    host: Optional[str] = None,
) -> NotificationResult:
    normalized = resolve_notification_mode(mode, host=host)
    if normalized == "none":
        return NotificationResult("skipped", event_id, message="notification mode is none")

    event = store.get_event(event_id)
    binding = store.get_binding(event.binding_id) if event.binding_id else None
    title, message = render_notification(event)
    activation_bundle_id = activation_bundle_id_for_binding(binding)
    delivered: List[str] = []
    failures: List[str] = []
    skipped: List[str] = []

    for channel in channels_for_mode(normalized):
        existing = store.get_notification(event_id, channel)
        if existing and not force:
            skipped.append(channel)
            continue
        if channel == "in_app":
            store.upsert_notification(
                event_id=event_id,
                channel=channel,
                title=title,
                message=message,
                target_url=event.pr_url,
                status="pending",
            )
            delivered.append(channel)
            continue

        sender = notifier or DesktopNotifier()
        result = sender.send(title, message, event, activation_bundle_id=activation_bundle_id)
        status = "sent" if result.ok else "failed"
        store.upsert_notification(
            event_id=event_id,
            channel=channel,
            title=title,
            message=message,
            target_url=event.pr_url,
            status=status,
            error=result.error,
        )
        if result.ok:
            delivered.append(channel)
        else:
            failures.append(f"{channel}: {result.error}")

    if failures:
        return NotificationResult("failed", event_id, channels=delivered, message="; ".join(failures))
    if delivered:
        return NotificationResult("notified", event_id, channels=delivered)
    return NotificationResult("already_notified", event_id, channels=skipped)


def render_notification(event: InboxItem) -> tuple[str, str]:
    title = f"{event.repo_name} #{event.pr_number} needs attention"
    message = event.summary
    return title, message


def activation_bundle_id_for_binding(binding: Optional[Binding]) -> Optional[str]:
    if binding is None or not binding.host:
        return None
    return HOST_ACTIVATION_BUNDLE_IDS.get(binding.host.strip().lower())


def normalize_notification_mode(mode: str) -> str:
    normalized = (mode or "none").strip().lower()
    if normalized == "browser":
        return "in_app"
    if normalized not in VALID_NOTIFICATION_MODES:
        raise ValueError(f"notification mode must be one of: {', '.join(sorted(VALID_NOTIFICATION_MODES))}")
    return normalized


def resolve_notification_mode(
    mode: str,
    host: Optional[str] = None,
    platform_name: Optional[str] = None,
) -> str:
    normalized = normalize_notification_mode(mode)
    if normalized != "auto":
        return normalized
    if (host or "").strip().lower() in APP_HOSTS:
        return "in_app"
    system = platform_name or platform.system()
    if system == "Darwin":
        return "desktop"
    return "in_app"


def channels_for_mode(mode: str) -> List[str]:
    normalized = normalize_notification_mode(mode)
    if normalized == "both":
        return ["desktop", "in_app"]
    if normalized == "none":
        return []
    if normalized == "auto":
        normalized = resolve_notification_mode(normalized)
    return [normalized]


def _applescript_string(value: str) -> str:
    one_line = " ".join(str(value).split())
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'
