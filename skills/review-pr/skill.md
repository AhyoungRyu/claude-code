---
name: review-pr
description: >
  Comprehensive PR review using parallel specialist agents and Codex CLI.
  Runs Claude Code specialist agents and Codex CLI review in parallel,
  cross-validates findings for accuracy, and produces a unified severity-rated report.
  Gracefully skips Codex if unavailable.
argument-hint: "[PR number | branch | security | quality | performance | api | all]"
allowed-tools: Bash, Read, Glob, Grep, Agent, Task
---

# PR Review: $ARGUMENTS

## Step 0 — Playbook Init (always runs first)

Initialize playbook artifacts and classify this task before any review work begins.

```bash
if [ -d ".omc" ]; then
  PLAYBOOK_DIR=".omc/playbook"
else
  PLAYBOOK_DIR=".context/playbook"
fi
mkdir -p "$PLAYBOOK_DIR"
RUN_TS=$(date -Iseconds)
printf "# work.md\nRun: %s\nTask type: research (PR review)\n\nGoal: Review PR $ARGUMENTS\n" "$RUN_TS" > "$PLAYBOOK_DIR/work.md"
printf "# plan.md\nRun: %s\nTask type: research\n\nScope: PR review — analysis only (no code changes unless Critical/High issues found)\n\nConstraints:\n- Cite file paths and line numbers as evidence\n- No code changes unless user explicitly allows it\n\nSkill mapping:\n- Detect context: direct implementation (gh CLI)\n- Agent routing: oh-my-claudecode:analyst\n- Review: oh-my-claudecode:code-reviewer + security-reviewer + quality-reviewer + codex review (parallel)\n- Cross-validate: Claude vs Codex findings\n- Aggregate: direct implementation\n- Fix (conditional): oh-my-claudecode:executor\n" "$RUN_TS" > "$PLAYBOOK_DIR/plan.md"
printf "# result.md\nRun: %s\n\n(summary will be written after review completes)\n" "$RUN_TS" > "$PLAYBOOK_DIR/result.md"
```

> **Classification**: `research` — PR review is read-only analysis. Build/test gates do not apply to this task type. Code fix step is conditional on Critical/High findings.
> Artifacts are written to `$PLAYBOOK_DIR/` throughout this run. On completion, `result.md` will be updated with the final summary.

---


## Step 1 — Parse Arguments, Detect PR Context & Check Codex CLI

### 1a — Parse `$ARGUMENTS`

Parse `$ARGUMENTS` to determine these variables:

```
PR_NUMBER=""        # PR number (if provided)
HEAD_REF=""         # head branch to review
BASE_REF=""         # base branch for comparison
REVIEW_FOCUS=""     # security | performance | api | quality (if provided)
NO_FIX=false        # --no-fix flag
```

**Parsing rules** (apply in order):

| `$ARGUMENTS` pattern | Action |
|---|---|
| Empty | Use current branch: `HEAD_REF=$(git branch --show-current)`. Find PR via `gh pr list --head "$HEAD_REF" --json number,baseRefName -q '.[0]'`. |
| Number only (e.g., `731`) | `PR_NUMBER=731`. Get refs via `gh pr view 731 --json headRefName,baseRefName -q '.headRefName + " " + .baseRefName'`. |
| `remote <branch-name>` | `HEAD_REF=<branch-name>`. Find PR via `gh pr list --head "$HEAD_REF" --json number,baseRefName -q '.[0]'`. **No local checkout needed.** |
| Contains `--no-fix` | Set `NO_FIX=true`, strip flag, continue parsing remaining args. |
| Contains keyword (`security`, `performance`, `api`, `quality`) | Set `REVIEW_FOCUS` to that keyword, strip it, continue parsing remaining args. |
| Combined (e.g., `731 security`) | Parse number as `PR_NUMBER`, keyword as `REVIEW_FOCUS`. |

