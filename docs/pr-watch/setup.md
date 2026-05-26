# PR Watch Setup

## Install

```bash
python3.11 -m pip install -e .
pr-watch doctor
```

`doctor` checks local dependencies, config, `gh` auth, and optional host tools.

## Pick A Profile

```bash
pr-watch init --profile terminal
pr-watch init --profile conductor
pr-watch init --profile app
```

| Profile | Default `notification_mode` | Best for |
|---------|-----------------------------|----------|
| `terminal` | `desktop` | Plain macOS terminal usage |
| `conductor` | `desktop` | Conductor users who want PR Watch desktop notifications |
| `app` | `in_app` | Codex App, Conductor, or other MCP-first usage |
| `auto` | `auto` | Let PR Watch resolve by host/platform |

## Register MCP

```bash
pr-watch install-mcp --target all
```

Targets:

| Target | Host |
|--------|------|
| `all` | Codex App/CLI and Conductor |
| `codex-app` | Codex App/CLI |
| `conductor` | Conductor bundled Codex |

MCP registration is user-level for the selected host. It makes PR Watch tools available from sessions in any repository. It does not add watched repositories or install background polling.

More MCP details: [MCP setup](./mcp.md).

## Watch Repositories

From the repository you want to watch:

```bash
pr-watch setup --current-repo --install-service
```

For a specific repo:

```bash
pr-watch setup --repo owner/name --install-service
```

Manual allowlist commands:

```bash
pr-watch watch add owner/name
pr-watch watch remove owner/name
pr-watch watch list
pr-watch watch clear
```

Background polling only checks repositories in this allowlist.

## Recommended Conductor Setup

```bash
pr-watch init --profile conductor
pr-watch install-mcp --target all
pr-watch setup --current-repo --install-service --host-sync --host conductor --notify-prompt-confirmed
```

This gives you:

| Feature | Result |
|---------|--------|
| `install-mcp` | Conductor/Codex sessions can call PR Watch tools |
| `--install-service` | launchd polls watched repos in the background |
| `--host-sync --host conductor` | Eligible events can surface in Conductor sessions |
| `--notify-prompt-confirmed` | Confirmed bindings receive notification-only prompts |

## Background Service

```bash
pr-watch service install --interval 120 --notification-mode auto
pr-watch service status
pr-watch service uninstall
```

The service is a launchd `StartInterval` job, not a resident Python process. Each wake-up runs `pr-watch service run-once`, polls watched repositories, updates local state, emits configured notifications, then exits.

Use host sync with the service:

```bash
pr-watch service install --interval 120 --notification-mode auto --host-sync --host conductor --notify-prompt-confirmed
```

`--trigger-confirmed` is opt-in. Without it, host sync can show prompts but does not automatically approve, resume, or queue work.

## Isolated Test Setup

```bash
pr-watch --state-dir ~/.pr-watch-test init --profile app
pr-watch --state-dir ~/.pr-watch-test install-mcp --target all
pr-watch --state-dir ~/.pr-watch-test setup --repo AhyoungRyu/claude-code --install-service --dry-run
```

Use a separate `--state-dir` when testing changes that should not touch your normal PR Watch inbox or bindings.
