from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .conductor_adapter import (
    ConductorStatus,
    check_conductor_db,
    confirmation_activity_after_prompt,
    explicit_pr_work_activity_after_event,
    mirror_confirmation_to_conductor,
    mirror_event_to_conductor,
    session_prompt_defer_reason,
    visible_conductor_message_exists,
)
from .delivery import (
    CONFIRMATION_PROMPT_HOST,
    DEFAULT_BUSY_POLICY,
    approve_event,
    confirmation_prompt_event,
    notify_prompt_event,
)
from .host_integration import CONDUCTOR_CODEX_BINARY
from .models import Binding, ClassifiedEvent, InboxItem, PullRequestRef
from .notifications import notify_conductor_session_event
from .state import StateStore


SUPPORTED_HOSTS = {"conductor", "codex-app", "all"}
CONDUCTOR_CONFIRMATION_HOST = "conductor_confirmation"
CONFIRMATION_DELIVERY_STATUSES = {
    "awaiting_first_binding_confirmation",
    "awaiting_rebind_confirmation",
}


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
    notifier: Optional[object] = None,
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
        host_results.extend(
            _sync_conductor(
                store,
                events,
                conductor_db_path,
                notifier=notifier,
                runner=runner,
                session_state=session_state,
                notify_prompt_confirmed=notify_prompt_confirmed,
            )
        )

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
                selected_hosts=selected_hosts,
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
    notifier: Optional[object],
    runner: Optional[object],
    session_state: str,
    notify_prompt_confirmed: bool,
) -> List[HostEventResult]:
    results: List[HostEventResult] = []
    conductor_status = check_conductor_db(conductor_db_path)
    reported_unavailable = False

    for event in events:
        event = store.get_event(event.event_id)
        binding = store.get_binding(event.binding_id)
        if _needs_binding_confirmation(event, binding):
            assert binding is not None
            if conductor_status.available:
                handled = _handle_event_if_session_pr_work_started(
                    store,
                    event,
                    binding,
                    conductor_status.db_path,
                    confirm_binding=True,
                )
                if handled is not None:
                    results.append(handled)
                    continue
                activity_evidence = confirmation_activity_after_prompt(
                    conductor_status.db_path,
                    event,
                    binding,
                )
                if activity_evidence:
                    confirmed = store.confirm_binding(
                        binding.binding_id,
                        source="auto_confirmed_by_session_activity",
                    )
                    _promote_confirmation_events_for_binding(
                        store,
                        confirmed,
                        activity_evidence,
                    )
                    results.append(
                        HostEventResult(
                            host="conductor",
                            event_id=event.event_id,
                            action="auto_confirmed_by_session_activity",
                            target_id=confirmed.session_id,
                            message=activity_evidence,
                        )
                    )
                    event = store.get_event(event.event_id)
                    binding = store.get_binding(event.binding_id)
                else:
                    existing_confirmation = store.get_host_sync(
                        event.event_id,
                        CONDUCTOR_CONFIRMATION_HOST,
                        binding.session_id,
                    ) or store.find_host_sync_for_binding(
                        binding.binding_id,
                        CONDUCTOR_CONFIRMATION_HOST,
                        binding.session_id,
                    )
                    if existing_confirmation is not None and visible_conductor_message_exists(
                        conductor_status.db_path,
                        binding.session_id,
                        existing_confirmation.external_id,
                    ):
                        _notify_conductor_session_event(store, event, notifier)
                        store.upsert_host_sync(
                            event.event_id,
                            CONDUCTOR_CONFIRMATION_HOST,
                            binding.session_id,
                            "confirmation_already_requested",
                            external_id=existing_confirmation.external_id,
                            error=existing_confirmation.error,
                        )
                        results.append(
                            HostEventResult(
                                host="conductor",
                                event_id=event.event_id,
                                action="confirmation_already_requested",
                                target_id=binding.session_id,
                                message="binding confirmation request already recorded; leaving event pending in inbox",
                            )
                        )
                        continue
                    defer_reason = session_prompt_defer_reason(conductor_status.db_path, binding)
                    if defer_reason:
                        _notify_conductor_session_event(store, event, notifier)
                        results.append(
                            HostEventResult(
                                host="conductor",
                                event_id=event.event_id,
                                action="deferred_session_busy",
                                target_id=binding.session_id,
                                message=defer_reason,
                            )
                        )
                        continue
                    mirrored = mirror_confirmation_to_conductor(conductor_status.db_path, event, binding)
                    if mirrored.action in {"confirmation_requested", "confirmation_already_requested"}:
                        store.upsert_host_sync(
                            event.event_id,
                            CONDUCTOR_CONFIRMATION_HOST,
                            binding.session_id,
                            mirrored.action,
                            external_id=mirrored.message_id,
                        )
                        _notify_conductor_session_event(store, event, notifier)
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

            if binding is not None and binding.confirmed:
                event = store.get_event(event.event_id)
                binding = store.get_binding(event.binding_id)
            if not _has_confirmed_binding(event, binding):
                assert binding is not None
                existing_confirmation = store.get_host_sync(
                    event.event_id,
                    CONDUCTOR_CONFIRMATION_HOST,
                    binding.session_id,
                ) or store.find_host_sync_for_binding(
                    binding.binding_id,
                    CONDUCTOR_CONFIRMATION_HOST,
                    binding.session_id,
                )
                if existing_confirmation is not None:
                    store.upsert_host_sync(
                        event.event_id,
                        CONDUCTOR_CONFIRMATION_HOST,
                        binding.session_id,
                        "confirmation_already_requested",
                        external_id=existing_confirmation.external_id,
                        error=existing_confirmation.error,
                    )
                    results.append(
                        HostEventResult(
                            host="conductor",
                            event_id=event.event_id,
                            action="confirmation_already_requested",
                            target_id=binding.session_id,
                            message="binding confirmation request already recorded; leaving event pending in inbox",
                        )
                    )
                    continue
                existing_prompt = store.get_host_sync(
                    event.event_id,
                    CONFIRMATION_PROMPT_HOST,
                    binding.session_id,
                )
                if existing_prompt is not None and existing_prompt.status == "failed":
                    existing_prompt = None
                if existing_prompt is None:
                    existing_prompt = store.find_host_sync_for_binding(
                        binding.binding_id,
                        CONFIRMATION_PROMPT_HOST,
                        binding.session_id,
                    )
                if existing_prompt is not None:
                    store.upsert_host_sync(
                        event.event_id,
                        CONFIRMATION_PROMPT_HOST,
                        binding.session_id,
                        existing_prompt.status,
                        external_id=existing_prompt.external_id,
                        error=existing_prompt.error,
                    )
                    results.append(
                        HostEventResult(
                            host="conductor",
                            event_id=event.event_id,
                            action=_confirmation_prompt_dedupe_action(existing_prompt.status),
                            target_id=binding.session_id,
                            message="binding confirmation prompt already recorded; leaving event pending in inbox",
                        )
                    )
                    continue

                delivered = confirmation_prompt_event(
                    store,
                    event.event_id,
                    runner=runner,
                    session_state=session_state,
                    codex_binary=_conductor_codex_binary(),
                )
                if delivered.action != "confirmation_prompt_skipped":
                    results.append(
                        HostEventResult(
                            host="conductor",
                            event_id=event.event_id,
                            action=delivered.action,
                            target_id=binding.session_id,
                            message=delivered.message,
                        )
                    )
                    continue
                continue
        confirmed_bindings = _confirmed_bindings_for_event(store, event, binding)
        if not confirmed_bindings:
            continue
        if not conductor_status.available:
            if not reported_unavailable:
                results.append(
                    HostEventResult(
                        host="conductor",
                        event_id="",
                        action=conductor_status.status,
                        message=conductor_status.message,
                    )
                )
                reported_unavailable = True
            continue
        handled = _handle_first_event_handled_by_session_pr_work(
            store,
            event,
            confirmed_bindings,
            conductor_status.db_path,
        )
        if handled is not None:
            results.append(handled)
            continue
        for confirmed_binding in confirmed_bindings:
            defer_reason = session_prompt_defer_reason(conductor_status.db_path, confirmed_binding)
            if defer_reason:
                _notify_conductor_session_event(store, event, notifier)
                results.append(
                    HostEventResult(
                        host="conductor",
                        event_id=event.event_id,
                        action="deferred_session_busy",
                        target_id=confirmed_binding.session_id,
                        message=defer_reason,
                    )
                )
                continue
            mirrored = mirror_event_to_conductor(conductor_status.db_path, event, confirmed_binding)
            target_id = mirrored.session_id or confirmed_binding.session_id
            if mirrored.action in {"mirrored", "already_synced"}:
                store.upsert_host_sync(
                    event.event_id,
                    "conductor",
                    target_id,
                    mirrored.action,
                    external_id=mirrored.message_id,
                )
                _notify_conductor_session_event(store, event, notifier)
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


