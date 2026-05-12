from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .models import ClassifiedEvent, PullRequestRef
from .util import stable_id


def classify_pr(pr_data: Dict[str, Any], current_user: str) -> List[ClassifiedEvent]:
    pr = _pr_ref(pr_data)
    author = _login(pr_data.get("author"))
    updated_at = str(pr_data.get("updatedAt") or pr_data.get("updated_at") or "")
    events: List[ClassifiedEvent] = []
    role = _current_user_role(pr_data, current_user, author)

    if author == current_user:
        events.extend(_author_events(pr, pr_data, current_user, updated_at))
    else:
        events.extend(_reviewer_events(pr, pr_data, current_user, author, updated_at))
    if role:
        events.extend(_linked_issue_events(pr, pr_data, current_user, role, updated_at))

    return [event for event in events if event.actionable]


def _pr_ref(pr_data: Dict[str, Any]) -> PullRequestRef:
    owner = str(pr_data.get("owner") or pr_data.get("repo_owner") or "")
    repo = str(pr_data.get("repo") or pr_data.get("repo_name") or "")
    repository = pr_data.get("repository") or {}
    if not owner and isinstance(repository, dict):
        owner = str((repository.get("owner") or {}).get("login") or repository.get("owner") or "")
    if not repo and isinstance(repository, dict):
        repo = str(repository.get("name") or "")
    number = int(pr_data.get("number"))
    url = str(pr_data.get("url") or pr_data.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}")
    return PullRequestRef(
        owner=owner,
        repo=repo,
        number=number,
        url=url,
        title=str(pr_data.get("title") or ""),
        head_ref=str(pr_data.get("headRefName") or pr_data.get("head_ref") or ""),
    )


def _author_events(
    pr: PullRequestRef, pr_data: Dict[str, Any], current_user: str, updated_at: str
) -> Iterable[ClassifiedEvent]:
    review_decision = str(pr_data.get("reviewDecision") or "").upper()
    reviews = _items(pr_data.get("latestReviews") or pr_data.get("reviews"))
    requested_change_review = next(
        (
            review
            for review in reviews
            if str(review.get("state") or "").upper() == "CHANGES_REQUESTED"
            and _login(review.get("author")) != current_user
        ),
        None,
    )
    if review_decision == "CHANGES_REQUESTED" or requested_change_review:
        actor = _login((requested_change_review or {}).get("author")) or "reviewer"
        yield _event(
            pr,
            role="author",
            event_type="requested_changes",
            actor=actor,
            occurred_at=str((requested_change_review or {}).get("submittedAt") or updated_at),
            summary=f"Requested changes on your PR #{pr.number}.",
            payload={"reviewDecision": review_decision},
        )

    failed_checks = [
        _check_name(check)
        for check in _items(pr_data.get("statusCheckRollup") or pr_data.get("checks"))
        if _check_failed(check)
    ]
    if failed_checks:
        names = ", ".join(name for name in failed_checks if name) or "one or more checks"
        yield _event(
            pr,
            role="author",
            event_type="ci_failed",
            actor="github",
            occurred_at=updated_at,
            summary=f"CI failed on PR #{pr.number}: {names}.",
            payload={"failed_checks": failed_checks},
        )

    if str(pr_data.get("mergeStateStatus") or "").upper() in {"DIRTY", "BLOCKED"}:
        yield _event(
            pr,
            role="author",
            event_type="merge_conflict",
            actor="github",
            occurred_at=updated_at,
            summary=f"PR #{pr.number} has a merge conflict.",
            payload={"mergeStateStatus": pr_data.get("mergeStateStatus")},
        )

    for comment in _items(pr_data.get("comments")):
        actor = _login(comment.get("author"))
        if actor and actor != current_user and _is_human(actor):
            yield _event(
                pr,
                role="author",
                event_type="human_comment",
                actor=actor,
                occurred_at=str(comment.get("updatedAt") or comment.get("createdAt") or updated_at),
                summary=f"{actor} commented on your PR #{pr.number}.",
                payload={"comment_id": comment.get("id")},
            )