**Error handling:**
- If `gh pr list --head` returns empty → error: `"No PR found for branch <HEAD_REF>"` and stop.
- If `gh pr view <number>` fails → error: `"PR #<number> not found"` and stop.

### 1b — Fetch remote refs & compute diff (branch-independent)

Once `HEAD_REF` and `BASE_REF` are determined:

```bash
# Fetch remote refs — no local checkout required
git fetch origin "$HEAD_REF":"refs/remotes/origin/$HEAD_REF" "$BASE_REF":"refs/remotes/origin/$BASE_REF" 2>/dev/null

# Verify fetch succeeded
if ! git rev-parse "origin/$HEAD_REF" &>/dev/null; then
  echo "ERROR: Failed to fetch origin/$HEAD_REF" && exit 1
fi
if ! git rev-parse "origin/$BASE_REF" &>/dev/null; then
  echo "ERROR: Failed to fetch origin/$BASE_REF" && exit 1
fi

# Diff always uses origin refs — never depends on local branch state
DIFF_CMD="git diff origin/$BASE_REF...origin/$HEAD_REF"
CHANGED_FILES=$($DIFF_CMD --name-only)
FULL_DIFF=$($DIFF_CMD)
DIFF_STAT=$($DIFF_CMD --stat)
```

If `CHANGED_FILES` is empty → warn: `"No changes detected between origin/$BASE_REF and origin/$HEAD_REF"`.

**Fallback for empty `$ARGUMENTS` (current branch, no PR found):**
If no PR is associated with the current branch, fall back to the legacy diff:
```bash
BASE_REF="main"
DIFF_CMD="git diff $(git merge-base HEAD origin/main 2>/dev/null || echo HEAD~1) HEAD"
```

### 1c — Fetch PR metadata & reviews (parallel)

Run these in parallel (use `PR_NUMBER` from 1a):

1. `gh pr view $PR_NUMBER --json number,title,body,baseRefName,headRefName,additions,deletions,files` — PR metadata
2. `gh pr view $PR_NUMBER --json comments,reviews` — existing review comments (avoid duplicate feedback)

### 1d — Codex CLI availability check

```bash
CODEX_AVAILABLE=false
CODEX_SKIP_REASON=""
if ! command -v codex &>/dev/null; then
  CODEX_SKIP_REASON="codex CLI not found in PATH"
elif ! codex --version &>/dev/null; then
  CODEX_SKIP_REASON="codex CLI installed but not functional"
else
  # Quick auth check — codex review requires valid credentials
  CODEX_VERSION=$(codex --version 2>&1)
  CODEX_AVAILABLE=true
fi
```

Store `CODEX_AVAILABLE`, `CODEX_SKIP_REASON`, `DIFF_CMD`, `FULL_DIFF`, `CHANGED_FILES`, `DIFF_STAT`, `BASE_REF`, `HEAD_REF` for use in subsequent steps.

## Step 2 — Smart Agent Routing

Examine the changed file list and select applicable agents:

| Condition | Agent | Model |
|---|---|---|
| **Always** | `oh-my-claudecode:code-reviewer` | opus |
| auth / network / env config / input handling changed | `oh-my-claudecode:security-reviewer` | opus |
| business logic / hooks / state / data processing changed | `oh-my-claudecode:quality-reviewer` | opus |
| loops / rendering hot paths / data-intensive code changed | `oh-my-claudecode:performance-reviewer` | sonnet |
| public API / exported types / interface changed | `oh-my-claudecode:api-reviewer` | sonnet |
| test files added/changed or critical logic added without tests | `oh-my-claudecode:test-engineer` | sonnet |

**For broad architectural patterns** (cross-cutting concerns, overall design):
- Use MCP Codex: `ask_codex(agent_role: "architect")` — faster and cheaper than spawning an agent.
- Pass changed file list + PR description as context.

**Trivial changes** (typo fix, comment only, rename):
- Skip specialist agents. Run `code-reviewer` only with brief effort level.

If $ARGUMENTS specifies a specific aspect (e.g., "security", "performance") → run only that agent.

