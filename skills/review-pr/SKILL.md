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

### 1c — Fetch PR metadata, reviews & existing bot comments (parallel)

Run these in parallel (use `PR_NUMBER` from 1a):

1. `gh pr view $PR_NUMBER --json number,title,body,baseRefName,headRefName,additions,deletions,files` — PR metadata
2. `gh pr view $PR_NUMBER --json comments,reviews` — existing review comments (avoid duplicate feedback)
3. Fetch existing Codex bot inline comments to build an **exclusion list**:

```bash
# Fetch all inline review comments left by Codex bot and human reviewers
ALL_PR_COMMENTS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
  --jq '[.[] | {user: .user.login, path, line: .original_line, body, commit: .original_commit_id}]' \
  2>/dev/null)

# Extract Codex bot comments specifically
CODEX_BOT_COMMENTS=$(echo "$ALL_PR_COMMENTS" | jq '[.[] | select(.user == "chatgpt-codex-connector[bot]")]')
```

**Build an exclusion list** from existing bot comments:
- Extract `path:line` pairs and a brief summary of each already-flagged issue
- This list is passed to all review agents in Step 3 so they **skip issues already covered**
- Also note whether each bot comment appears addressed (compare `commit` field vs latest PR commit) or still open

### 1d — External CLI availability checks

**Codex CLI:**
```bash
CODEX_AVAILABLE=false
CODEX_SKIP_REASON=""
if ! command -v codex &>/dev/null; then
  CODEX_SKIP_REASON="codex CLI not found in PATH"
elif ! codex --version &>/dev/null; then
  CODEX_SKIP_REASON="codex CLI installed but not functional"
else
  CODEX_VERSION=$(codex --version 2>&1)
  CODEX_AVAILABLE=true
fi

# Run built-in Codex reviews with the dedicated review profile.
# This keeps review effort at "high" even if the default coding profile uses "xhigh".
CODEX_REVIEW_FLAGS="--profile review"
```

**Gemini CLI:**
```bash
GEMINI_AVAILABLE=false
GEMINI_SKIP_REASON=""
if ! command -v gemini &>/dev/null; then
  GEMINI_SKIP_REASON="gemini CLI not found in PATH"
elif ! gemini --version &>/dev/null; then
  GEMINI_SKIP_REASON="gemini CLI installed but not functional"
else
  GEMINI_VERSION=$(gemini --version 2>&1)
  GEMINI_AVAILABLE=true
fi
```

Store `CODEX_AVAILABLE`, `CODEX_SKIP_REASON`, `GEMINI_AVAILABLE`, `GEMINI_SKIP_REASON`, `DIFF_CMD`, `FULL_DIFF`, `CHANGED_FILES`, `DIFF_STAT`, `BASE_REF`, `HEAD_REF` for use in subsequent steps.

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

## Step 3 — Launch Reviews in Parallel (Claude Code + Codex CLI + Gemini CLI)

Launch **up to three review tracks** simultaneously:

### Track A: Claude Code Specialist Agents

Use the Agent tool with `run_in_background: true` to launch all applicable agents simultaneously.

