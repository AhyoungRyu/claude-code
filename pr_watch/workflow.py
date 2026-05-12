from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

from .models import Binding, ClassifiedEvent, InboxItem, PullRequestRef, SessionInfo
from .state import StateStore
from .util import parse_pr_url, stable_id


CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class SessionCandidate:
    confidence: str
    session: SessionInfo
    evidence: List[str]
    rank: int
    active_rank: int
    evidence_score: int
    activity_ts: float

    @property
    def decisive_key(self) -> Tuple[int, int, int, float]:
        return (self.rank, self.active_rank, self.evidence_score, self.activity_ts)


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
    return store.create_binding(
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


def route_event(store: StateStore, event: ClassifiedEvent, sessions: Iterable[SessionInfo]) -> InboxItem:
    confirmed = store.find_confirmed_binding(event)
    if confirmed:
        evidence = confirmed.evidence + ["confirmed binding matched this PR and role"]
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="awaiting_approval",
            binding_id=confirmed.binding_id,
            confidence="high",
            evidence=evidence,
        )

    selection = select_session_candidate(event.pr, sessions)
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
    binding_id = stable_id("binding", event.pr.repo_full_name, event.pr.number, event.role, session.agent, session.session_id)
    binding = store.create_binding(
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
        confirmation_source="inferred_candidate",
        evidence=evidence,
        binding_id=binding_id,
    )
    return store.upsert_event(
        event,
        status="needs_confirmation",
        delivery_status="awaiting_first_binding_confirmation",
        binding_id=binding.binding_id,
        confidence=confidence,
        evidence=evidence + ["first inferred binding requires approval"],
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
            candidate.evidence_score,
            candidate.activity_ts,
            candidate.session.agent,
            candidate.session.session_id,
        ),
        reverse=True,
    )
    best = ranked[0]
    top_ties = [candidate for candidate in ranked if candidate.decisive_key == best.decisive_key]
    if best.rank >= CONFIDENCE_RANK["medium"] and len(top_ties) > 1:
        return SessionCandidateSelection(candidate=None, ambiguous_candidates=top_ties)

    annotated = SessionCandidate(
        confidence=best.confidence,
        session=best.session,
        evidence=annotated_selection_evidence(best, ranked),
        rank=best.rank,
        active_rank=best.active_rank,
        evidence_score=best.evidence_score,
        activity_ts=best.activity_ts,
    )
    return SessionCandidateSelection(candidate=annotated, ambiguous_candidates=[])


def annotated_selection_evidence(best: SessionCandidate, ranked: List[SessionCandidate]) -> List[str]:
    evidence = list(best.evidence)
    peers = [candidate for candidate in ranked if candidate is not best and candidate.rank == best.rank]
    if not peers:
        return evidence
    if best.active_rank and any(peer.active_rank < best.active_rank for peer in peers):
        evidence.append("preferred active or focused session over other matching candidates")
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

    exact_pr = any("PR URL" in item or f"PR #{pr.number}" in item for item in evidence)
    repo_match = any("repo" in item or "cwd" in item for item in evidence)
    branch_match = any("branch" in item for item in evidence)

    if exact_pr and repo_match:
        return "high", evidence
    if exact_pr:
        return "medium", evidence
    if repo_match and branch_match:
        return "medium", evidence
    if repo_match:
        return "low", evidence
    return "none", []


def is_active_or_focused_session(session: SessionInfo) -> bool:
    marker = " ".join([str(session.host or ""), session.title]).lower()
    return any(token in marker for token in ("active", "focused", "foreground", "current"))


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
