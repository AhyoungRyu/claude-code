from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional

from .models import Binding, DeliveryResult, InboxItem
from .state import StateStore
from .util import shell_join


DEFAULT_BUSY_POLICY = "run_if_idle_queue_if_busy"


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


def resume_command(binding: Binding, prompt: str) -> List[str]:
    if binding.agent == "claude":
        return ["claude", "--resume", binding.session_id, prompt]
    if binding.agent == "codex":
        return ["codex", "resume", binding.session_id, prompt]
    raise ValueError(f"unsupported agent: {binding.agent}")


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