Each agent receives:
- PR title and description (from Step 1)
- `$FULL_DIFF` (the `origin/$BASE_REF...origin/$HEAD_REF` diff from Step 1b)
- Instruction: **focus only on changed files**, not the entire codebase
- **Exclusion list from Step 1c**: list of `path:line` + summary of issues already flagged by Codex bot on the PR. **Do NOT re-report these issues.** Only report them if you have a materially different assessment (e.g., the bot's suggestion is wrong, or the issue is more severe than flagged).
- LSP and AST tools available for deep analysis (`lsp_diagnostics`, `ast_grep_search`)
- For newly introduced wrapper components or utility functions, **read their callsites** before assessing the pattern — the caller's constraints (render props, async context, platform limitations) often justify designs that look unusual in isolation

### Track B: Codex CLI Review

**If `CODEX_AVAILABLE` is true**, run `codex review` in the background simultaneously with Track A.

**Timeout: 600000ms (10 minutes).** Codex review with high reasoning effort can take 5-10 minutes on large diffs. Always set `timeout: 600000` on the Bash tool call that runs `codex review`.

**Important constraints:**
- `codex review --base <branch>` diffs the **currently checked-out branch** against the given base. It does NOT support diffing two arbitrary remote refs.
- `--base` and `[PROMPT]` arguments are **mutually exclusive** — you cannot pass custom review instructions when using `--base`.
- `--title` can be combined with `--base`.

**Execution steps (handles both local and remote branch modes):**

```bash
# Step 1: Ensure we're on the PR head branch
CURRENT_BRANCH=$(git branch --show-current)
NEEDS_CHECKOUT=false

if [ "$CURRENT_BRANCH" != "$HEAD_REF" ]; then
  # Remote branch mode — checkout PR head locally
  NEEDS_CHECKOUT=true
  git fetch origin "$HEAD_REF" 2>/dev/null
  git checkout -B "$HEAD_REF" "origin/$HEAD_REF" 2>/dev/null
fi

# Step 2: Run codex review (no [PROMPT] allowed with --base)
# $CODEX_REVIEW_FLAGS forces the dedicated review profile.
CODEX_OUTPUT=$(codex $CODEX_REVIEW_FLAGS review \
  --base "origin/$BASE_REF" \
  --title "$PR_TITLE" \
  2>&1)
CODEX_EXIT=$?

# Step 3: Return to original branch if we switched
if [ "$NEEDS_CHECKOUT" = true ]; then
  git checkout "$CURRENT_BRANCH" 2>/dev/null
fi

# Step 4: Handle result
if [ $CODEX_EXIT -ne 0 ]; then
  CODEX_SKIP_REASON="codex review exited $CODEX_EXIT: $CODEX_OUTPUT"
fi
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
- Continue with other tracks' results
- Report the failure reason in the final report

**Also check for existing Codex review comments on the PR** (from GitHub Codex bot integration):

```bash
# Fetch inline review comments left by Codex bot
CODEX_PR_COMMENTS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | {path, line: .original_line, body, commit: .original_commit_id}]' \
  2>/dev/null)
```

When compiling the final report:
- Include existing Codex GitHub comments alongside local Codex CLI findings
- For each Codex GitHub comment, assess whether the PR author has already addressed it in subsequent commits (compare `commit_id` of the comment vs the latest PR commit)
- Mark addressed comments as `[Addressed]` and unaddressed ones as `[Open]`
- Avoid duplicating findings that overlap between Codex CLI and Codex GitHub bot

### Track C: Gemini CLI Review

**If `GEMINI_AVAILABLE` is true**, run Gemini analysis in the background simultaneously with Tracks A and B.

**Timeout: 600000ms (10 minutes).** Always set `timeout: 600000` on the Bash tool call that runs `gemini`.

Gemini's strength is its 1M token context window — feed it the **full diff plus surrounding file contents** for holistic analysis:

```bash
# Use --include-directories for broad context, plus the full diff
gemini -m gemini-3.1-pro-preview \
  "You are reviewing a PR (origin/$BASE_REF...origin/$HEAD_REF).

Changed files:
$CHANGED_FILES

Full diff:
$FULL_DIFF

Review this PR for:
1. Architecture and design pattern issues across the changed files
2. Security vulnerabilities (injection, auth bypass, data exposure)
3. Cross-file consistency (are changes in one file properly reflected in related files?)
4. Missing error handling or edge cases
5. Performance concerns (N+1 queries, unnecessary re-renders, memory leaks)

For each issue: file:line, severity (Critical/High/Medium/Low), description, suggested fix.
Also note positive patterns worth keeping." \
  2>&1
