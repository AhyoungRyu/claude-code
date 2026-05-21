from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import Binding, ClassifiedEvent, InboxItem, PullRequestRef, SessionInfo
from .state import StateStore
from .util import parse_pr_url, stable_id


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
CONFIRMATION_DELIVERY_STATUSES = {
    "awaiting_first_binding_confirmation",
    "awaiting_rebind_confirmation",
}
STALE_SESSION_RECENCY_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class SessionCandidate:
    confidence: str
    session: SessionInfo
    evidence: List[str]
    rank: int
    active_rank: int
    host_rank: int
    focus_score: int
    evidence_score: int
    activity_ts: float

    @property
    def decisive_key(self) -> Tuple[int, int, int, int, int, float]:
        return (
            self.rank,
            self.active_rank,
            self.host_rank,
            self.focus_score,
            self.evidence_score,
            self.activity_ts,
        )


@dataclass(frozen=True)
class SessionCandidateSelection:
    candidate: Optional[SessionCandidate]
    ambiguous_candidates: List[SessionCandidate]


def create_explicit_binding(
    store: StateStore,
    pr: str,
    role: str,
    agent: str,
    session_id: str,
    cwd: str = "",
    branch: str = "",
    host: Optional[str] = None,
    repo: Optional[str] = None,
) -> Binding:
    pr_ref = parse_pr_reference(pr, repo=repo)
    binding = store.create_binding(
        repo_owner=pr_ref.owner,
        repo_name=pr_ref.repo,
        pr_number=pr_ref.number,
        pr_url=pr_ref.url,
        role=role,
        agent=agent,
        session_id=session_id,
        cwd=cwd,
        branch=branch,
        host=host,
        confidence="high",
        confirmed=True,
        confirmation_source="explicit_bind",
        evidence=["explicit user binding"],
    )
    return binding


def confirm_binding_for_event(
    store: StateStore,
    event_id: str,
    session_id: Optional[str] = None,
    mirror_now: bool = True,
    trigger: bool = False,
    host: str = "conductor",
    conductor_db_path: Optional[str | Path] = None,
    session_state: str = "unknown",
    busy_policy: Optional[str] = None,
    runner: Optional[object] = None,
) -> Dict[str, Any]:
    event = store.get_event(event_id)
    binding = binding_for_confirmation(store, event, session_id=session_id)
    source = "user_confirmed_rebind" if event.delivery_status == "awaiting_rebind_confirmation" else "user_confirmed"
    confirmed = store.confirm_binding(binding.binding_id, source=source)
    promoted = promote_confirmation_events_for_binding(store, confirmed)
    updated = promoted.get(event.event_id) or store.update_event(
        event.event_id,
        status="pending",
        delivery_status="awaiting_approval",
        binding_id=confirmed.binding_id,
        confidence="high",
        evidence=event.evidence + [f"user confirmed active binding {confirmed.agent}:{confirmed.session_id}"],
        recovery_command="",
        error="",
    )

    host_sync = None
    if mirror_now:
        from .host_adapter import sync_once

        host_sync = sync_once(
            store,
            hosts=[host],
            conductor_db_path=conductor_db_path,
            trigger_confirmed=False,
            event_ids=[event.event_id],
        )

    trigger_result = None
    if trigger:
        from .delivery import DEFAULT_BUSY_POLICY, approve_event

        trigger_result = approve_event(
            store,
            event.event_id,
            session_state=session_state,
            busy_policy=busy_policy or DEFAULT_BUSY_POLICY,
            runner=runner,
        )

    return {
        "action": "confirmed_binding",
        "event": updated,
        "binding": confirmed,
        "host_sync": host_sync,
        "trigger": trigger_result,
    }


def promote_confirmation_events_for_binding(store: StateStore, confirmed: Binding) -> Dict[str, InboxItem]:
    promoted: Dict[str, InboxItem] = {}
    for item in store.list_events(include_done=False):
        if item.binding_id != confirmed.binding_id:
            continue
        if item.status != "needs_confirmation" or item.delivery_status not in CONFIRMATION_DELIVERY_STATUSES:
            continue
        promoted[item.event_id] = store.update_event(
            item.event_id,
            status="pending",
            delivery_status="awaiting_approval",
            binding_id=confirmed.binding_id,
            confidence="high",
            evidence=item.evidence + [f"user confirmed active binding {confirmed.agent}:{confirmed.session_id}"],
            recovery_command="",
            error="",
        )
    return promoted


