from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .conductor_adapter import (
    ConductorStatus,
    check_conductor_db,
    mirror_confirmation_to_conductor,
    mirror_event_to_conductor,
)
from .delivery import DEFAULT_BUSY_POLICY, approve_event, notify_prompt_event
from .models import Binding, InboxItem
from .state import StateStore


SUPPORTED_HOSTS = {"conductor", "codex-app", "all"}


@dataclass(frozen=True)
class CodexAppStatus:
    status: str = "no_push_support"
    available: bool = False
    message: str = (
        "MCP registration exposes pr-watch inbox tools, but Codex App does not expose "
        "a stable local badge, popup, or session-push API."
    )


@dataclass(frozen=True)
class HostStatus:
    conductor: ConductorStatus
    codex_app: CodexAppStatus = field(default_factory=CodexAppStatus)


@dataclass(frozen=True)
class HostEventResult:
    host: str
    event_id: str
    action: str
    target_id: str = ""
    message: str = ""


@dataclass(frozen=True)
class HostTriggerResult:
    event_id: str
    action: str
    message: str = ""


@dataclass(frozen=True)
class HostSyncResult:
    host_results: List[HostEventResult] = field(default_factory=list)
    trigger_results: List[HostTriggerResult] = field(default_factory=list)
    notify_prompt_results: List[HostTriggerResult] = field(default_factory=list)
    codex_app: CodexAppStatus = field(default_factory=CodexAppStatus)


def status(conductor_db_path: Optional[str | Path] = None) -> HostStatus:
    return HostStatus(conductor=check_conductor_db(conductor_db_path))


def sync_once(
    store: StateStore,
    hosts: Iterable[str] = ("all",),
    conductor_db_path: Optional[str | Path] = None,
    trigger_confirmed: bool = False,
    notify_prompt_confirmed: bool = False,
    runner: Optional[object] = None,
    session_state: str = "unknown",
    busy_policy: str = DEFAULT_BUSY_POLICY,
    event_ids: Optional[Iterable[str]] = None,
) -> HostSyncResult:
    selected_hosts = _normalize_hosts(hosts)
    events = store.list_events(include_done=False)
    if event_ids is not None:
        selected_event_ids = set(event_ids)
        events = [event for event in events if event.event_id in selected_event_ids]
    host_results: List[HostEventResult] = []

    if "conductor" in selected_hosts:
        host_results.extend(_sync_conductor(store, events, conductor_db_path))

    mirrored_event_ids = {
        item.event_id
        for item in host_results
        if item.action in {"mirrored", "already_synced"} and item.event_id
    }

    trigger_results: List[HostTriggerResult] = []
    if trigger_confirmed:
        trigger_results.extend(
            _trigger_confirmed_events(
                store,
                events,
                runner=runner,
                session_state=session_state,
                busy_policy=busy_policy,
            )
        )

    notify_prompt_results: List[HostTriggerResult] = []
    if notify_prompt_confirmed:
        notify_prompt_results.extend(
            _notify_prompt_confirmed_events(
                store,
                events,
                mirrored_event_ids=mirrored_event_ids,
                runner=runner,
                session_state=session_state,
            )
        )

    return HostSyncResult(
        host_results=host_results,
        trigger_results=trigger_results,
        notify_prompt_results=notify_prompt_results,
    )


def _normalize_hosts(hosts: Iterable[str]) -> set[str]:
    normalized = {host.strip().lower() for host in hosts if host and host.strip()}
    if not normalized:
        return set()
    unknown = normalized - SUPPORTED_HOSTS
    if unknown:
        raise ValueError("unsupported host(s): " + ", ".join(sorted(unknown)))
    if "all" in normalized:
        return {"conductor", "codex-app"}
    return normalized


