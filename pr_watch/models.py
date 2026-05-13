from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repo: str
    number: int
    url: str
    title: str = ""
    head_ref: str = ""

    @property
    def repo_full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class ClassifiedEvent:
    pr: PullRequestRef
    role: str
    event_type: str
    summary: str
    actor: str
    actionable: bool
    dedupe_key: str
    occurred_at: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionInfo:
    agent: str
    session_id: str
    title: str = ""
    cwd: str = ""
    branch: str = ""
    text: str = ""
    host: Optional[str] = None
    last_activity_at: str = ""


@dataclass(frozen=True)
class Binding:
    binding_id: str
    repo_owner: str
    repo_name: str
    pr_number: int
    pr_url: str
    role: str
    agent: str
    session_id: str
    cwd: str = ""
    branch: str = ""
    host: Optional[str] = None
    confidence: str = "high"
    confirmed: bool = False
    confirmation_source: str = "inferred_candidate"
    evidence: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_event_at: str = ""


@dataclass(frozen=True)
class InboxItem:
    event_id: str
    dedupe_key: str
    repo_owner: str
    repo_name: str
    pr_number: int
    pr_url: str
    role: str
    event_type: str
    summary: str
    actor: str
    actionable: bool
    status: str
    delivery_status: str
    binding_id: Optional[str]
    confidence: str
    evidence: List[str] = field(default_factory=list)
    payload: Dict[str, Any] = field(default_factory=dict)
    recovery_command: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class QueueItem:
    queue_id: str
    event_id: str
    command: List[str]
    prompt: str
    status: str
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class NotificationItem:
    notification_id: str
    event_id: str
    channel: str
    title: str
    message: str
    target_url: str
    status: str
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class HostSyncItem:
    sync_id: str
    event_id: str
    host: str
    target_id: str
    status: str
    external_id: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DeliveryResult:
    action: str
    event_id: str
    command: List[str] = field(default_factory=list)
    message: str = ""


@dataclass(frozen=True)
class NotificationResult:
    action: str
    event_id: str
    channels: List[str] = field(default_factory=list)
    message: str = ""
