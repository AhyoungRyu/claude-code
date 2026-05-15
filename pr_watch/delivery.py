from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional

from .models import Binding, DeliveryResult, InboxItem
from .state import StateStore
from .util import shell_join


DEFAULT_BUSY_POLICY = "run_if_idle_queue_if_busy"
NOTIFY_PROMPT_HOST = "notify_prompt"
CONFIRMATION_PROMPT_HOST = "conductor_confirmation_prompt"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner:
    def run(self, command: List[str]) -> CommandResult:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class RecordingRunner:
    def __init__(self, result: Optional[CommandResult] = None):
        self.result = result or CommandResult(0, "", "")
        self.commands: List[List[str]] = []

    def run(self, command: List[str]) -> CommandResult:
        self.commands.append(command)
        return self.result


def approve_event(
    store: StateStore,
    event_id: str,
    session_state: str = "unknown",
    runner: Optional[object] = None,
    busy_policy: str = DEFAULT_BUSY_POLICY,
) -> DeliveryResult:
    event = store.get_event(event_id)
    binding = store.get_binding(event.binding_id)
    if binding is None:
        updated = store.update_event(
            event_id,
            status="pending",
            delivery_status="needs_binding",
            error="No confirmed session binding is available for this event.",
        )
        return DeliveryResult("needs_binding", updated.event_id, message=updated.error)

    if not binding.confirmed:
        binding = store.confirm_binding(binding.binding_id)

    prompt = render_resume_prompt(event)
    command = resume_command(binding, prompt)

    normalized_state = session_state if session_state in {"idle", "working", "unknown"} else "unknown"
    if busy_policy == "always_queue" or (
        busy_policy == DEFAULT_BUSY_POLICY and normalized_state != "idle"
    ):
        store.enqueue(event_id, command, prompt)
        store.update_event(
            event_id,
            status="queued",
            delivery_status="queued",
            recovery_command=shell_join(command),
            error="",
        )
        return DeliveryResult("queued", event_id, command=command)

    if busy_policy == "notify_only":
        store.update_event(
            event_id,
            status="pending",
            delivery_status="notify_only",
            recovery_command=shell_join(command),
        )
        return DeliveryResult("notify_only", event_id, command=command)

    if busy_policy == "drop_if_busy" and normalized_state != "idle":
        store.update_event(event_id, status="dropped", delivery_status="dropped")
        return DeliveryResult("dropped", event_id, command=command)

    if busy_policy == "ask_when_busy" and normalized_state != "idle":
        store.update_event(
            event_id,
            status="pending",
            delivery_status="busy_needs_decision",
            recovery_command=shell_join(command),
        )
        return DeliveryResult("busy_needs_decision", event_id, command=command)

    actual_runner = runner or SubprocessRunner()
    result = actual_runner.run(command)
    if result.returncode == 0:
        store.update_event(event_id, status="delivered", delivery_status="delivered", error="")
        return DeliveryResult("delivered", event_id, command=command)

    error = result.stderr.strip() or result.stdout.strip() or f"resume command exited {result.returncode}"
    store.update_event(
        event_id,
        status="pending",
        delivery_status="failed",
        recovery_command=shell_join(command),
        error=error,
    )
    return DeliveryResult("failed", event_id, command=command, message=error)


def notify_prompt_event(
    store: StateStore,
    event_id: str,
    session_state: str = "unknown",
    runner: Optional[object] = None,
    codex_binary: Optional[str] = None,
) -> DeliveryResult:
    event = store.get_event(event_id)
    binding = store.get_binding(event.binding_id)
    skip_reason = notify_prompt_skip_reason(event, binding)
    if skip_reason:
        return DeliveryResult("notify_prompt_skipped", event_id, message=skip_reason)

    assert binding is not None
    existing = store.get_host_sync(event.event_id, NOTIFY_PROMPT_HOST, binding.session_id)
    if existing:
        return DeliveryResult(
            _notify_prompt_dedupe_action(existing.status),
            event_id,
            message="notify prompt already recorded for this event/session",
        )

    prompt = render_notify_prompt(event)
    command = resume_command(binding, prompt, codex_binary=codex_binary)
    normalized_state = session_state if session_state in {"idle", "working", "unknown"} else "unknown"
    if normalized_state != "idle":
        store.enqueue(event_id, command, prompt)
        store.upsert_host_sync(event_id, NOTIFY_PROMPT_HOST, binding.session_id, "queued")
        return DeliveryResult("notify_prompt_queued", event_id, command=command)

    actual_runner = runner or SubprocessRunner()
    result = actual_runner.run(command)
    if result.returncode == 0:
        store.upsert_host_sync(event_id, NOTIFY_PROMPT_HOST, binding.session_id, "sent")
        return DeliveryResult("notify_prompt_sent", event_id, command=command)

    error = result.stderr.strip() or result.stdout.strip() or f"resume command exited {result.returncode}"
    store.upsert_host_sync(event_id, NOTIFY_PROMPT_HOST, binding.session_id, "failed", error=error)
    return DeliveryResult("notify_prompt_failed", event_id, command=command, message=error)