def confirm_binding_and_mark_handled(
    store: StateStore,
    event_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    event = store.get_event(event_id)
    binding = binding_for_confirmation(store, event, session_id=session_id)
    source = (
        "user_confirmed_rebind_handled"
        if event.delivery_status == "awaiting_rebind_confirmation"
        else "user_confirmed_handled"
    )
    confirmed = store.confirm_binding(binding.binding_id, source=source)
    promote_confirmation_events_for_binding(store, confirmed)
    updated = store.update_event(
        event.event_id,
        status="dismissed",
        delivery_status="user_marked_handled",
        binding_id=confirmed.binding_id,
        confidence="high",
        evidence=event.evidence
        + [
            f"user confirmed active binding {confirmed.agent}:{confirmed.session_id}",
            "user confirmed binding and marked event handled",
        ],
        recovery_command="",
        error="User confirmed the session binding and marked this PR Watch event handled.",
    )
    return {"action": "confirmed_and_marked_handled", "event": updated, "binding": confirmed}


def reject_binding_for_event(
    store: StateStore,
    event_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    event = store.get_event(event_id)
    binding = binding_for_confirmation(store, event, session_id=session_id)
    rejected = Binding(
        binding_id=binding.binding_id,
        repo_owner=binding.repo_owner,
        repo_name=binding.repo_name,
        pr_number=binding.pr_number,
        pr_url=binding.pr_url,
        role=binding.role,
        agent=binding.agent,
        session_id=binding.session_id,
        cwd=binding.cwd,
        branch=binding.branch,
        host=binding.host,
        confidence=binding.confidence,
        confirmed=False,
        active=False,
        confirmation_source="user_rejected",
        evidence=binding.evidence + [f"user rejected session candidate {binding.agent}:{binding.session_id}"],
        created_at=binding.created_at,
        last_event_at=binding.last_event_at,
    )
    rejected = store.upsert_binding(rejected)
    updated = store.update_event(
        event.event_id,
        status="pending",
        delivery_status="session_candidate_rejected",
        binding_id="",
        confidence="low",
        evidence=event.evidence + [f"user rejected session candidate {binding.agent}:{binding.session_id}"],
        recovery_command="",
        error="User rejected the candidate session binding.",
    )
    return {"action": "rejected_binding", "event": updated, "binding": rejected}


def dismiss_event(store: StateStore, event_id: str) -> Dict[str, Any]:
    event = store.get_event(event_id)
    updated = store.update_event(
        event.event_id,
        status="dismissed",
        delivery_status="user_dismissed",
        recovery_command="",
        error="User dismissed this PR Watch event.",
    )
    return {"action": "dismissed_event", "event": updated}


def binding_for_confirmation(
    store: StateStore,
    event: InboxItem,
    session_id: Optional[str] = None,
) -> Binding:
    current = store.get_binding(event.binding_id)
    if session_id is None:
        if current is None:
            raise ValueError("event has no inferred binding candidate to confirm")
        return current

    if current and current.session_id == session_id:
        return current

    existing = store.find_binding_for_session(
        event.repo_owner,
        event.repo_name,
        event.pr_number,
        event.role,
        session_id,
    )
    if existing:
        return existing

    template = current or store.find_confirmed_binding(classified_event_from_inbox(event))
    if template is None:
        raise ValueError("cannot infer agent for selected session; bind the PR explicitly first")

    binding_id = stable_id(
        "binding",
        f"{event.repo_owner}/{event.repo_name}",
        event.pr_number,
        event.role,
        template.agent,
        session_id,
    )
    return store.create_binding(
        repo_owner=event.repo_owner,
        repo_name=event.repo_name,
        pr_number=event.pr_number,
        pr_url=event.pr_url,
        role=event.role,
        agent=template.agent,
        session_id=session_id,
        cwd=template.cwd,
        branch=template.branch,
        host=template.host,
        confidence="high",
        confirmed=False,
        active=False,
        confirmation_source="user_selected_session",
        evidence=event.evidence + [f"user selected session {session_id} for this PR and role"],
        binding_id=binding_id,
    )


def route_event(store: StateStore, event: ClassifiedEvent, sessions: Iterable[SessionInfo]) -> InboxItem:
    session_list = list(sessions)
    confirmed_bindings = store.list_confirmed_bindings(event)
    if confirmed_bindings:
        primary_confirmed = confirmed_bindings[0]
        selection = select_session_candidate(
            event.pr,
            sessions_without_confirmed_bindings(session_list, confirmed_bindings),
        )
        rebind_item = route_confirmed_event_with_session_discovery(store, event, confirmed_bindings, selection)
        if rebind_item:
            return rebind_item
        evidence = primary_confirmed.evidence + [
            "confirmed binding matched this PR and role",
            *active_binding_evidence(confirmed_bindings),
        ]
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="awaiting_approval",
            binding_id=primary_confirmed.binding_id,
            confidence="high",
            evidence=evidence,
        )

    selection = select_session_candidate(event.pr, session_list)
    if selection.ambiguous_candidates:
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="ambiguous_session_candidates",
            binding_id=None,
            confidence=selection.ambiguous_candidates[0].confidence,
            evidence=ambiguous_candidate_evidence(selection.ambiguous_candidates),
        )

    if selection.candidate is None or selection.candidate.confidence == "low":
        evidence = selection.candidate.evidence if selection.candidate else ["no session candidate with enough evidence"]
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="inbox_only",
            binding_id=None,
            confidence="low",
            evidence=evidence,
        )

    confidence = selection.candidate.confidence
    session = selection.candidate.session
    evidence = selection.candidate.evidence
    binding = create_inferred_binding(store, event, session, confidence, evidence, "inferred_candidate")
    return store.upsert_event(
        event,
        status="needs_confirmation",
        delivery_status="awaiting_first_binding_confirmation",
        binding_id=binding.binding_id,
        confidence=confidence,
        evidence=evidence + ["first inferred binding requires approval"],
    )


