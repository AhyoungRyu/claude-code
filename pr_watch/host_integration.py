from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


MCP_SERVER_NAME = "pr-watch"
CONDUCTOR_CODEX_BINARY = (
    Path.home() / "Library" / "Application Support" / "com.conductor.app" / "bin" / "codex"
)


@dataclass(frozen=True)
class McpLaunchConfig:
    command: str
    args: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CodexHost:
    host: str
    binary: str


@dataclass(frozen=True)
class HostCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class HostInstallResult:
    host: str
    status: str
    binary: str = ""
    message: str = ""


class SubprocessHostCommandRunner:
    def run(self, command: List[str]) -> HostCommandResult:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        return HostCommandResult(completed.returncode, completed.stdout, completed.stderr)


class RecordingHostCommandRunner:
    def __init__(self):
        self.commands: List[List[str]] = []

    def run(self, command: List[str]) -> HostCommandResult:
        self.commands.append(command)
        if command[1:4] == ["mcp", "get", MCP_SERVER_NAME]:
            return HostCommandResult(1, "", f"No MCP server named '{MCP_SERVER_NAME}' found.")
        return HostCommandResult(0, "", "")


def build_mcp_launch_config(
    python_executable: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> McpLaunchConfig:
    args = ["-m", "pr_watch"]
    if state_dir:
        args.extend(["--state-dir", str(Path(state_dir).expanduser())])
    args.append("mcp")
    return McpLaunchConfig(command=python_executable or sys.executable, args=args)


def build_codex_mcp_add_command(
    codex_binary: str,
    launch: McpLaunchConfig,
    server_name: str = MCP_SERVER_NAME,
) -> List[str]:
    return [codex_binary, "mcp", "add", server_name, "--", launch.command, *launch.args]


def build_codex_mcp_get_command(codex_binary: str, server_name: str = MCP_SERVER_NAME) -> List[str]:
    return [codex_binary, "mcp", "get", server_name]


def build_codex_mcp_remove_command(codex_binary: str, server_name: str = MCP_SERVER_NAME) -> List[str]:
    return [codex_binary, "mcp", "remove", server_name]


def resolve_codex_hosts(
    target: str,
    codex_binary: Optional[str] = None,
    conductor_codex_binary: Optional[str] = None,
) -> List[CodexHost]:
    normalized = _normalize_target(target)
    hosts: List[CodexHost] = []
    if normalized in {"codex-app", "all"}:
        binary = codex_binary or _default_codex_binary()
        if binary:
            hosts.append(CodexHost("codex-app", binary))
    if normalized in {"conductor", "all"}:
        binary = conductor_codex_binary or _default_conductor_codex_binary()
        if binary:
            hosts.append(CodexHost("conductor", binary))
    return hosts


def install_mcp_hosts(
    target: str,
    python_executable: Optional[str] = None,
    state_dir: Optional[str] = None,
    codex_binary: Optional[str] = None,
    conductor_codex_binary: Optional[str] = None,
    runner: Optional[object] = None,
    replace: bool = True,
) -> List[HostInstallResult]:
    launch = build_mcp_launch_config(python_executable=python_executable, state_dir=state_dir)
    command_runner = runner or SubprocessHostCommandRunner()
    results: List[HostInstallResult] = []
    for host in resolve_codex_hosts(target, codex_binary, conductor_codex_binary):
        if replace:
            existing = command_runner.run(build_codex_mcp_get_command(host.binary))
            if existing.returncode == 0:
                removed = command_runner.run(build_codex_mcp_remove_command(host.binary))
                if removed.returncode != 0:
                    results.append(
                        HostInstallResult(
                            host=host.host,
                            status="failed",
                            binary=host.binary,
                            message=removed.stderr.strip() or removed.stdout.strip(),
                        )
                    )
                    continue
        added = command_runner.run(build_codex_mcp_add_command(host.binary, launch))
        if added.returncode == 0:
            results.append(HostInstallResult(host=host.host, status="installed", binary=host.binary))
            continue
        results.append(
            HostInstallResult(
                host=host.host,
                status="failed",
                binary=host.binary,
                message=added.stderr.strip() or added.stdout.strip(),
            )
        )
    return results


def _normalize_target(target: str) -> str:
    normalized = (target or "all").strip().lower()
    aliases = {
        "codex": "codex-app",
        "app": "codex-app",
        "codex_app": "codex-app",
        "codex-app": "codex-app",
        "conductor": "conductor",
        "all": "all",
    }
    if normalized not in aliases:
        raise ValueError("target must be one of: codex-app, conductor, all")
    return aliases[normalized]


def _default_codex_binary() -> Optional[str]:
    return shutil.which("codex") or _existing_path(Path.home() / "bin" / "codex")


def _default_conductor_codex_binary() -> Optional[str]:
    return _existing_path(CONDUCTOR_CODEX_BINARY)


def _existing_path(path: Path) -> Optional[str]:
    expanded = path.expanduser()
    return str(expanded) if expanded.exists() else None
