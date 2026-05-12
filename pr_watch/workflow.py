from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from .models import Binding, ClassifiedEvent, InboxItem, PullRequestRef, SessionInfo
from .state import StateStore
from .util import parse_pr_url, stable_id


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

    score = best_session_candidate(event.pr, sessions)
    if score is None or score[0] == "low":
        evidence = score[2] if score else ["no session candidate with enough evidence"]
        return store.upsert_event(
            event,
            status="pending",
            delivery_status="inbox_only",
            binding_id=None,
            confidence="low",
            evidence=evidence,
        )

    confidence, session, evidence = score
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
    ranked: List[Tuple[int, str, SessionInfo, List[str]]] = []
    for session in sessions:
        confidence, evidence = score_session(pr, session)
        rank = {"high": 3, "medium": 2, "low": 1}.get(confidence, 0)
        if rank:
            ranked.append((rank, confidence, session, evidence))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    _, confidence, session, evidence = ranked[0]
    return confidence, session, evidence


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
