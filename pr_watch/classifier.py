from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .models import ClassifiedEvent, PullRequestRef
from .util import stable_id


SERVICE_LOGINS = {
    "github-actions",
    "github-actions[bot]",
    "netlify",
    "netlify[bot]",
    "vercel",
    "vercel[bot]",
}
SERVICE_LOGIN_PREFIXES = ("dependabot", "renovate")


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
        condition_key = f"requested_changes:{review_decision or 'CHANGES_REQUESTED'}:{actor}"
        yield _event(
            pr,
            role="author",
            event_type="requested_changes",
            actor=actor,
            occurred_at=str((requested_change_review or {}).get("submittedAt") or condition_key),
            summary=f"Requested changes on your PR #{pr.number}.",
            payload={"reviewDecision": review_decision, "condition_key": condition_key},
        )

    failed_checks = sorted(
        {
            name
            for name in (
                _check_name(check)
                for check in _items(pr_data.get("statusCheckRollup") or pr_data.get("checks"))
                if _check_failed(check)
            )
            if name
        }
    )
    if failed_checks:
        names = ", ".join(failed_checks) or "one or more checks"
        head_key = _head_key(pr_data, updated_at)
        condition_key = f"{head_key}:{names}"
        yield _event(
            pr,
            role="author",
            event_type="ci_failed",
            actor="github",
            occurred_at=f"ci_failed:{condition_key}",
            summary=f"CI failed on PR #{pr.number}: {names}.",
            payload={
                "failed_checks": failed_checks,
                "headRefOid": str(pr_data.get("headRefOid") or ""),
                "condition_key": condition_key,
            },
        )

    if _has_merge_conflict(pr_data):
        head_key = _head_key(pr_data, updated_at)
        merge_state = str(pr_data.get("mergeStateStatus") or "")
        mergeable = str(pr_data.get("mergeable") or "")
        condition_key = f"{head_key}:{merge_state}:{mergeable}"
        yield _event(
            pr,
            role="author",
            event_type="merge_conflict",
            actor="github",
            occurred_at=f"merge_conflict:{condition_key}",
            summary=f"PR #{pr.number} has a merge conflict.",
            payload={
                "mergeStateStatus": merge_state,
                "mergeable": mergeable,
                "headRefOid": str(pr_data.get("headRefOid") or ""),
                "condition_key": condition_key,
            },
        )

    comments_by_actor: Dict[str, List[Dict[str, Any]]] = {}
    for comment in _items(pr_data.get("comments")):
        actor = _login(comment.get("author"))
        if _is_actionable_human_comment(comment, current_user):
            comments_by_actor.setdefault(actor, []).append(comment)

    for actor in sorted(comments_by_actor):
        comments = sorted(
            comments_by_actor[actor],
            key=lambda comment: (
                str(comment.get("updatedAt") or comment.get("createdAt") or updated_at),
                str(comment.get("id") or comment.get("databaseId") or comment.get("url") or ""),
            ),
        )
        latest = comments[-1]
        comment_ids = [
            comment.get("id") or comment.get("databaseId") or comment.get("url") or ""
            for comment in comments
        ]
        comment_id = comment_ids[-1] if comment_ids else ""
        occurred_at = str(latest.get("updatedAt") or latest.get("createdAt") or updated_at)
        count = len(comments)
        if count == 1:
            summary = f"{actor} commented on your PR #{pr.number}."
        else:
            summary = f"{actor} left {count} comments on your PR #{pr.number}."
        yield _event(
            pr,
            role="author",
            event_type="human_comment",
            actor=actor,
            occurred_at=f"{occurred_at}:{comment_id}",
            summary=summary,
            payload={
                "comment_id": comment_id,
                "comment_ids": comment_ids,
                "comment_count": count,
                "condition_key": f"human_comment:{actor}",
            },
        )

    review_comments_by_actor: Dict[str, List[Dict[str, Any]]] = {}
    for comment in _items(pr_data.get("reviewComments") or pr_data.get("review_comments")):
        actor = _login(comment.get("author"))
        if _is_actionable_human_comment(comment, current_user):
            review_comments_by_actor.setdefault(actor, []).append(comment)

    for actor in sorted(review_comments_by_actor):
        comments = sorted(
            review_comments_by_actor[actor],
            key=lambda comment: (
                str(comment.get("updatedAt") or comment.get("createdAt") or updated_at),
                str(comment.get("id") or comment.get("databaseId") or comment.get("url") or ""),
            ),
        )
        latest = comments[-1]
        comment_ids = [
            comment.get("id") or comment.get("databaseId") or comment.get("url") or ""
            for comment in comments
        ]
        comment_id = comment_ids[-1] if comment_ids else ""
        occurred_at = str(latest.get("updatedAt") or latest.get("createdAt") or updated_at)
        count = len(comments)
        if count == 1:
            summary = f"{actor} left an inline review comment on your PR #{pr.number}."
        else:
            summary = f"{actor} left {count} inline review comments on your PR #{pr.number}."
        yield _event(
            pr,
            role="author",
            event_type="human_review_comment",
            actor=actor,
            occurred_at=f"{occurred_at}:{comment_id}",
            summary=summary,
            payload={
                "comment_id": comment_id,
                "path": latest.get("path") or "",
                "url": latest.get("url") or "",
                "comment_ids": comment_ids,
                "paths": [comment.get("path") or "" for comment in comments],
                "urls": [comment.get("url") or "" for comment in comments],
                "comment_count": count,
                "condition_key": f"human_review_comment:{actor}",
            },
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

    replies_by_actor: Dict[str, List[Dict[str, Any]]] = {}
    for reply in _items(pr_data.get("authorReplies") or pr_data.get("replies")):
        actor = _login(reply.get("author")) or author
        if actor and actor != current_user:
            replies_by_actor.setdefault(actor, []).append(reply)

    for actor in sorted(replies_by_actor):
        replies = sorted(
            replies_by_actor[actor],
            key=lambda reply: (
                str(reply.get("updatedAt") or reply.get("createdAt") or updated_at),
                str(reply.get("id") or reply.get("databaseId") or reply.get("url") or ""),
            ),
        )
        latest = replies[-1]
        reply_ids = [reply.get("id") or reply.get("databaseId") or reply.get("url") or "" for reply in replies]
        reply_id = reply_ids[-1] if reply_ids else ""
        occurred_at = str(latest.get("updatedAt") or latest.get("createdAt") or updated_at)
        count = len(replies)
        if count == 1:
            summary = f"{actor} replied on PR #{pr.number}."
        else:
            summary = f"{actor} replied {count} times on PR #{pr.number}."
        yield _event(
            pr,
            role="reviewer",
            event_type="author_reply",
            actor=actor,
            occurred_at=f"{occurred_at}:{reply_id}",
            summary=summary,
            payload={
                "reply_id": reply_id,
                "reply_ids": reply_ids,
                "reply_count": count,
                "condition_key": f"author_reply:{actor}",
            },
        )

    threads_by_state_actor: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for thread in _items(pr_data.get("reviewThreads") or pr_data.get("threads")):
        state = str(thread.get("state") or "").lower()
        if state in {"resolved", "reopened"}:
            actor = _login(thread.get("actor")) or author or "github"
            threads_by_state_actor.setdefault((state, actor), []).append(thread)

    for state, actor in sorted(threads_by_state_actor):
        threads = sorted(
            threads_by_state_actor[(state, actor)],
            key=lambda thread: (
                str(thread.get("updatedAt") or updated_at),
                str(thread.get("id") or thread.get("databaseId") or thread.get("url") or ""),
            ),
        )
        latest = threads[-1]
        thread_ids = [
            thread.get("id") or thread.get("databaseId") or thread.get("url") or ""
            for thread in threads
        ]
        thread_id = thread_ids[-1] if thread_ids else ""
        occurred_at = str(latest.get("updatedAt") or updated_at)
        count = len(threads)
        if count == 1:
            summary = f"A review thread was {state} on PR #{pr.number}."
        else:
            summary = f"{count} review threads were {state} on PR #{pr.number}."
        yield _event(
            pr,
            role="reviewer",
            event_type=f"thread_{state}",
            actor=actor,
            occurred_at=f"{occurred_at}:{thread_id}",
            summary=summary,
            payload={
                "thread_id": thread_id,
                "thread_ids": thread_ids,
                "thread_count": count,
                "condition_key": f"thread_{state}:{actor}",
            },
        )

    requested = [_login(item) for item in _items(pr_data.get("reviewRequests") or pr_data.get("requestedReviewers"))]
    if current_user in requested:
        requested_key = ",".join(sorted({login for login in requested if login}))
        review_request_event = _latest_review_requested_event(pr_data, current_user)
        requested_at = str(review_request_event.get("createdAt") or review_request_event.get("created_at") or "")
        condition_key = f"review_requested:{requested_key}"
        if requested_at:
            condition_key = f"{condition_key}:{requested_at}"
        yield _event(
            pr,
            role="requested_reviewer",
            event_type="review_requested",
            actor=_login(review_request_event.get("actor")) or author or "author",
            occurred_at=condition_key,
            summary=f"You were requested to review PR #{pr.number}.",
            payload={
                "requested_reviewers": requested,
                "condition_key": condition_key,
                "requested_at": requested_at,
            },
        )


def _linked_issue_events(
    pr: PullRequestRef, pr_data: Dict[str, Any], current_user: str, role: str, updated_at: str
) -> Iterable[ClassifiedEvent]:
    for issue in _linked_issues(pr_data):
        issue_number = issue.get("number")
        issue_url = str(issue.get("url") or "")
        issue_title = str(issue.get("title") or "")
        comments_by_actor: Dict[str, List[Dict[str, Any]]] = {}
        for comment in _items(issue.get("comments")):
            actor = _login(comment.get("author"))
            if not _is_actionable_human_comment(comment, current_user):
                continue
            comments_by_actor.setdefault(actor, []).append(comment)

        for actor in sorted(comments_by_actor):
            comments = sorted(
                comments_by_actor[actor],
                key=lambda comment: (
                    str(comment.get("updatedAt") or comment.get("createdAt") or updated_at),
                    str(comment.get("id") or comment.get("databaseId") or comment.get("url") or ""),
                ),
            )
            latest = comments[-1]
            comment_ids = [
                comment.get("id") or comment.get("databaseId") or comment.get("url") or ""
                for comment in comments
            ]
            comment_id = comment_ids[-1] if comment_ids else ""
            occurred_at = str(latest.get("updatedAt") or latest.get("createdAt") or updated_at)
            condition_key = f"linked_issue_comment:{issue_number}:{actor}"
            count = len(comments)
            if count == 1:
                summary = f"{actor} commented on linked issue #{issue_number} for PR #{pr.number}."
            else:
                summary = f"{actor} left {count} comments on linked issue #{issue_number} for PR #{pr.number}."
            payload = {
                "source": "github_issue",
                "issue_number": issue_number,
                "issue_url": issue_url,
                "issue_title": issue_title,
                "comment_id": comment_id,
                "comment_ids": comment_ids,
                "comment_count": count,
                "latest_comment_url": latest.get("url") or "",
                "condition_key": condition_key,
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


def _has_merge_conflict(pr_data: Dict[str, Any]) -> bool:
    merge_state = str(pr_data.get("mergeStateStatus") or "").upper()
    mergeable = str(pr_data.get("mergeable") or "").upper()
    return merge_state == "DIRTY" or mergeable == "CONFLICTING"


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
    linked_by_key: Dict[str, Dict[str, Any]] = {}
    for key in ("linkedIssues", "closingIssuesReferences"):
        for issue in _items(pr_data.get(key)):
            issue_key = str(issue.get("number") or issue.get("url") or issue.get("id") or "")
            if not issue_key:
                issue_key = f"index:{len(linked_by_key)}"
            existing = linked_by_key.get(issue_key)
            if existing is None:
                linked_by_key[issue_key] = dict(issue)
                continue
            for field in ("number", "url", "title"):
                if not existing.get(field) and issue.get(field):
                    existing[field] = issue[field]
            comments = _merge_comments(_items(existing.get("comments")) + _items(issue.get("comments")))
            if comments:
                existing["comments"] = comments
    return list(linked_by_key.values())


def _merge_comments(comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for comment in comments:
        key = _comment_identity(comment)
        if key in seen:
            continue
        seen.add(key)
        merged.append(comment)
    return merged


def _comment_identity(comment: Dict[str, Any]) -> str:
    return str(
        comment.get("id")
        or comment.get("databaseId")
        or comment.get("url")
        or "|".join(
            [
                _login(comment.get("author")),
                str(comment.get("createdAt") or ""),
                str(comment.get("updatedAt") or ""),
                str(comment.get("body") or ""),
            ]
        )
    )


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


def _latest_review_requested_event(pr_data: Dict[str, Any], login: str) -> Dict[str, Any]:
    events = [
        event
        for event in _items(pr_data.get("issueEvents") or pr_data.get("timelineEvents"))
        if str(event.get("event") or "") == "review_requested"
        and _login(event.get("requestedReviewer") or event.get("requested_reviewer")) == login
    ]
    if not events:
        return {}
    return sorted(events, key=lambda item: str(item.get("createdAt") or item.get("created_at") or ""))[-1]


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


def _head_key(pr_data: Dict[str, Any], updated_at: str) -> str:
    return str(
        pr_data.get("headRefOid")
        or pr_data.get("head_sha")
        or pr_data.get("headSha")
        or pr_data.get("lastPushedAt")
        or pr_data.get("last_pushed_at")
        or updated_at
    )


def _is_actionable_human_comment(comment: Dict[str, Any], current_user: str) -> bool:
    actor = _login(comment.get("author"))
    if not actor or actor == current_user:
        return False
    if _is_service_comment(comment, actor):
        return False
    return _is_human(actor)


def _is_service_comment(comment: Dict[str, Any], login: str) -> bool:
    return _is_service_login(login) or _is_bot_author(comment) or _is_deploy_preview_comment(comment, login)


def _is_service_login(login: str) -> bool:
    lowered = login.lower()
    normalized = lowered.removesuffix("[bot]")
    return (
        lowered in SERVICE_LOGINS
        or normalized in SERVICE_LOGINS
        or any(lowered.startswith(prefix) or normalized.startswith(prefix) for prefix in SERVICE_LOGIN_PREFIXES)
    )


def _is_bot_author(comment: Dict[str, Any]) -> bool:
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    values = [
        author.get("type"),
        author.get("__typename"),
        comment.get("authorType"),
        comment.get("type"),
        user.get("type"),
    ]
    return any(str(value).lower() == "bot" for value in values if value)


def _is_deploy_preview_comment(comment: Dict[str, Any], login: str) -> bool:
    body = str(comment.get("body") or "").lower()
    if not body:
        return False
    association = str(comment.get("authorAssociation") or comment.get("author_association") or "").upper()
    service_like_author = _is_service_login(login) or _is_bot_author(comment) or association in {"NONE", "BOT"}
    if not service_like_author:
        return False
    if "deploy preview" in body and ("ready" in body or "netlify" in body):
        return True
    if "preview deployment" in body and ("ready" in body or "vercel" in body):
        return True
    return False


def _is_human(login: str) -> bool:
    lowered = login.lower()
    return not (
        lowered.endswith("[bot]")
        or lowered.endswith("-bot")
        or lowered == "github-actions"
        or _is_service_login(login)
    )