## Step 3 — Launch Reviews in Parallel (Claude Code + Codex CLI)

Launch **two review tracks** simultaneously:

### Track A: Claude Code Specialist Agents

Use the Agent tool with `run_in_background: true` to launch all applicable agents simultaneously.

Each agent receives:
- PR title and description (from Step 1)
- `$FULL_DIFF` (the `origin/$BASE_REF...origin/$HEAD_REF` diff from Step 1b)
- Instruction: **focus only on changed files**, not the entire codebase
- LSP and AST tools available for deep analysis (`lsp_diagnostics`, `ast_grep_search`)
- For newly introduced wrapper components or utility functions, **read their callsites** before assessing the pattern — the caller's constraints (render props, async context, platform limitations) often justify designs that look unusual in isolation

### Track B: Codex CLI Review

**If `CODEX_AVAILABLE` is true**, run `codex review` in the background simultaneously with Track A:

```bash
# Use BASE_REF from Step 1a — always compare against origin refs
# Run codex review and capture output
codex review \
  --base "origin/$BASE_REF" \
  --title "[PR Title from Step 1]" \
  "Review this PR thoroughly. Focus on: logic bugs, security issues, performance problems, API contract violations, missing error handling, and test coverage gaps. For each issue found, provide: 1) file path and line number, 2) severity (Critical/High/Medium/Low), 3) description, 4) suggested fix. Also note positive patterns worth keeping." \
  2>&1
```

Save the Codex output to `$PLAYBOOK_DIR/codex-review.md`.

**If `CODEX_AVAILABLE` is false**, record the skip:
```
## Codex CLI Review: SKIPPED
Reason: [CODEX_SKIP_REASON]
```

**Handle Codex runtime failures** — if the `codex review` command exits non-zero or produces an error (e.g., authentication failure, rate limit, network error):
- Capture the error output
- Set `CODEX_SKIP_REASON` to the actual error message (e.g., "Authentication failed — run `codex login` to re-authenticate", "Rate limit exceeded", "Network timeout")
- Continue with Claude Code results only
- Report the failure reason in the final report

### Critical Review Mindset (include in every Claude agent prompt)

Every issue found MUST pass these filters before being reported:

