from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .util import normalize_repo_full_name


SCP_STYLE_GITHUB_REMOTE_RE = re.compile(r"^(?:[^@]+@)?github\.com:(?P<path>.+)$")


def parse_github_remote_url(value: str) -> Optional[str]:
    remote = str(value or "").strip()
    if not remote:
        return None

    scp_match = SCP_STYLE_GITHUB_REMOTE_RE.match(remote)
    if scp_match:
        return _repo_from_path(scp_match.group("path"))

    parsed = urlparse(remote)
    if parsed.hostname != "github.com":
        return None
    return _repo_from_path(parsed.path)


def detect_current_repo(cwd: Optional[Path | str] = None, remote: str = "origin") -> str:
    working_directory = Path(cwd or Path.cwd())
    result = subprocess.run(
        ["git", "-C", str(working_directory), "remote", "get-url", remote],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"could not read git remote {remote!r}")

    repo = parse_github_remote_url(result.stdout.strip())
    if not repo:
        raise ValueError(f"git remote {remote!r} is not a GitHub owner/repo URL")
    return repo


def _repo_from_path(value: str) -> Optional[str]:
    path = str(value or "").strip().lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None
    return normalize_repo_full_name("/".join(parts))