```

Save the Gemini output to `$PLAYBOOK_DIR/gemini-review.md`.

**If `GEMINI_AVAILABLE` is false**, record the skip:
```
## Gemini CLI Review: SKIPPED
Reason: [GEMINI_SKIP_REASON]
```

**Handle Gemini runtime failures** — same pattern as Codex: capture error, set `GEMINI_SKIP_REASON`, continue with other tracks.

### Critical Review Mindset (include in every Claude agent prompt)

Every issue found MUST pass these filters before being reported:

1. **Practical Impact Test**: "Would this actually cause a bug or degrade UX in production?" If the effect is idempotent or the theoretical issue has no real-world consequence, downgrade to informational or omit. For `useEffect` double-invocation concerns (StrictMode, cleanup), always check whether the triggered callback is idempotent — if it is, the double-call is harmless and should not be flagged as an issue.
2. **Author Intent Test**: Before flagging a pattern as wrong, investigate *why* the author may have written it this way. Read surrounding code (e.g., is it inside a render prop where hooks can't be called directly? Is there a platform-specific reason?). If the pattern is a deliberate, justified tradeoff, acknowledge it rather than flagging it.
3. **YAGNI Filter**: Before suggesting abstractions, refactors, or extractions (e.g., "extract to a shared hook"), verify that the use cases are actually identical. Check if platform-specific differences, different timing requirements, or other divergences make the abstraction premature.
4. **Devil's Advocate**: For each High/Medium issue, briefly state the strongest counter-argument the PR author might make. If the counter-argument is convincing, reconsider the severity or drop the issue.

## Step 4 — Cross-Validate & Aggregate Report

After all tracks complete, **cross-validate findings** before compiling the final report.

### Cross-Validation Process

1. **Merge findings**: Collect all issues from Claude Code agents (Track A), Codex CLI (Track B), and Gemini CLI (Track C).

2. **Deduplicate against existing PR comments**: Before classifying findings, compare each issue against the exclusion list (Codex bot comments from Step 1c). If an issue is substantially the same as an existing bot comment:
   - **Drop it** from the new findings list
   - Add it to a separate "Already Flagged by Codex Bot" section in the report (with status: Open/Addressed)
   - Only retain it as a new finding if the review agent's assessment materially differs from the bot's (different severity, different root cause, or the bot was wrong)

2. **Classify each finding by agreement level:**
   - **Strong consensus** (2+ sources agree): High confidence — include with boosted credibility. If all 3 sources agree, mark as **unanimous**.
   - **Claude-only**: Only Claude agents found this. Assess whether the other models likely missed it (complex logic requiring deep context) or whether it might be a false positive.
   - **Codex-only**: Only Codex found this. Verify by reading the relevant code.
   - **Gemini-only**: Only Gemini found this. Gemini's large context may catch cross-file issues others miss, but verify specificity — Gemini can be broad.
   - **Contradicted**: Two or more sources disagree on the same code section. **Investigate the actual code** to determine which assessment is correct. State which reviewer(s) were right and why.

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
| Gemini CLI | Completed / SKIPPED | [version or skip reason] |

## Review Matrix
| Reviewer         | Verdict           | Critical | High | Medium | Low |
|-----------------|-------------------|----------|------|--------|-----|
| Code Quality     | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Security         | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Quality/Logic    | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Performance      | APPROVE/COMMENT   |    X     |  X   |   X    |  X  |
| API Compat.      | APPROVE/REQUEST   |    X     |  X   |   X    |  X  |
| Codex CLI        | APPROVE/REQUEST/SKIPPED |  X  |  X   |   X    |  X  |
| Gemini CLI       | APPROVE/REQUEST/SKIPPED |  X  |  X   |   X    |  X  |

---

## Cross-Validation Summary
- **Unanimous findings** (all available sources agree): X issues
- **Strong consensus** (2+ sources agree): X issues
- **Claude-only findings**: X issues (Y verified, Z questionable)
- **Codex-only findings**: X issues (Y verified, Z questionable)
- **Gemini-only findings**: X issues (Y verified, Z questionable)
- **Contradictions resolved**: X (explain which source was correct per issue)

## Existing Codex Bot Comments (pre-review)
| # | File:Line | Summary | Status |
|---|-----------|---------|--------|
| 1 | `file.ts:42` | [brief summary of bot comment] | Open / Addressed |

> These issues were already flagged by the Codex bot on the PR and are excluded from new findings below.

---

## 🔴 Critical Issues (must fix before merge)
- `file.ts:42` — [issue description] — **Source:** [Unanimous / Claude+Codex / Claude+Gemini / Codex+Gemini / Claude-only / Codex-only / Gemini-only] — **Fix:** [concrete fix]

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

### Optional: External CLI Setup

For tri-model review, install and authenticate Codex CLI and/or Gemini CLI:

```bash
# Codex CLI
npm install -g @openai/codex
codex auth

# Gemini CLI
npm install -g @google/gemini-cli
```

If either CLI is not available, the skill works with whichever sources are present. Claude Code agents always run.

## Usage

```
/review-pr                       # review current branch's PR
/review-pr 731                   # review PR #731
/review-pr security              # security-focused review only
/review-pr --no-fix              # analysis only, skip local fixes
/review-pr remote test-check     # review remote branch's PR (no local checkout needed)
/review-pr 731 security          # review PR #731, security focus only
```