1. **Practical Impact Test**: "Would this actually cause a bug or degrade UX in production?" If the effect is idempotent or the theoretical issue has no real-world consequence, downgrade to informational or omit. For `useEffect` double-invocation concerns (StrictMode, cleanup), always check whether the triggered callback is idempotent — if it is, the double-call is harmless and should not be flagged as an issue.
2. **Author Intent Test**: Before flagging a pattern as wrong, investigate *why* the author may have written it this way. Read surrounding code (e.g., is it inside a render prop where hooks can't be called directly? Is there a platform-specific reason?). If the pattern is a deliberate, justified tradeoff, acknowledge it rather than flagging it.
3. **YAGNI Filter**: Before suggesting abstractions, refactors, or extractions (e.g., "extract to a shared hook"), verify that the use cases are actually identical. Check if platform-specific differences, different timing requirements, or other divergences make the abstraction premature.
4. **Devil's Advocate**: For each High/Medium issue, briefly state the strongest counter-argument the PR author might make. If the counter-argument is convincing, reconsider the severity or drop the issue.

## Step 4 — Cross-Validate & Aggregate Report

After both tracks complete, **cross-validate findings** before compiling the final report.

### Cross-Validation Process

1. **Merge findings**: Collect all issues from Claude Code agents (Track A) and Codex CLI (Track B).

2. **Classify each finding into one of these categories:**
   - **Corroborated**: Both Claude and Codex flagged the same issue (or substantially similar). **High confidence** — include with boosted credibility.
   - **Claude-only**: Only Claude agents found this. Assess whether Codex likely missed it (complex logic requiring deep context) or whether it might be a false positive (overly cautious pattern matching).
   - **Codex-only**: Only Codex found this. Assess whether Claude agents likely missed it or whether the finding is inaccurate. Read the relevant code to verify.
   - **Contradicted**: Claude and Codex disagree on the same code section. **Investigate the actual code** to determine which assessment is correct. State which reviewer was right and why.

3. **Accuracy judgment**: For each non-corroborated finding, add a brief accuracy assessment:
   - `[Verified]` — manually confirmed by reading the code
   - `[Likely valid]` — consistent with codebase patterns but not manually verified
   - `[Questionable]` — may be a false positive; include the counter-argument
   - `[Dismissed]` — determined to be incorrect after investigation; explain why

### Unified Report Format

```
# PR Review: [PR Title] (#[number])
Base: [baseRef] <- Head: [headRef] | +[additions] / -[deletions] lines

## Review Sources
| Source | Status | Notes |
|--------|--------|-------|
| Claude Code (specialist agents) | Completed | [list of agents used] |
| Codex CLI | Completed / SKIPPED | [version or skip reason] |

## Review Matrix
| Reviewer         | Verdict           | Critical | High | Medium | Low |
|-----------------|-------------------|----------|------|--------|-----|
| Code Quality     | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Security         | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Quality/Logic    | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Performance      | APPROVE/COMMENT   |    X     |  X   |   X    |  X  |
| API Compat.      | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Codex CLI        | APPROVE/REQUEST/SKIPPED |  X  |  X   |   X    |  X  |

---

## Cross-Validation Summary
- **Corroborated findings** (both Claude & Codex agree): X issues
- **Claude-only findings**: X issues (Y verified, Z questionable)
- **Codex-only findings**: X issues (Y verified, Z questionable)
- **Contradictions resolved**: X (Claude correct: Y, Codex correct: Z)

---

## 🔴 Critical Issues (must fix before merge)
- `file.ts:42` — [issue description] — **Source:** [Claude+Codex / Claude-only / Codex-only] — **Fix:** [concrete fix]

## 🟠 High Issues (should fix)
- `file.ts:88` — [issue description] — **Source:** [source] [accuracy tag] — **Fix:** [concrete fix]

## 🟡 Medium / Low (consider)
- `file.ts:120` — [suggestion] — **Source:** [source] [accuracy tag]

## ✅ Positive Observations
- [What is done well — reinforce good patterns]

## 🔵 Design Choices (informational)
- [Patterns that look unusual but appear intentional — describe the tradeoff rather than prescribing a change]

---

## Final Verdict: APPROVE / REQUEST CHANGES / COMMENT
[1-2 sentence rationale based on highest severity found, noting cross-validation confidence level]
```

## Step 5 — Apply Local Fixes

For **Critical** and **High** issues that were **corroborated or verified**, make local code changes to address them.
- **Do NOT commit or push**
- After each fix, run `lsp_diagnostics` on the modified file to verify no type errors introduced
- Note each fix inline in the report above with `[FIXED]` marker
- Prioritize corroborated findings (both reviewers agree) over single-source findings

Use `--no-fix` flag to skip this step (analysis only).

---

## Installation

```bash
git clone https://github.com/AhyoungRyu/claude-code.git

# As a skill (invokable via Skill tool)
cp -r claude-code/skills/review-pr ~/.claude/skills/

# Or as a command (invokable via /review-pr slash command)
cp claude-code/skills/review-pr/skill.md ~/.claude/commands/review-pr.md
```

### Optional: Codex CLI Setup

For dual-model review, install and authenticate Codex CLI:
```bash
# Install
npm install -g @anthropic-ai/codex
# or: brew install codex

# Authenticate
codex login
```

If Codex CLI is not available, the skill works normally with Claude Code agents only.

## Usage

```
/review-pr                       # review current branch's PR
/review-pr 731                   # review PR #731
/review-pr security              # security-focused review only
/review-pr --no-fix              # analysis only, skip local fixes
/review-pr remote test-check     # review remote branch's PR (no local checkout needed)
/review-pr 731 security          # review PR #731, security focus only
```
