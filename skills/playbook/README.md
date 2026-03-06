# playbook

Turn any natural-language goal into a structured, executable runbook.

## What it does

1. Classifies your task type (`code-change`, `refactor`, `code-cleanup`, `file-ops`, `research`, `config`, `docs`, `planning`)
2. Snapshots all available Claude Code skills and OMC agents
3. Delegates runbook authoring to Codex with task-appropriate phases and constraints
4. Executes autonomously — stops only on critical gates (irreversible actions, unexpected scope, failing baseline)
5. Writes trace artifacts to `.omc/playbook/` or `.context/playbook/` (work.md, plan.md, result.md)

## Install

```bash
cp -r skills/playbook ~/.claude/skills/
```

## Dependencies

**OMC is optional.** Playbook works in two modes:

| Mode | Runbook authoring | Requirement |
|------|-------------------|-------------|
| With OMC | Delegates to Codex via `oh-my-claudecode:omc-teams` for higher-quality runbooks | [oh-my-claudecode](https://github.com/AhyoungRyu/claude-code) installed |
| Without OMC | Claude authors the runbook directly using the same template | Plain Claude Code, no extras |

During execution, playbook invokes whatever skills are listed in your `skills_snapshot.md` — resolved at runtime from your local `~/.claude/skills/` directory.

## Usage

```
/playbook <your goal>
```

Examples:
```
/playbook fix the type error in UserCard component
/playbook remove all unused imports across the repo
/playbook research how data flows from API to the chart render
/playbook update README to reflect the new monorepo structure
```

## How it works

```mermaid
flowchart TD
    START(["/playbook &lt;goal&gt;"]) --> R

    subgraph R["Step R — Reset &amp; detect"]
        R1{"<code>.omc/</code> exists?"}
        R1 -->|Yes| R2["PLAYBOOK_DIR = <code>.omc/playbook</code>"]
        R1 -->|No|  R3["PLAYBOOK_DIR = <code>.context/playbook</code>"]
        R2 & R3 --> R4["✍ init work.md · result.md · plan.md"]
    end

    R --> A["Step A — Classify task type
    code-change · refactor · code-cleanup
    file-ops · research · config · docs · planning"]

    A --> C["Step C — Scan skills
    ✍ skills_snapshot.md"]

    C --> C2{"<code>steering.md</code>
    exists?"}
    C2 -->|Yes| C2R["📖 read steering.md\n(inject as hard constraints)"]
    C2 -->|No|  D
    C2R --> D

    subgraph D["Step D — Author runbook"]
        DA{"OMC available?"}
        DA -->|omc-teams| DB["Codex writes runbook"]
        DA -->|no OMC|   DC["Claude writes runbook"]
        DB & DC --> DD["✍ work.md"]
    end

    D --> ISSUE{"⚠️ issues in
    Consistency Check?"}
    ISSUE -->|Yes| STOP1["🛑 surface issues
    wait for user input"]
    ISSUE -->|No|  CT

    CT{"Code task?
    code-change / refactor
    code-cleanup"}
    CT -->|Yes| E2["Step E2
    ✍ plan.md
    (before any code is touched)"]
    CT -->|No|  F

    E2 --> F

    subgraph F["Step F — Execute runbook"]
        FA["invoke skills/agents from snapshot"]
        FA --> FB{"Critical gate?"}
        FB -->|Yes| STOP2["🛑 ask user"]
        FB -->|No|  FC["continue phases"]
        FC --> FD["✍ result.md"]
    end

    F --> DONE(["Present summary to user"])

    style START fill:#e8f4fd,stroke:#0d6efd
    style DONE  fill:#d1e7dd,stroke:#198754
    style STOP1 fill:#fff3cd,stroke:#ffc107
    style STOP2 fill:#fff3cd,stroke:#ffc107
    style R     fill:#f8f9fa,stroke:#adb5bd
    style D     fill:#f8f9fa,stroke:#adb5bd
    style F     fill:#f8f9fa,stroke:#adb5bd
```

## Artifacts

All output is written to `.omc/playbook/` (or `.context/playbook/` if `.omc/` doesn't exist):

| File | What's inside |
|------|---------------|
| `work.md` | Full Codex-authored runbook: phases, step-by-step actions, skill mappings, consistency check |
| `plan.md` | Pre-execution plan extracted before any code is touched: files to modify, rationale, test gates *(code tasks only)* |
| `result.md` | Run summary written at completion: changes made, skills invoked, artifacts produced, open TODOs |
| `baseline.md` | Test/build state captured before any changes *(code tasks only, when applicable)* |
| `skills_snapshot.md` | Auto-generated inventory of all available slash commands and OMC agents, used during planning |
| `steering.md` | Persistent project-level constraints you write once and inject into every subsequent run |

### Example: `work.md`

```markdown
# work.md
Run: 2026-03-06T10:42:00+09:00
Task type: code-change

## Baseline
- Tests: 331 passing
- Build: success (68.73 kB)

## Plan
1. Locate `UserCard` component — direct implementation
2. Fix type error on line 42 — oh-my-claudecode:executor
3. Run type check — direct implementation

## Implement
...

## Proof
- pnpm tsc --noEmit ✅
- pnpm test ✅

## Consistency Check
✅ All consistency checks passed.
```

### Example: `result.md`

```markdown
# result.md
Run: 2026-03-06T10:45:00+09:00
Task type: code-change

## Changes / Findings
Fixed type error in UserCard component (src/components/UserCard.tsx:42):
replaced `any` with `User` interface type.

## Skills invoked
- oh-my-claudecode:executor

## Artifacts produced
- .omc/playbook/work.md
- .omc/playbook/plan.md
- .omc/playbook/result.md

## Risks / TODOs
none
```

### Example: `steering.md`

```markdown
# Steering

- We use Zustand, not Redux
- Never use barrel imports
- All async functions must handle errors explicitly
- pnpm workspace: packages/charts is the library, apps/demo is the showcase
```

Playbook reads `steering.md` automatically on every run and injects it into the Codex prompt as hard constraints.