def _handle_first_event_handled_by_session_pr_work(
    store: StateStore,
    event: InboxItem,
    bindings: Iterable[Binding],
    conductor_db_path: Optional[str | Path],
) -> Optional[HostEventResult]:
    for binding in bindings:
        handled = _handle_event_if_session_pr_work_started(
            store,
            event,
            binding,
            conductor_db_path,
            confirm_binding=False,
        )
        if handled is not None:
            return handled
    return None


def _handle_event_if_session_pr_work_started(
    store: StateStore,
    event: InboxItem,
    binding: Binding,
    conductor_db_path: Optional[str | Path],
    confirm_binding: bool,
) -> Optional[HostEventResult]:
    activity_evidence = explicit_pr_work_activity_after_event(conductor_db_path, event, binding)
    if not activity_evidence:
        return None

    effective_binding = binding
    if confirm_binding and not binding.confirmed:
        effective_binding = store.confirm_binding(
            binding.binding_id,
            source="auto_handled_by_session_activity",
        )

    store.update_event(
        event.event_id,
        status="dismissed",
        delivery_status="handled_by_session_activity",
        binding_id=effective_binding.binding_id,
        confidence="high",
        evidence=event.evidence
        + [
            "already being handled by explicit PR work in bound Conductor session",
            activity_evidence,
        ],
        recovery_command="",
        error=activity_evidence,
    )
    return HostEventResult(
        host="conductor",
        event_id=event.event_id,
        action="marked_handled_by_session_activity",
        target_id=effective_binding.session_id,
        message=activity_evidence,
    )