def _reviewer_events(
    pr: PullRequestRef, pr_data: Dict[str, Any], current_user: str, author: str, updated_at: str
) -> Iterable[ClassifiedEvent]:
    reviews = _items(pr_data.get("latestReviews") or pr_data.get("reviews"))
    own_review = _latest_by_user(reviews, current_user)
    last_pushed_at = str(pr_data.get("lastPushedAt") or pr_data.get("last_pushed_at") or "")
    if own_review and last_pushed_at and last_pushed_at > str(own_review.get("submittedAt") or ""):
        actor = author or "author"
        yield _event(
            pr,
            role="reviewer",
            event_type="author_push_after_review",
            actor=actor,
            occurred_at=last_pushed_at,
            summary=f"{actor} pushed commits to PR #{pr.number} after your review.",
            payload={"lastPushedAt": last_pushed_at},
        )

    for reply in _items(pr_data.get("authorReplies") or pr_data.get("replies")):
        actor = _login(reply.get("author")) or author
        if actor and actor != current_user:
            yield _event(
                pr,
                role="reviewer",
                event_type="author_reply",
                actor=actor,
                occurred_at=str(reply.get("updatedAt") or reply.get("createdAt") or updated_at),
                summary=f"{actor} replied on PR #{pr.number}.",
                payload={"reply_id": reply.get("id")},
            )

    for thread in _items(pr_data.get("reviewThreads") or pr_data.get("threads")):
        state = str(thread.get("state") or "").lower()
        if state in {"resolved", "reopened"}:
            yield _event(
                pr,
                role="reviewer",
                event_type=f"thread_{state}",
                actor=_login(thread.get("actor")) or author or "github",
                occurred_at=str(thread.get("updatedAt") or updated_at),
                summary=f"A review thread was {state} on PR #{pr.number}.",
                payload={"thread_id": thread.get("id")},
            )

    requested = [_login(item) for item in _items(pr_data.get("reviewRequests") or pr_data.get("requestedReviewers"))]
    if current_user in requested:
        yield _event(
            pr,
            role="requested_reviewer",
            event_type="review_requested",
            actor=author or "author",
            occurred_at=updated_at,
            summary=f"You were requested to review PR #{pr.number}.",
            payload={"requested_reviewers": requested},
        )


def _linked_issue_events(
    pr: PullRequestRef, pr_data: Dict[str, Any], current_user: str, role: str, updated_at: str
) -> Iterable[ClassifiedEvent]:
    for issue in _linked_issues(pr_data):
        issue_number = issue.get("number")
        issue_url = str(issue.get("url") or "")
        issue_title = str(issue.get("title") or "")
        for comment in _items(issue.get("comments")):
            actor = _login(comment.get("author"))
            if not actor or actor == current_user or not _is_human(actor):
                continue
            comment_id = comment.get("id") or comment.get("databaseId") or comment.get("url") or ""
            occurred_at = str(comment.get("updatedAt") or comment.get("createdAt") or updated_at)
            summary = f"{actor} commented on linked issue #{issue_number} for PR #{pr.number}."
            payload = {
                "source": "github_issue",
                "issue_number": issue_number,
                "issue_url": issue_url,
                "issue_title": issue_title,
                "comment_id": comment_id,
            }
            yield _event(
                pr,
                role=role,
                event_type="linked_issue_comment",
                actor=actor,
                occurred_at=f"{occurred_at}:{comment_id}",
                summary=summary,
                payload=payload,
            )


def _current_user_role(pr_data: Dict[str, Any], current_user: str, author: str) -> str:
    if author == current_user:
        return "author"
    reviews = _items(pr_data.get("latestReviews") or pr_data.get("reviews"))
    if _latest_by_user(reviews, current_user):
        return "reviewer"
    requested = [_login(item) for item in _items(pr_data.get("reviewRequests") or pr_data.get("requestedReviewers"))]
    if current_user in requested:
        return "requested_reviewer"
    comments = _items(pr_data.get("comments"))
    if any(_login(comment.get("author")) == current_user for comment in comments):
        return "commenter"
    return ""


def _linked_issues(pr_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    linked = []
    for key in ("linkedIssues", "closingIssuesReferences"):
        linked.extend(_items(pr_data.get(key)))
    return linked


def _event(
    pr: PullRequestRef,
    role: str,
    event_type: str,
    actor: str,
    occurred_at: str,
    summary: str,
    payload: Optional[Dict[str, Any]] = None,
) -> ClassifiedEvent:
    dedupe_key = stable_id(pr.repo_full_name, pr.number, role, event_type, actor, occurred_at)
    return ClassifiedEvent(
        pr=pr,
        role=role,
        event_type=event_type,
        actor=actor,
        occurred_at=occurred_at,
        summary=summary,
        actionable=True,
        dedupe_key=dedupe_key,
        payload=payload or {},
    )


def _items(value: Any) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict) and isinstance(value.get("nodes"), list):
        return [item for item in value["nodes"] if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _login(value: Any) -> str:
    if isinstance(value, dict):
        nested = value.get("login") or value.get("name")
        if nested:
            return str(nested)
    if isinstance(value, str):
        return value
    return ""


def _latest_by_user(items: List[Dict[str, Any]], login: str) -> Optional[Dict[str, Any]]:
    matches = [item for item in items if _login(item.get("author")) == login]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("submittedAt") or item.get("updatedAt") or ""))[-1]


def _check_failed(check: Dict[str, Any]) -> bool:
    conclusion = str(check.get("conclusion") or check.get("status") or "").upper()
    return conclusion in {"FAILURE", "FAILED", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"}


def _check_name(check: Dict[str, Any]) -> str:
    return str(check.get("name") or check.get("context") or check.get("workflowName") or "check")


def _is_human(login: str) -> bool:
    lowered = login.lower()
    return not (lowered.endswith("[bot]") or lowered.endswith("-bot") or lowered == "github-actions")
