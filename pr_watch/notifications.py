from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .models import Binding, InboxItem, NotificationResult
from .state import StateStore


VALID_NOTIFICATION_MODES = {"none", "desktop", "in_app", "both", "auto", "browser"}
APP_HOSTS = {"conductor", "codex-app", "codex_app", "mcp"}
DESKTOP_PRIMARY_HOSTS = {"conductor"}
CONDUCTOR_OPEN_URL = "conductor://open"
CONDUCTOR_DESKTOP_CHANNEL = "desktop_conductor"
PR_WATCH_NOTIFICATION_BUNDLE_ID = "com.pr-watch.notification"
TERMINAL_NOTIFIER_INSTALL_MESSAGE = (
    "terminal-notifier is required for clickable PR Watch desktop notifications; "
    "install it with `brew install terminal-notifier`."
)
PR_WATCH_ICON_FILENAME = "pr-watch-notification.png"
PR_WATCH_APP_BUNDLE_NAME = "PR Watch.app"
PR_WATCH_APP_EXECUTABLE_NAME = "pr-watch-notification"
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
        open_url: Optional[str] = None,
        app_icon: Optional[str] = None,
        sender_bundle_id: Optional[str] = None,
        sender_app_path: Optional[str] = None,
    ) -> NotificationSendResult:
        if platform.system() != "Darwin":
            return NotificationSendResult(False, "desktop notifications currently use macOS osascript")
        terminal_notifier = shutil.which("terminal-notifier")
        if open_url and not terminal_notifier:
            return NotificationSendResult(False, TERMINAL_NOTIFIER_INSTALL_MESSAGE)
        icon_url = app_icon or pr_watch_icon_url()
        if terminal_notifier and (sender_bundle_id or activation_bundle_id or open_url or icon_url):
            if sender_bundle_id and sender_app_path:
                register_app_bundle(Path(sender_app_path))
            command = [
                terminal_notifier,
                "-title",
                title,
                "-message",
                message,
                "-group",
                event.event_id,
            ]
            if sender_bundle_id:
                command.extend(["-sender", sender_bundle_id])
            if icon_url:
                command.extend(["-appIcon", icon_url])
            elif activation_bundle_id:
                command.extend(["-sender", activation_bundle_id])
            if open_url:
                command.extend(["-open", open_url])
            elif activation_bundle_id:
                command.extend(["-activate", activation_bundle_id])
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
        open_url: Optional[str] = None,
        app_icon: Optional[str] = None,
        sender_bundle_id: Optional[str] = None,
        sender_app_path: Optional[str] = None,
    ) -> NotificationSendResult:
        self.messages.append(
            {
                "title": title,
                "message": message,
                "event_id": event.event_id,
                "activation_bundle_id": activation_bundle_id,
                "open_url": open_url,
                "app_icon": app_icon,
                "sender_bundle_id": sender_bundle_id,
                "sender_app_path": sender_app_path,
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
    notify_event_types: Optional[Iterable[str]] = None,
) -> List[NotificationResult]:
    results: List[NotificationResult] = []
    for event in events:
        if not event_type_is_notifiable(event.event_type, notify_event_types):
            results.append(
                NotificationResult(
                    "skipped",
                    event.event_id,
                    message=f"event type {event.event_type} filtered by notify_event_types",
                )
            )
            continue
        results.append(notify_event(store, event.event_id, mode=mode, notifier=notifier, force=force, host=host))
    return results


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
    title, message = render_notification(event, binding)
    state_dir = store.path.parent
    activation_bundle_id = activation_bundle_id_for_binding(binding) if is_conductor_binding(binding) else None
    open_url = None if activation_bundle_id else open_url_for_event(event, binding)
    app_icon = pr_watch_icon_url(state_dir)
    sender_bundle_id, sender_app_path = sender_for_binding(binding, state_dir)
    target_url = CONDUCTOR_OPEN_URL if is_conductor_binding(binding) else open_url or event.pr_url
    delivered: List[str] = []
    failures: List[str] = []
    skipped: List[str] = []

    for channel in channels_for_mode(normalized):
        actual_channel = notification_channel_for_binding(channel, binding)
        existing = store.get_notification(event_id, actual_channel)
        if _notification_blocks_send(existing, force):
            skipped.append(actual_channel)
            continue
        if actual_channel == "in_app":
            store.upsert_notification(
                event_id=event_id,
                channel=actual_channel,
                title=title,
                message=message,
                target_url=event.pr_url,
                status="pending",
            )
            delivered.append(actual_channel)
            continue

        sender = notifier or DesktopNotifier()
        result = sender.send(
            title,
            message,
            event,
            activation_bundle_id=activation_bundle_id,
            open_url=open_url,
            app_icon=app_icon,
            sender_bundle_id=sender_bundle_id,
            sender_app_path=sender_app_path,
        )
        status = "sent" if result.ok else "failed"
        store.upsert_notification(
            event_id=event_id,
            channel=actual_channel,
            title=title,
            message=message,
            target_url=target_url,
            status=status,
            error=result.error,
        )
        if result.ok:
            delivered.append(actual_channel)
        else:
            failures.append(f"{actual_channel}: {result.error}")

    if failures:
        return NotificationResult("failed", event_id, channels=delivered, message="; ".join(failures))
    if delivered:
        return NotificationResult("notified", event_id, channels=delivered)
    return NotificationResult("already_notified", event_id, channels=skipped)


def notify_conductor_session_event(
    store: StateStore,
    event_id: str,
    notifier: Optional[object] = None,
    force: bool = False,
    title: Optional[str] = None,
    message: Optional[str] = None,
) -> NotificationResult:
    event = store.get_event(event_id)
    existing = store.get_notification(event_id, CONDUCTOR_DESKTOP_CHANNEL)
    if _notification_blocks_send(existing, force):
        return NotificationResult("already_notified", event_id, channels=[CONDUCTOR_DESKTOP_CHANNEL])

    default_title, default_message = render_notification(event, store.get_binding(event.binding_id))
    title = title or default_title
    message = message or default_message
    sender = notifier or DesktopNotifier()
    state_dir = store.path.parent
    app_icon = pr_watch_icon_url(state_dir)
    sender_bundle_id, sender_app_path = pr_watch_notification_sender(state_dir)
    result = sender.send(
        title,
        message,
        event,
        activation_bundle_id=HOST_ACTIVATION_BUNDLE_IDS["conductor"],
        app_icon=app_icon,
        sender_bundle_id=sender_bundle_id,
        sender_app_path=sender_app_path,
    )
    status = "sent" if result.ok else "failed"
    store.upsert_notification(
        event_id=event_id,
        channel=CONDUCTOR_DESKTOP_CHANNEL,
        title=title,
        message=message,
        target_url=CONDUCTOR_OPEN_URL,
        status=status,
        error=result.error,
    )
    if result.ok:
        return NotificationResult("notified", event_id, channels=[CONDUCTOR_DESKTOP_CHANNEL])
    return NotificationResult(
        "failed",
        event_id,
        channels=[],
        message=f"{CONDUCTOR_DESKTOP_CHANNEL}: {result.error}",
    )


def _notification_blocks_send(existing: object, force: bool) -> bool:
    return bool(existing and not force and getattr(existing, "status", "") != "failed")


def event_type_is_notifiable(event_type: str, notify_event_types: Optional[Iterable[str]]) -> bool:
    if notify_event_types is None:
        return True
    allowed = {str(item).strip() for item in notify_event_types if str(item).strip()}
    if not allowed:
        return False
    return "*" in allowed or event_type in allowed


def render_notification(event: InboxItem, binding: Optional[Binding] = None) -> tuple[str, str]:
    if is_conductor_binding(binding) and event.status == "needs_confirmation":
        title = f"{event.repo_name} #{event.pr_number} confirm Conductor session"
        message = f"{event.summary} Open Conductor to confirm or ignore."
        return title, message
    title = f"{event.repo_name} #{event.pr_number} needs attention"
    if is_conductor_binding(binding):
        message = f"{event.summary} Open Conductor for the session prompt."
    else:
        message = event.summary
    return title, message


def activation_bundle_id_for_binding(binding: Optional[Binding]) -> Optional[str]:
    if binding is None or not binding.host:
        return None
    return HOST_ACTIVATION_BUNDLE_IDS.get(binding.host.strip().lower())


def sender_for_binding(binding: Optional[Binding], state_dir: Optional[Path | str] = None) -> tuple[Optional[str], Optional[str]]:
    if not is_conductor_binding(binding):
        return None, None
    return pr_watch_notification_sender(state_dir)


def pr_watch_notification_sender(state_dir: Optional[Path | str] = None) -> tuple[str, str]:
    app_path = ensure_pr_watch_notification_app(state_dir)
    return PR_WATCH_NOTIFICATION_BUNDLE_ID, str(app_path)


def notification_channel_for_binding(channel: str, binding: Optional[Binding]) -> str:
    if channel == "desktop" and is_conductor_binding(binding):
        return CONDUCTOR_DESKTOP_CHANNEL
    return channel


def open_url_for_binding(binding: Optional[Binding]) -> Optional[str]:
    if is_conductor_binding(binding):
        return CONDUCTOR_OPEN_URL
    return None


def open_url_for_event(event: InboxItem, binding: Optional[Binding]) -> str:
    return open_url_for_binding(binding) or event.pr_url


def is_conductor_binding(binding: Optional[Binding]) -> bool:
    return binding is not None and (binding.host or "").strip().lower() == "conductor"


def pr_watch_icon_url(state_dir: Optional[Path | str] = None) -> str:
    path = pr_watch_icon_path(state_dir)
    if not path.exists():
        path.write_bytes(_render_pr_watch_png())
    return path.as_uri()


def pr_watch_icon_path(state_dir: Optional[Path | str] = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else Path.home() / ".pr-watch"
    root.mkdir(parents=True, exist_ok=True)
    return root / PR_WATCH_ICON_FILENAME


def pr_watch_notification_app_path(state_dir: Optional[Path | str] = None) -> Path:
    root = Path(state_dir).expanduser() if state_dir else Path.home() / ".pr-watch"
    root.mkdir(parents=True, exist_ok=True)
    return root / PR_WATCH_APP_BUNDLE_NAME


def ensure_pr_watch_notification_app(state_dir: Optional[Path | str] = None) -> Path:
    app_path = pr_watch_notification_app_path(state_dir)
    contents = app_path / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    icon_source = pr_watch_icon_path(state_dir)
    if not icon_source.exists():
        icon_source.write_bytes(_render_pr_watch_png())
    icon_target = resources / PR_WATCH_ICON_FILENAME
    icon_target.write_bytes(icon_source.read_bytes())

    executable = macos / PR_WATCH_APP_EXECUTABLE_NAME
    executable.write_text('#!/bin/sh\nopen "conductor://open"\n', encoding="utf-8")
    executable.chmod(0o755)

    info = {
        "CFBundleDisplayName": "PR Watch",
        "CFBundleExecutable": PR_WATCH_APP_EXECUTABLE_NAME,
        "CFBundleIdentifier": PR_WATCH_NOTIFICATION_BUNDLE_ID,
        "CFBundleName": "PR Watch",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
    }
    with (contents / "Info.plist").open("wb") as file:
        plistlib.dump(info, file)
    (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
    return app_path


def register_app_bundle(app_path: Path) -> None:
    if platform.system() != "Darwin":
        return
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    )
    if not lsregister.exists():
        return
    subprocess.run([str(lsregister), "-f", str(app_path)], capture_output=True, text=True, check=False)


def _render_pr_watch_png(size: int = 128) -> bytes:
    pixels = bytearray()
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            r, g, b, a = _icon_pixel(x, y, size)
            pixels.extend([r, g, b, a])
    raw = bytes(pixels)
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)),
            _png_chunk(b"IDAT", zlib.compress(raw, level=9)),
            _png_chunk(b"IEND", b""),
        ]
    )


def _icon_pixel(x: int, y: int, size: int) -> tuple[int, int, int, int]:
    cx = x - size / 2
    cy = y - size / 2
    radius = (cx * cx + cy * cy) ** 0.5
    if radius > size * 0.46:
        return 0, 0, 0, 0

    bg = (36, 47, 64, 255)
    accent = (72, 201, 176, 255)
    light = (244, 248, 252, 255)

    # Git-style branch glyph: three nodes connected by a stem and fork.
    nodes = [(44, 38), (44, 90), (86, 64)]
    for nx, ny in nodes:
        if (x - nx) ** 2 + (y - ny) ** 2 <= 11**2:
            return light
        if (x - nx) ** 2 + (y - ny) ** 2 <= 17**2:
            return accent
    if 39 <= x <= 49 and 38 <= y <= 90:
        return light
    if 44 <= x <= 86 and 59 <= y <= 69:
        return light
    if 80 <= x <= 90 and 64 <= y <= 90:
        return accent

    if radius > size * 0.40:
        return 23, 33, 47, 255
    return bg


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


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
    if (host or "").strip().lower() in DESKTOP_PRIMARY_HOSTS:
        system = platform_name or platform.system()
        return "desktop" if system == "Darwin" else "in_app"
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