def route_confirmed_event_with_session_discovery(
    store: StateStore,
    event: ClassifiedEvent,
    confirmed_bindings: List[Binding],
    selection: SessionCandidateSelection,
) -> Optional[InboxItem]:
    if selection.ambiguous_candidates:
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="ambiguous_session_candidates",
            binding_id=None,
            confidence=selection.ambiguous_candidates[0].confidence,
            evidence=ambiguous_candidate_evidence(selection.ambiguous_candidates)
            + ["active confirmed bindings kept unchanged until a session is chosen"],
        )

    candidate = selection.candidate
    if candidate is None:
        return None

    if any(same_session(confirmed, candidate.session) for confirmed in confirmed_bindings):
        return None

    if candidate.confidence == "high":
        binding = create_inferred_binding(
            store,
            event,
            candidate.session,
            candidate.confidence,
            candidate.evidence,
            "rebind_candidate",
        )
        return store.upsert_event(
            event,
            status="needs_confirmation",
            delivery_status="awaiting_rebind_confirmation",
            binding_id=binding.binding_id,
            confidence="high",
            evidence=candidate.evidence
            + [
                *active_binding_evidence(confirmed_bindings),
                "confirm additional session before mirroring to it",
            ],
        )

    if candidate.confidence == "low":
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="inbox_only",
            binding_id=None,
            confidence="low",
            evidence=candidate.evidence
            + ["low-confidence different session did not replace the active binding"],
        )

    return None


def active_binding_evidence(bindings: List[Binding]) -> List[str]:
    if len(bindings) == 1:
        binding = bindings[0]
        return [f"active binding includes {binding.agent}:{binding.session_id}"]
    labels = ", ".join(f"{binding.agent}:{binding.session_id}" for binding in bindings)
    return [f"active bindings include {labels}"]


def sessions_without_confirmed_bindings(
    sessions: Iterable[SessionInfo],
    confirmed_bindings: List[Binding],
) -> List[SessionInfo]:
    return [
        session
        for session in sessions
        if not any(same_session(binding, session) for binding in confirmed_bindings)
    ]


def create_inferred_binding(
    store: StateStore,
    event: ClassifiedEvent,
    session: SessionInfo,
    confidence: str,
    evidence: List[str],
    confirmation_source: str,
) -> Binding:
    binding_id = stable_id("binding", event.pr.repo_full_name, event.pr.number, event.role, session.agent, session.session_id)
    return store.create_binding(
        repo_owner=event.pr.owner,
        repo_name=event.pr.repo,
        pr_number=event.pr.number,
        pr_url=event.pr.url,
        role=event.role,
        agent=session.agent,
        session_id=session.session_id,
        cwd=session.cwd,
        branch=session.branch,
        host=session.host,
        confidence=confidence,
        confirmed=False,
        active=False,
        confirmation_source=confirmation_source,
        evidence=evidence,
        binding_id=binding_id,
    )


