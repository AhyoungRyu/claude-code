from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional, Set

from .classifier import classify_pr
from .models import InboxItem, SessionInfo
from .notifications import notify_events
from .state import StateStore
from .util import normalize_repo_full_name
from .workflow import route_event


GH_PR_FIELDS = (
    "number,url,title,author,body,headRefName,updatedAt,isDraft,reviewDecision,"
    "mergeStateStatus,mergeable,statusCheckRollup,latestReviews,comments,reviewRequests"
    ",closingIssuesReferences,commits"
)

ISSUE_REF_RE = re.compile(r"(?:^|\s)(?:fixes|closes|resolves|refs)?\s*#(?P<number>\d+)\b", re.IGNORECASE)
ISSUE_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/issues/(?P<number>\d+)")


def load_fixture(path: str) -> List[dict]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(value, dict) and isinstance(value.get("pullRequests"), list):
        return value["pullRequests"]
    if isinstance(value, list):
        return value
    raise ValueError("fixture must be a list of PR objects or an object with pullRequests")


def filter_prs_for_repo(prs: Iterable[dict], repo: str) -> List[dict]:
    owner, name = normalize_repo_full_name(repo).split("/", 1)
    return [pr for pr in prs if _pr_repo_full_name(pr) == (owner, name)]


def current_user() -> str:
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh auth lookup failed")
    return result.stdout.strip()


def fetch_prs(repo: str) -> List[dict]:
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", GH_PR_FIELDS],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh pr list failed for {repo}")
    prs = json.loads(result.stdout or "[]")
    owner, name = repo.split("/", 1)
    for pr in prs:
        pr.setdefault("owner", owner)
        pr.setdefault("repo", name)
    enrich_pull_request_metadata(prs, repo)
    return prs


def enrich_pull_request_metadata(prs: List[dict], repo: str) -> None:
    for pr in prs:
        _derive_last_pushed_at(pr)
        number = _int_or_none(pr.get("number"))
        if number is not None:
            pr["reviewComments"] = fetch_pull_request_review_comments(repo, number)
    enrich_linked_issue_comments(prs, repo)


def enrich_linked_issue_comments(prs: List[dict], repo: str) -> None:
    owner, name = repo.split("/", 1)
    for pr in prs:
        linked = _normalized_linked_issues(pr, owner, name)
        for issue in linked:
            number = issue.get("number")
            if not number:
                continue
            issue["comments"] = fetch_issue_comments(repo, int(number))
        if linked:
            pr["linkedIssues"] = linked


def _derive_last_pushed_at(pr: dict) -> None:
    if pr.get("lastPushedAt"):
        return
    commit_dates = []
    for commit in _items(pr.get("commits")):
        date = commit.get("committedDate") or commit.get("authoredDate")
        if date:
            commit_dates.append(str(date))
    if commit_dates:
        pr["lastPushedAt"] = sorted(commit_dates)[-1]


def fetch_issue_comments(repo: str, issue_number: int) -> List[dict]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{issue_number}/comments"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    raw_comments = json.loads(result.stdout or "[]")
    comments = []
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        comments.append(
            {
                "id": comment.get("id"),
                "url": comment.get("html_url"),
                "author": {"login": user.get("login"), "type": user.get("type")},
                "authorAssociation": comment.get("author_association") or comment.get("authorAssociation"),
                "body": comment.get("body") or "",
                "createdAt": comment.get("created_at") or "",
                "updatedAt": comment.get("updated_at") or "",
            }
        )
    return comments


def fetch_pull_request_review_comments(repo: str, pr_number: int) -> List[dict]:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}/comments"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    raw_comments = json.loads(result.stdout or "[]")
    comments = []
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        comments.append(
            {
                "id": comment.get("id"),
                "url": comment.get("html_url"),
                "author": {"login": user.get("login"), "type": user.get("type")},
                "authorAssociation": comment.get("author_association") or comment.get("authorAssociation"),
                "body": comment.get("body") or "",
                "path": comment.get("path") or "",
                "createdAt": comment.get("created_at") or "",
                "updatedAt": comment.get("updated_at") or "",
            }
        )
    return comments