def _sync_conductor(
    store: StateStore,
    events: Iterable[InboxItem],
    conductor_db_path: Optional[str | Path],
) -> List[HostEventResult]:
    results: List[HostEventResult] = []
    conductor_status = check_conductor_db(conductor_db_path)
    if not conductor_status.available:
        return [
            HostEventResult(
                host="conductor",
                event_id="",
                action=conductor_status.status,
                message=conductor_status.message,
            )
        ]

    for event in events:
        binding = store.get_binding(event.binding_id)
        if _needs_binding_confirmation(event, binding):
            assert binding is not None
            existing = store.get_host_sync(event.event_id, "conductor_confirmation", binding.session_id)
            if existing:
                continue
            mirrored = mirror_confirmation_to_conductor(conductor_status.db_path, event, binding)
            if mirrored.action in {"confirmation_requested", "confirmation_already_requested"}:
                store.upsert_host_sync(
                    event.event_id,
                    "conductor_confirmation",
                    binding.session_id,
                    mirrored.action,
                    external_id=mirrored.message_id,
                )
            results.append(
                HostEventResult(
                    host="conductor",
                    event_id=event.event_id,
                    action=mirrored.action,
                    target_id=mirrored.session_id or binding.session_id,
                    message=mirrored.message,
                )
            )
            continue
        if not _has_confirmed_binding(event, binding):
            continue
        assert binding is not None
        existing = store.get_host_sync(event.event_id, "conductor", binding.session_id)
        if existing:
            results.append(
                HostEventResult(
                    host="conductor",
                    event_id=event.event_id,
                    action="already_synced",
                    target_id=binding.session_id,
                    message="event already synced to this host target",
                )
            )
            continue

        mirrored = mirror_event_to_conductor(conductor_status.db_path, event, binding)
        target_id = mirrored.session_id or binding.session_id
        if mirrored.action in {"mirrored", "already_synced"}:
            store.upsert_host_sync(
                event.event_id,
                "conductor",
                target_id,
                mirrored.action,
                external_id=mirrored.message_id,
            )
        results.append(
            HostEventResult(
                host="conductor",
                event_id=event.event_id,
                action=mirrored.action,
                target_id=target_id,
                message=mirrored.message,
            )
        )
    return results


def _trigger_confirmed_events(
    store: StateStore,
    events: Iterable[InboxItem],
    runner: Optional[object],
    session_state: str,
    busy_policy: str,
) -> List[HostTriggerResult]:
    results: List[HostTriggerResult] = []
    for event in events:
        binding = store.get_binding(event.binding_id)
        if not _is_confirmed_trigger_candidate(event, binding):
            continue
        delivery = approve_event(
            store,
            event.event_id,
            runner=runner,
            session_state=session_state,
            busy_policy=busy_policy,
        )
        results.append(HostTriggerResult(event.event_id, delivery.action, delivery.message))
    return results


def _notify_prompt_confirmed_events(
    store: StateStore,
    events: Iterable[InboxItem],
    mirrored_event_ids: set[str],
    runner: Optional[object],
    session_state: str,
) -> List[HostTriggerResult]:
    results: List[HostTriggerResult] = []
    for event in events:
        if event.event_id in mirrored_event_ids:
            continue
        delivery = notify_prompt_event(
            store,
            event.event_id,
            runner=runner,
            session_state=session_state,
        )
        if delivery.action == "notify_prompt_skipped":
            continue
        results.append(HostTriggerResult(event.event_id, delivery.action, delivery.message))
    return results


def _has_confirmed_binding(event: InboxItem, binding: Optional[Binding]) -> bool:
    return (
        binding is not None
        and binding.confirmed
        and binding.active
        and event.binding_id == binding.binding_id
        and event.status == "pending"
        and event.confidence == "high"
    )


def _needs_binding_confirmation(event: InboxItem, binding: Optional[Binding]) -> bool:
    return (
        binding is not None
        and not binding.confirmed
        and event.status == "needs_confirmation"
        and event.confidence == "high"
        and event.delivery_status
        in {"awaiting_first_binding_confirmation", "awaiting_rebind_confirmation"}
    )


def _is_confirmed_trigger_candidate(event: InboxItem, binding: Optional[Binding]) -> bool:
    return (
        _has_confirmed_binding(event, binding)
        and event.delivery_status == "awaiting_approval"
        and binding is not None
        and binding.confidence == "high"
    )