def ensure_silent_author_binding(
    store: StateStore,
    pr: PullRequestRef,
    sessions: Iterable[SessionInfo],
) -> Optional[Binding]:
    author_event = ClassifiedEvent(
        pr=pr,
        role="author",
        event_type="silent_author_pr_binding",
        summary=f"PR #{pr.number} author session binding",
        actor="pr-watch",
        actionable=False,
        dedupe_key=stable_id("silent-author-binding", pr.repo_full_name, pr.number),
    )
    if store.list_confirmed_bindings(author_event):
        return None

    selection = select_session_candidate(pr, sessions)
    if selection.ambiguous_candidates or selection.candidate is None:
        return None
    if selection.candidate.confidence != "high":
        return None

    session = selection.candidate.session
    existing = store.find_binding_for_session(
        pr.owner,
        pr.repo,
        pr.number,
        "author",
        session.session_id,
    )
    if existing is not None:
        return existing if existing.confirmed and existing.active else None

    binding_id = stable_id("binding", pr.repo_full_name, pr.number, "author", session.agent, session.session_id)
    return store.create_binding(
        repo_owner=pr.owner,
        repo_name=pr.repo,
        pr_number=pr.number,
        pr_url=pr.url,
        role="author",
        agent=session.agent,
        session_id=session.session_id,
        cwd=session.cwd,
        branch=session.branch,
        host=session.host,
        confidence="high",
        confirmed=True,
        active=True,
        confirmation_source="silent_author_pr_binding",
        evidence=selection.candidate.evidence
        + ["silently bound current user's authored PR to matching session without notifying"],
        binding_id=binding_id,
    )


def same_session(binding: Binding, session: SessionInfo) -> bool:
    return binding.agent == session.agent and binding.session_id == session.session_id


def classified_event_from_inbox(event: InboxItem) -> ClassifiedEvent:
    return ClassifiedEvent(
        pr=PullRequestRef(
            owner=event.repo_owner,
            repo=event.repo_name,
            number=event.pr_number,
            url=event.pr_url,
        ),
        role=event.role,
        event_type=event.event_type,
        summary=event.summary,
        actor=event.actor,
        actionable=event.actionable,
        dedupe_key=event.dedupe_key,
        payload=event.payload,
    )


def best_session_candidate(
    pr: PullRequestRef, sessions: Iterable[SessionInfo]
) -> Optional[Tuple[str, SessionInfo, List[str]]]:
    selection = select_session_candidate(pr, sessions)
    if selection.candidate is None or selection.ambiguous_candidates:
        return None
    candidate = selection.candidate
    return candidate.confidence, candidate.session, candidate.evidence


def select_session_candidate(pr: PullRequestRef, sessions: Iterable[SessionInfo]) -> SessionCandidateSelection:
    ranked: List[SessionCandidate] = []
    for session in sessions:
        confidence, evidence = score_session(pr, session)
        rank = CONFIDENCE_RANK.get(confidence, 0)
        if rank:
            ranked.append(
                SessionCandidate(
                    confidence=confidence,
                    session=session,
                    evidence=evidence,
                    rank=rank,
                    active_rank=1 if is_active_or_focused_session(session) else 0,
                    host_rank=session_host_rank(session),
                    focus_score=session_focus_score(pr, session),
                    evidence_score=len(evidence),
                    activity_ts=session_activity_ts(session),
                )
            )
    if not ranked:
        return SessionCandidateSelection(candidate=None, ambiguous_candidates=[])

    ranked.sort(
        key=lambda candidate: (
            candidate.rank,
            candidate.active_rank,
            candidate.host_rank,
            candidate.focus_score,
            candidate.evidence_score,
            candidate.activity_ts,
            candidate.session.agent,
            candidate.session.session_id,
        ),
        reverse=True,
    )
    best = ranked[0]
    best = prefer_recent_repo_session_over_stale_focus(best, ranked)
    top_ties = [candidate for candidate in ranked if candidate.decisive_key == best.decisive_key]
    if best.rank >= CONFIDENCE_RANK["medium"] and len(top_ties) > 1:
        return SessionCandidateSelection(candidate=None, ambiguous_candidates=top_ties)

    annotated = SessionCandidate(
        confidence=best.confidence,
        session=best.session,
        evidence=annotated_selection_evidence(best, ranked),
        rank=best.rank,
        active_rank=best.active_rank,
        host_rank=best.host_rank,
        focus_score=best.focus_score,
        evidence_score=best.evidence_score,
        activity_ts=best.activity_ts,
    )
    return SessionCandidateSelection(candidate=annotated, ambiguous_candidates=[])


