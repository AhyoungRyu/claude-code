from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import Iterable, List, Optional

from .models import InboxItem, NotificationResult
from .state import StateStore


VALID_NOTIFICATION_MODES = {"none", "desktop", "browser", "both"}


@dataclass(frozen=True)
class NotificationSendResult:
    ok: bool
    error: str = ""


class DesktopNotifier:
    def send(self, title: str, message: str, event: InboxItem) -> NotificationSendResult:
        if platform.system() != "Darwin":
            return NotificationSendResult(False, "desktop notifications currently use macOS osascript")
        script = (
            f"display notification {_applescript_string(message)} "
            f"with title {_applescript_string(title)} "
            f"subtitle {_applescript_string(event.pr_url)}"
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return NotificationSendResult(True)
        return NotificationSendResult(False, result.stderr.strip() or result.stdout.strip())


class RecordingNotifier:
    def __init__(self, result: Optional[NotificationSendResult] = None):
        self.result = result or NotificationSendResult(True)
        self.messages: List[dict] = []

    def send(self, title: str, message: str, event: InboxItem) -> NotificationSendResult:
        self.messages.append({"title": title, "message": message, "event_id": event.event_id})
        return self.result


def notify_events(
    store: StateStore,
    events: Iterable[InboxItem],
    mode: str = "none",
    notifier: Optional[object] = None,
    force: bool = False,
) -> List[NotificationResult]:
    return [notify_event(store, event.event_id, mode=mode, notifier=notifier, force=force) for event in events]


def notify_event(
    store: StateStore,
    event_id: str,
    mode: str = "desktop",
    notifier: Optional[object] = None,
    force: bool = False,
) -> NotificationResult:
    normalized = normalize_notification_mode(mode)
    if normalized == "none":
        return NotificationResult("skipped", event_id, message="notification mode is none")

    event = store.get_event(event_id)
    title, message = render_notification(event)
    delivered: List[str] = []
    failures: List[str] = []
    skipped: List[str] = []

    for channel in channels_for_mode(normalized):
        existing = store.get_notification(event_id, channel)
        if existing and not force:
            skipped.append(channel)
            continue
        if channel == "browser":
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
        result = sender.send(title, message, event)
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
    title = f"PR #{event.pr_number} needs attention"
    message = f"{event.repo_owner}/{event.repo_name}: {event.summary}"
    return title, message


def normalize_notification_mode(mode: str) -> str:
    normalized = (mode or "none").strip().lower()
    if normalized not in VALID_NOTIFICATION_MODES:
        raise ValueError(f"notification mode must be one of: {', '.join(sorted(VALID_NOTIFICATION_MODES))}")
    return normalized


def channels_for_mode(mode: str) -> List[str]:
    normalized = normalize_notification_mode(mode)
    if normalized == "both":
        return ["desktop", "browser"]
    if normalized == "none":
        return []
    return [normalized]


def _applescript_string(value: str) -> str:
    one_line = " ".join(str(value).split())
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'