def confirmation_prompt_event(
    store: StateStore,
    event_id: str,
    session_state: str = "unknown",
    runner: Optional[object] = None,
    codex_binary: Optional[str] = None,
) -> DeliveryResult:
    event = store.get_event(event_id)
    binding = store.get_binding(event.binding_id)
    skip_reason = confirmation_prompt_skip_reason(event, binding)
    if skip_reason:
        return DeliveryResult("confirmation_prompt_skipped", event_id, message=skip_reason)

    assert binding is not None
    existing = store.get_host_sync(event.event_id, CONFIRMATION_PROMPT_HOST, binding.session_id)
    if existing and existing.status != "failed":
        return DeliveryResult(
            _confirmation_prompt_dedupe_action(existing.status),
            event_id,
            message="confirmation prompt already recorded for this event/session",
        )

    prompt = render_confirmation_prompt(event, binding)
    command = resume_command(binding, prompt, codex_binary=codex_binary)
    normalized_state = session_state if session_state in {"idle", "working", "unknown"} else "unknown"
    if normalized_state != "idle":
        store.enqueue(event_id, command, prompt)
        store.upsert_host_sync(event_id, CONFIRMATION_PROMPT_HOST, binding.session_id, "queued")
        return DeliveryResult("confirmation_prompt_queued", event_id, command=command)

    actual_runner = runner or SubprocessRunner()
    result = actual_runner.run(command)
    if result.returncode == 0:
        store.upsert_host_sync(event_id, CONFIRMATION_PROMPT_HOST, binding.session_id, "sent")
        return DeliveryResult("confirmation_prompt_sent", event_id, command=command)

    error = result.stderr.strip() or result.stdout.strip() or f"resume command exited {result.returncode}"
    store.upsert_host_sync(event_id, CONFIRMATION_PROMPT_HOST, binding.session_id, "failed", error=error)
    return DeliveryResult("confirmation_prompt_failed", event_id, command=command, message=error)


def notify_prompt_skip_reason(event: InboxItem, binding: Optional[Binding]) -> str:
    if event.status != "pending" or event.delivery_status != "awaiting_approval":
        return "notify prompt requires a pending event awaiting approval"
    if event.confidence != "high":
        return "notify prompt requires a high-confidence event"
    if binding is None or event.binding_id != binding.binding_id:
        return "notify prompt requires an active confirmed session binding"
    if not binding.confirmed:
        return "notify prompt does not confirm inferred or rebind candidates"
    if not binding.active:
        return "notify prompt requires the active binding"
    if binding.confidence != "high":
        return "notify prompt requires a high-confidence binding"
    return ""


def confirmation_prompt_skip_reason(event: InboxItem, binding: Optional[Binding]) -> str:
    if event.status != "needs_confirmation":
        return "confirmation prompt requires a needs_confirmation event"
    if event.delivery_status not in {"awaiting_first_binding_confirmation", "awaiting_rebind_confirmation"}:
        return "confirmation prompt requires a binding-confirmation delivery status"
    if event.confidence != "high":
        return "confirmation prompt requires a high-confidence event"
    if binding is None or event.binding_id != binding.binding_id:
        return "confirmation prompt requires a candidate session binding"
    if binding.confirmed:
        return "confirmation prompt is only for unconfirmed candidate bindings"
    if binding.confidence != "high":
        return "confirmation prompt requires a high-confidence binding"
    return ""


def resume_command(binding: Binding, prompt: str, codex_binary: Optional[str] = None) -> List[str]:
    if binding.agent == "claude":
        return ["claude", "--resume", binding.session_id, prompt]
    if binding.agent == "codex":
        return [codex_binary or "codex", "exec", "resume", binding.session_id, prompt]
    raise ValueError(f"unsupported agent: {binding.agent}")


def render_notify_prompt(event: InboxItem) -> str:
    return "\n".join(
        [
            f"PR Watch: PR #{event.pr_number} has an update",
            "",
            f"{event.actor}: {event.summary}",
            f"Repo: {event.repo_owner}/{event.repo_name}#{event.pr_number}",
            f"Link: {event.pr_url}",
            "",
            "Suggested replies:",
            "- Inspect update",
            "- Queue for later",
            "- Ignore this update",
            "",
            f"Event id: {event.event_id}",
            "",
            "Do not run tools or read files unless the user chooses Inspect update; wait for the user's choice before inspecting files, calling GitHub, editing, commenting, or pushing.",
        ]
    )


def render_confirmation_prompt(event: InboxItem, binding: Binding) -> str:
    return "\n".join(
        [
            f"PR Watch: Is this the right session for PR {event.repo_owner}/{event.repo_name}#{event.pr_number}?",
            "",
            event.summary,
            "",
            "Suggested replies:",
            "- Confirm this session",
            "- Confirm and mark handled",
            "- Not this session",
            "- Ignore this update",
            "",
            "Do not run tools or read files unless the user chooses Confirm this session or Confirm and mark handled; wait for the user's choice before inspecting files, calling GitHub, editing, commenting, or pushing.",
        ]
    )


def render_resume_prompt(event: InboxItem) -> str:
    if event.role == "author":
        heading = f"Your PR #{event.pr_number} has new feedback."
        suggested = "inspect the feedback and propose an action plan before editing"
    else:
        heading = f"PR #{event.pr_number} was updated after your review."
        suggested = "inspect the new activity and decide whether follow-up is needed"
    return "\n\n".join(
        [
            heading,
            f"Event: {event.summary}",
            f"Role: {event.role}",
            f"Repo: {event.repo_owner}/{event.repo_name}",
            f"Suggested next step: {suggested}.",
            "Please summarize what changed and ask before posting any GitHub comment.",
        ]
    )


def _notify_prompt_dedupe_action(status: str) -> str:
    if status == "sent":
        return "notify_prompt_already_sent"
    if status == "queued":
        return "notify_prompt_already_queued"
    return "notify_prompt_already_attempted"


def _confirmation_prompt_dedupe_action(status: str) -> str:
    if status == "sent":
        return "confirmation_prompt_already_sent"
    if status == "queued":
        return "confirmation_prompt_already_queued"
    return "confirmation_prompt_already_attempted"