def prefer_recent_repo_session_over_stale_focus(
    best: SessionCandidate,
    ranked: List[SessionCandidate],
) -> SessionCandidate:
    if best.active_rank:
        return best

    fresher = [
        candidate
        for candidate in ranked
        if candidate is not best
        and candidate.rank == best.rank
        and candidate.activity_ts - best.activity_ts >= STALE_SESSION_RECENCY_SECONDS
        and candidate_has_repo_worktree_evidence(candidate)
        and candidate_has_pr_anchor_evidence(candidate)
    ]
    if not fresher:
        return best

    chosen = max(
        fresher,
        key=lambda candidate: (
            candidate.activity_ts,
            candidate.evidence_score,
            candidate.focus_score,
            candidate.session.agent,
            candidate.session.session_id,
        ),
    )
    return SessionCandidate(
        confidence=chosen.confidence,
        session=chosen.session,
        evidence=chosen.evidence + ["preferred newer matching session over stale focused candidate"],
        rank=chosen.rank,
        active_rank=chosen.active_rank,
        host_rank=chosen.host_rank,
        focus_score=chosen.focus_score,
        evidence_score=chosen.evidence_score,
        activity_ts=chosen.activity_ts,
    )


def candidate_has_repo_worktree_evidence(candidate: SessionCandidate) -> bool:
    return any("session cwd matches repo name" in item for item in candidate.evidence)


def candidate_has_pr_anchor_evidence(candidate: SessionCandidate) -> bool:
    return any(
        "PR URL" in item or "PR #" in item or "PR branch" in item
        for item in candidate.evidence
    )


def annotated_selection_evidence(best: SessionCandidate, ranked: List[SessionCandidate]) -> List[str]:
    evidence = list(best.evidence)
    peers = [candidate for candidate in ranked if candidate is not best and candidate.rank == best.rank]
    if not peers:
        return evidence
    if best.active_rank and any(peer.active_rank < best.active_rank for peer in peers):
        evidence.append("preferred active or focused session over other matching candidates")
    if best.host_rank and any(peer.host_rank < best.host_rank for peer in peers):
        evidence.append("preferred Conductor session over non-Conductor candidates")
    if best.focus_score and all(best.focus_score > peer.focus_score for peer in peers):
        evidence.append("preferred PR-focused session over PR listing or triage candidates")
    if best.activity_ts and all(best.activity_ts > peer.activity_ts for peer in peers):
        evidence.append("preferred newest matching session by last_activity_at")
    if all(best.evidence_score > peer.evidence_score for peer in peers):
        evidence.append("preferred strongest matching session evidence")
    return evidence


def ambiguous_candidate_evidence(candidates: List[SessionCandidate]) -> List[str]:
    evidence = ["multiple equally likely session candidates; choose one explicitly before resuming"]
    for candidate in candidates[:5]:
        session = candidate.session
        label = f"{session.agent}:{session.session_id}"
        details = []
        if session.cwd:
            details.append(f"cwd={session.cwd}")
        if session.branch:
            details.append(f"branch={session.branch}")
        if session.last_activity_at:
            details.append(f"last_activity_at={session.last_activity_at}")
        suffix = f" ({', '.join(details)})" if details else ""
        evidence.append(f"candidate {label}{suffix}")
    return evidence