def _notify_conductor_session_event(
    store: StateStore,
    event: InboxItem,
    notifier: Optional[object],
) -> None:
    notify_conductor_session_event(store, event.event_id, notifier=notifier)


def _confirmed_bindings_for_event(
    store: StateStore,
    event: InboxItem,
    event_binding: Optional[Binding],
) -> List[Binding]:
    if not (
        event.status == "pending"
        and event.confidence == "high"
    ):
        return []

    confirmed = store.list_confirmed_bindings(
        ClassifiedEvent(
            pr=event_pr_ref(event),
            role=event.role,
            event_type=event.event_type,
            summary=event.summary,
            actor=event.actor,
            actionable=event.actionable,
            dedupe_key=event.dedupe_key,
            payload=event.payload,
        )
    )
    if confirmed:
        return confirmed
    if _has_confirmed_binding(event, event_binding):
        assert event_binding is not None
        return [event_binding]
    return []


def event_pr_ref(event: InboxItem) -> PullRequestRef:
    return PullRequestRef(
        owner=event.repo_owner,
        repo=event.repo_name,
        number=event.pr_number,
        url=event.pr_url,
    )


def _promote_confirmation_events_for_binding(
    store: StateStore,
    confirmed: Binding,
    activity_evidence: str,
) -> None:
    for item in store.list_events(include_done=False):
        if item.binding_id != confirmed.binding_id:
            continue
        if item.status != "needs_confirmation" or item.delivery_status not in CONFIRMATION_DELIVERY_STATUSES:
            continue
        store.update_event(
            item.event_id,
            status="pending",
            delivery_status="awaiting_approval",
            binding_id=confirmed.binding_id,
            confidence="high",
            evidence=item.evidence
            + [
                f"same-session PR activity confirmed active binding {confirmed.agent}:{confirmed.session_id}",
                activity_evidence,
            ],
            recovery_command="",
            error="",
        )


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
    selected_hosts: set[str],
) -> List[HostTriggerResult]:
    results: List[HostTriggerResult] = []
    for event in events:
        if event.event_id in mirrored_event_ids:
            continue
        binding = store.get_binding(event.binding_id)
        delivery = notify_prompt_event(
            store,
            event.event_id,
            runner=runner,
            session_state=session_state,
            codex_binary=_notify_prompt_codex_binary(binding, selected_hosts),
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
        and event.delivery_status in CONFIRMATION_DELIVERY_STATUSES
    )


def _is_confirmed_trigger_candidate(event: InboxItem, binding: Optional[Binding]) -> bool:
    return (
        _has_confirmed_binding(event, binding)
        and event.delivery_status == "awaiting_approval"
        and binding is not None
        and binding.confidence == "high"
    )


def _conductor_codex_binary() -> str:
    return str(CONDUCTOR_CODEX_BINARY) if Path(CONDUCTOR_CODEX_BINARY).expanduser().exists() else "codex"


def _notify_prompt_codex_binary(binding: Optional[Binding], selected_hosts: set[str]) -> Optional[str]:
    if binding is None or binding.agent != "codex" or "conductor" not in selected_hosts:
        return None
    if selected_hosts == {"conductor"} or binding.host == "conductor":
        return _conductor_codex_binary()
    return None


def _confirmation_prompt_dedupe_action(status: str) -> str:
    if status == "sent":
        return "confirmation_prompt_already_sent"
    if status == "queued":
        return "confirmation_prompt_already_queued"
    return "confirmation_prompt_already_attempted"