def _normalized_linked_issues(pr: dict, owner: str, name: str) -> List[dict]:
    by_number: dict[int, dict] = {}
    for issue in _items(pr.get("closingIssuesReferences")):
        number = _int_or_none(issue.get("number"))
        if number is None:
            continue
        by_number[number] = {
            "number": number,
            "url": issue.get("url") or f"https://github.com/{owner}/{name}/issues/{number}",
            "title": issue.get("title") or "",
        }
    for number in _issue_numbers_from_body(str(pr.get("body") or "")):
        by_number.setdefault(
            number,
            {
                "number": number,
                "url": f"https://github.com/{owner}/{name}/issues/{number}",
                "title": "",
            },
        )
    return list(by_number.values())


def _issue_numbers_from_body(body: str) -> Set[int]:
    numbers = {_int_or_none(match.group("number")) for match in ISSUE_REF_RE.finditer(body)}
    numbers.update(_int_or_none(match.group("number")) for match in ISSUE_URL_RE.finditer(body))
    return {number for number in numbers if number is not None}


def _items(value: object) -> List[dict]:
    if isinstance(value, dict) and isinstance(value.get("nodes"), list):
        return [item for item in value["nodes"] if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _pr_repo_full_name(pr: dict) -> tuple[str, str]:
    owner = str(pr.get("owner") or pr.get("repo_owner") or "").lower()
    repo = str(pr.get("repo") or pr.get("repo_name") or "").lower()
    repository = pr.get("repository") or {}
    if isinstance(repository, dict):
        if not owner:
            raw_owner = repository.get("owner") or {}
            owner = str(raw_owner.get("login") if isinstance(raw_owner, dict) else raw_owner).lower()
        if not repo:
            repo = str(repository.get("name") or "").lower()
    return owner, repo


def _int_or_none(value: object) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def poll_once(
    store: StateStore,
    current_user_login: str,
    repo: Optional[str] = None,
    fixture: Optional[str] = None,
    sessions: Optional[Iterable[SessionInfo]] = None,
    include_drafts: bool = False,
    notification_mode: str = "none",
    notifier: Optional[object] = None,
    notification_host: Optional[str] = None,
) -> List[InboxItem]:
    if fixture:
        prs = load_fixture(fixture)
        if repo:
            prs = filter_prs_for_repo(prs, repo)
    elif repo:
        prs = fetch_prs(repo)
    else:
        raise ValueError("poll_once requires --repo or --fixture")

    reconcile_repo = normalize_repo_full_name(repo) if repo else _infer_repo_from_prs(prs)
    if reconcile_repo:
        open_numbers = [number for number in (_int_or_none(pr.get("number")) for pr in prs) if number is not None]
        store.dismiss_stale_open_pr_events(reconcile_repo, open_numbers)

    session_list = list(sessions or [])
    routed: List[InboxItem] = []
    current_dedupe_keys: List[str] = []
    for pr in prs:
        if pr.get("isDraft") and not include_drafts:
            continue
        for event in classify_pr(pr, current_user_login):
            routed_event = route_event(store, event, session_list)
            current_dedupe_keys.append(routed_event.dedupe_key)
            routed.append(routed_event)
    if reconcile_repo:
        store.dismiss_stale_current_pr_events(reconcile_repo, open_numbers, current_dedupe_keys)
    notify_events(store, routed, mode=notification_mode, notifier=notifier, host=notification_host)
    return routed


def daemon_loop(
    store: StateStore,
    current_user_login: str,
    repo: Optional[str],
    fixture: Optional[str],
    sessions: Optional[Iterable[SessionInfo]],
    interval_seconds: int,
    include_drafts: bool = False,
    notification_mode: str = "none",
    notification_host: Optional[str] = None,
) -> None:
    while True:
        poll_once(
            store,
            current_user_login,
            repo=repo,
            fixture=fixture,
            sessions=sessions,
            include_drafts=include_drafts,
            notification_mode=notification_mode,
            notification_host=notification_host,
        )
        time.sleep(interval_seconds)


def _infer_repo_from_prs(prs: Iterable[dict]) -> Optional[str]:
    repos = {
        f"{owner}/{name}"
        for owner, name in (_pr_repo_full_name(pr) for pr in prs)
        if owner and name
    }
    if len(repos) == 1:
        return next(iter(repos))
    return None