def score_session(pr: PullRequestRef, session: SessionInfo) -> Tuple[str, List[str]]:
    haystack = " ".join([session.title, session.cwd, session.branch, session.text]).lower()
    evidence: List[str] = []
    if pr.url.lower() in haystack:
        evidence.append(f"session text contains exact PR URL {pr.url}")
    if f"#{pr.number}" in haystack or f"pr {pr.number}" in haystack or f"pull/{pr.number}" in haystack:
        evidence.append(f"session text mentions PR #{pr.number}")
    if pr.repo_full_name.lower() in haystack:
        evidence.append(f"session text mentions repo {pr.repo_full_name}")
    if pr.repo.lower() in session.cwd.lower():
        evidence.append(f"session cwd matches repo name {pr.repo}")
    if pr.head_ref and pr.head_ref.lower() in session.branch.lower():
        evidence.append(f"session branch resembles PR head branch {pr.head_ref}")
    focus_evidence = pr_focused_session_evidence(pr, session)
    if focus_evidence:
        evidence.extend(focus_evidence)
    if looks_like_pr_listing_session(pr, session) and not focus_evidence:
        evidence.append("session appears to list or triage multiple PRs rather than handle this PR")

    exact_pr = any("PR URL" in item or f"PR #{pr.number}" in item for item in evidence)
    repo_match = any("repo" in item or "cwd" in item for item in evidence)
    branch_match = any("branch" in item for item in evidence)

    if looks_like_pr_listing_session(pr, session) and not focus_evidence:
        return ("low", evidence) if repo_match else ("none", [])
    if exact_pr and repo_match:
        return "high", evidence
    if exact_pr:
        return "medium", evidence
    if repo_match and branch_match:
        return "medium", evidence
    if repo_match:
        return "low", evidence
    return "none", []


def session_focus_score(pr: PullRequestRef, session: SessionInfo) -> int:
    return len(pr_focused_session_evidence(pr, session))


def pr_focused_session_evidence(pr: PullRequestRef, session: SessionInfo) -> List[str]:
    title = session.title.lower()
    haystack = " ".join([session.title, session.branch, session.text]).lower()
    evidence: List[str] = []
    if re.search(rf"\b(?:review|fix|check|handle|resume)\s+(?:pr\s*)?#?{pr.number}\b", title):
        evidence.append(f"session title is focused on PR #{pr.number}")
    if re.search(rf"\b(?:origin/)?pr[-/]{pr.number}\b", haystack):
        evidence.append(f"session work references PR branch {pr.number}")
    if f"pull/{pr.number}#discussion_r" in haystack:
        evidence.append(f"session references a review discussion on PR #{pr.number}")
    if f"/pulls/{pr.number}/comments" in haystack or (
        "pulls/comments" in haystack and f"pull/{pr.number}" in haystack
    ):
        evidence.append(f"session performed review comment work for PR #{pr.number}")
    return evidence


def looks_like_pr_listing_session(pr: PullRequestRef, session: SessionInfo) -> bool:
    title = session.title.lower()
    haystack = " ".join([session.title, session.text]).lower()
    if re.search(r"\b(show|list|find|search)\s+(?:pending\s+|open\s+)?prs?\b", title):
        return True
    listing_markers = (
        "gh search prs",
        "--review-requested",
        "--reviewed-by",
        "review-requested",
        "reviewed-by",
        "list open prs",
        "open prs in",
    )
    if any(marker in haystack for marker in listing_markers) and _distinct_pr_mentions(haystack) >= 3:
        return True
    return False


def _distinct_pr_mentions(text: str) -> int:
    numbers = set(re.findall(r"(?:#|pr\s+|pull/)(\d{1,7})", text, flags=re.IGNORECASE))
    return len(numbers)


def is_active_or_focused_session(session: SessionInfo) -> bool:
    marker = " ".join([str(session.host or ""), session.title]).lower()
    return any(token in marker for token in ("active", "focused", "foreground", "current"))


def session_host_rank(session: SessionInfo) -> int:
    return 1 if (session.host or "").strip().lower() == "conductor" else 0


def session_activity_ts(session: SessionInfo) -> float:
    value = (session.last_activity_at or "").strip()
    if not value:
        return 0.0
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_pr_reference(value: str, repo: Optional[str] = None) -> PullRequestRef:
    parsed = parse_pr_url(value)
    if parsed:
        return PullRequestRef(
            owner=str(parsed["owner"]),
            repo=str(parsed["repo"]),
            number=int(parsed["number"]),
            url=str(parsed["url"]),
        )
    if value.startswith("#") and repo and "/" in repo:
        owner, repo_name = repo.split("/", 1)
        number = int(value[1:])
        return PullRequestRef(
            owner=owner,
            repo=repo_name,
            number=number,
            url=f"https://github.com/{owner}/{repo_name}/pull/{number}",
        )
    raise ValueError("Use a GitHub PR URL, or pass #123 with --repo owner/name.")
