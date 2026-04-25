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
printf "# plan.md\nRun: %s\nTask type: research\n\nScope: PR review — analysis only (no code changes unless Critical/High issues found)\n\nConstraints:\n- Cite file paths and line numbers as evidence\n- No code changes unless user explicitly allows it\n- Specialist reviewers receive the FULL PR body verbatim, not a summary\n- Findings on the same surface as existing Codex bot comments require explicit justification\n\nSkill mapping:\n- Detect context: direct implementation (gh CLI)\n- Agent routing: oh-my-claudecode:analyst\n- Review (Step 3): oh-my-claudecode:code-reviewer + security-reviewer + quality-reviewer + codex review CLI + gemini CLI (parallel)\n- Step 4a: merge + cross-validate + auto-down-rank findings missing author-intent justification\n- Step 4b: verifier round (cross-model devil's advocate) — keeps decision trail\n- Step 4c: reviewer rebuttal mini-loop (only for unanimous/Critical/Verified drops)\n- Fix (conditional): oh-my-claudecode:executor\n" "$RUN_TS" > "$PLAYBOOK_DIR/plan.md"
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

Run these in parallel (use `PR_NUMBER` from 1a). **Capture the FULL PR body verbatim** — it is required input to Step 3, not just a summary. Many false-positives ("scope leak", "undocumented side-effect") are caused by reviewers not seeing the PR body.

1. `gh pr view $PR_NUMBER --json number,title,body,baseRefName,headRefName,additions,deletions,files,headRefOid` — PR metadata. Save `body` verbatim to `$PR_BODY` (do NOT summarize; specialist reviewers receive the full text).
2. `gh pr view $PR_NUMBER --json comments,reviews` — existing review comments and review-level submissions
3. Fetch existing Codex bot inline comments + reviews to build an **exclusion list**:

```bash
# Fetch ALL inline review comments (bot + human reviewers)
ALL_PR_COMMENTS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
  --jq '[.[] | {user: .user.login, path, line: .original_line, body, commit: .original_commit_id}]' \
  2>/dev/null)

# Extract Codex bot comments specifically
CODEX_BOT_COMMENTS=$(echo "$ALL_PR_COMMENTS" | jq '[.[] | select(.user == "chatgpt-codex-connector[bot]")]')

# Fetch review-level submissions (Codex bot leaves a review with "react with 👍" when it has NO concerns)
CODEX_BOT_REVIEWS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/reviews" \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | {state, body, commit_id, submitted_at}]' \
  2>/dev/null)

LATEST_HEAD_SHA=$(gh pr view $PR_NUMBER --json headRefOid -q .headRefOid)
```

**Build a strengthened exclusion list** from existing bot signals:
- For each Codex bot inline comment: capture `path`, `line`, summary, status (Open/Addressed). A comment is **Addressed** when its `commit_id` is older than `LATEST_HEAD_SHA` AND no later bot comment re-raises the same `path:line`.
- For each Codex bot review submission: if `state == "APPROVED"` or body contains "react with 👍" (no-concerns signal) AND its `commit_id == LATEST_HEAD_SHA`, treat the bot as having **signed off on the current head**. Record this prominently in the exclusion list.
- **Surface-neighborhood rule**: any path:line within ±20 lines of an existing bot comment is the *same surface*. New findings on the same surface require explicit justification — see Step 3 author-intent rule.
- The full exclusion list + `$PR_BODY` are passed to **every** review agent in Step 3 and to the verifier in Step 4b.

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
- PR title and **`$PR_BODY` verbatim** (from Step 1c — full text, not a summary). Reviewers MUST read the body before flagging anything as "scope leak", "undocumented", "unexpected change", "unrelated refactor", etc.
- `$FULL_DIFF` (the `origin/$BASE_REF...origin/$HEAD_REF` diff from Step 1b)
- Instruction: **focus only on changed files**, not the entire codebase
- **Exclusion list from Step 1c** with surface-neighborhood rule: list of `path:line` + summary + status (Open/Addressed) of bot comments, plus any "bot signed off on this head commit" signal. The exclusion covers the issue itself **and the same surface ±20 lines**.
  - **Do NOT re-report a bot-flagged issue.**
  - **Do NOT repackage** the same concern as a different finding on the same surface (e.g., bot said "missing aria-label", reviewer says "aria-label content differs across platforms" — same dialog-a11y surface, treat as overlap unless materially distinct).
  - Only file a finding on a bot-cleared surface when the reviewer can articulate, in one line, *why the bot missed this and a human reviewer should re-examine*.
- **Author-intent justification (mandatory for every finding)**: each finding must include a one-line "why this passed the author-intent test" — i.e., why the deliberate design choice the author may have made does NOT explain the pattern. Findings without this justification are auto-down-ranked one severity tier in Step 4.
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

> ⚠️ **Conductor / shared-worktree safety guard**: Before checking out a PR branch, detect whether this worktree is shared with other sessions. If yes, **SKIP Track B entirely** with `CODEX_SKIP_REASON="Conductor/shared worktree — checkout would clobber another session's branch"`. Detection signals (any one is enough):
> - `$CONDUCTOR_WORKSPACE_ID` env var is set, OR
> - the worktree path matches `*/conductor/{workspaces,repo}/*`, OR
> - `git worktree list` shows the same path attached to multiple branches owned by other processes
>
> When skipped, Codex GitHub bot comments still feed the exclusion list — this only disables the *additional* `codex review` CLI run.

```bash
# Step 1: Ensure we're on the PR head branch
CURRENT_BRANCH=$(git branch --show-current)
NEEDS_CHECKOUT=false

# Conductor / shared-worktree detection — bail out before checkout
if [ -n "$CONDUCTOR_WORKSPACE_ID" ] || [[ "$PWD" == */conductor/workspaces/* ]] || [[ "$PWD" == */conductor/repo/* ]]; then
  CODEX_AVAILABLE=false
  CODEX_SKIP_REASON="Conductor/shared worktree — checkout would collide with parallel sessions"
fi

if [ "$CODEX_AVAILABLE" = true ] && [ "$CURRENT_BRANCH" != "$HEAD_REF" ]; then
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
# Try gemini-2.5-pro first, fallback to gemini-2.5-flash on 429/rate-limit
GEMINI_MODEL="gemini-2.5-pro"
REVIEW_PROMPT="You are reviewing a PR (origin/$BASE_REF...origin/$HEAD_REF).

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
Also note positive patterns worth keeping."

GEMINI_OUTPUT=$(gemini -m "$GEMINI_MODEL" "$REVIEW_PROMPT" 2>&1)
GEMINI_EXIT=$?
if [ $GEMINI_EXIT -ne 0 ] && echo "$GEMINI_OUTPUT" | grep -qi "429\|capacity.exhausted\|rate.limit\|quota"; then
  echo "⚠ gemini-2.5-pro rate limited, falling back to gemini-2.5-flash..."
  GEMINI_MODEL="gemini-2.5-flash"
  GEMINI_OUTPUT=$(gemini -m "$GEMINI_MODEL" "$REVIEW_PROMPT" 2>&1)
  GEMINI_EXIT=$?
fi
echo "$GEMINI_OUTPUT"
```

Record which model was actually used (`$GEMINI_MODEL`) for the report — either `[gemini-2.5-pro]` or `[gemini-2.5-flash (fallback)]`.

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
2. **Author Intent Test**: Before flagging a pattern as wrong, investigate *why* the author may have written it this way. Read surrounding code (e.g., is it inside a render prop where hooks can't be called directly? Is there a platform-specific reason?). **Read `$PR_BODY` end-to-end** — if the body explicitly calls out the change ("per Figma", "intentional non-dismissable", "removes legacy fallback"), the change is in-scope and not a side-effect. If the pattern is a deliberate, justified tradeoff, acknowledge it rather than flagging it. **Output the one-line author-intent justification with every finding** — its absence triggers an automatic severity down-rank.
3. **YAGNI Filter**: Before suggesting abstractions, refactors, or extractions (e.g., "extract to a shared hook"), verify that the use cases are actually identical. Check if platform-specific differences, different timing requirements, or other divergences make the abstraction premature.
4. **Devil's Advocate**: For each High/Medium issue, briefly state the strongest counter-argument the PR author might make. If the counter-argument is convincing, reconsider the severity or drop the issue.
5. **Bot-overlap Filter**: For every finding, check the exclusion list (Step 1c). If the finding is on the same surface (path:line ±20 lines) as a Codex bot comment, you must explicitly justify *why this is materially different from the bot's framing* — not "different angle on the same dialog", but a concrete distinct defect. Failing that justification: drop the finding.

## Step 4 — Cross-Validate, Verify & Aggregate Report

After all tracks complete, run **three sub-phases**: merge → verifier round → reviewer rebuttal. Then compile the final report. The verifier and rebuttal phases are STANDARD (not optional). Use `--fast` to skip 4b/4c when latency matters more than quality.

### Step 4a — Merge & cross-validate

1. **Merge findings**: Collect all issues from Claude Code agents (Track A), Codex CLI (Track B), and Gemini CLI (Track C).

2. **Deduplicate against existing PR comments**: Before classifying findings, compare each issue against the exclusion list (Codex bot comments + reviews from Step 1c) using the **surface-neighborhood rule** (±20 lines). If an issue is on the same surface as an existing bot comment:
   - If substantially the same → **drop** and record under "Already Flagged by Codex Bot"
   - If "different angle" without a concrete materially-distinct defect → **drop**
   - Only retain when the reviewer's one-line justification (from Step 3 Bot-overlap Filter) shows a defect the bot truly missed

3. **Apply auto-down-rank**: any finding without an author-intent justification line drops one severity tier (Critical→High, High→Medium, Medium→Low, Low→drop).

4. **Classify each finding by agreement level:**
   - **Strong consensus** (2+ sources agree): High confidence — include with boosted credibility. If all 3 sources agree, mark as **unanimous**.
   - **Claude-only**: Only Claude agents found this. Assess whether the other models likely missed it (complex logic requiring deep context) or whether it might be a false positive.
   - **Codex-only**: Only Codex found this. Verify by reading the relevant code.
   - **Gemini-only**: Only Gemini found this. Gemini's large context may catch cross-file issues others miss, but verify specificity — Gemini can be broad.
   - **Contradicted**: Two or more sources disagree on the same code section. **Investigate the actual code** to determine which assessment is correct. State which reviewer(s) were right and why.

5. **Accuracy judgment**: For each non-corroborated finding, add a brief accuracy assessment:
   - `[Verified]` — manually confirmed by reading the code
   - `[Likely valid]` — consistent with codebase patterns but not manually verified
   - `[Questionable]` — may be a false positive; include the counter-argument
   - `[Dismissed]` — determined to be incorrect after investigation; explain why

### Step 4b — Verifier round (cross-model devil's advocate)

Pass the merged finding list to a **different model than the originator** and ask for keep/drop verdicts. Goal: catch over-flagging by reviewers who default to "log if uncertain".

**Verifier model selection** (reverse the strongest reviewer to avoid self-validation):
- If Track A (Claude agents) produced most findings → verifier = Codex CLI (`codex --profile review exec`) when available, else Gemini
- If Track B (Codex CLI) was the dominant source → verifier = Claude opus via `Agent(subagent_type="oh-my-claudecode:critic", model="opus")`
- If only one source ran → verifier = the strongest available alternative

**Verifier input** (pass *everything*):
- All findings from Step 4a (with severity, source attribution, author-intent justification)
- `$PR_BODY` verbatim
- The exclusion list from Step 1c (so verifier can flag missed bot-overlaps)
- Latest commit SHA + relevant file contents (read via `gh api .../contents/<path>?ref=$LATEST_HEAD_SHA`)

**Verifier asks for each finding**:
1. **Verdict**: AGREE / PARTIAL / DISAGREE
2. **Severity adjustment**: keep / upgrade / downgrade
3. **Bot-overlap**: duplicate / partial / separate
4. **One-line counter-argument** the PR author could reasonably make
5. **Recommendation**: keep / drop / merge with another finding

**Apply verifier verdicts**:
- DISAGREE + drop → move to "Verifier-dropped" section (kept in report for trail, NOT in main severity buckets)
- AGREE → keep as-is
- PARTIAL → apply severity adjustment
- Save verifier output to `$PLAYBOOK_DIR/verifier-output.md`

Timeout: 600000ms. If verifier fails (rate-limit, network, etc.), record `VERIFIER_SKIP_REASON` and continue with Step 4a output as-is — do NOT silently keep all findings as if verified.

### Step 4c — Reviewer rebuttal (mini-loop)

For findings the verifier dropped that meet **any** of these criteria:
- Marked **unanimous** (all 3 sources agreed) in Step 4a, OR
- Severity was **Critical** before any down-ranking, OR
- Tagged `[Verified]` (manually confirmed by reading code)

Send a one-shot rebuttal request back to the original reviewer source: "Verifier dropped this with reason X. Can you point to a concrete defect in the latest commit, or do you concede?"

- If reviewer concedes → finding stays dropped
- If reviewer points to concrete code evidence → finding is **reinstated** with note `[Reinstated after rebuttal]` and the verifier's counter recorded

This loop runs **once**, not iteratively. Save rebuttal exchanges to `$PLAYBOOK_DIR/rebuttal-output.md`.

Skip 4c when `--no-rebuttal` flag is passed or when 4b was skipped.

### Unified Report Format

```
# PR Review: [PR Title] (#[number])
Base: [baseRef] <- Head: [headRef] | +[additions] / -[deletions] lines

## Review Sources
| Source | Status | Notes |
|--------|--------|-------|
| Claude Code (specialist agents) | Completed | [list of agents used] |
| Codex CLI | Completed / SKIPPED | [version or skip reason — note Conductor-skip if applicable] |
| Gemini CLI | Completed / SKIPPED | [gemini-2.5-pro] or [gemini-2.5-flash (fallback)] or [skip reason] |
| **Verifier (Step 4b)** | Completed / SKIPPED | [model used + drop count] |
| **Reviewer rebuttal (Step 4c)** | Completed / SKIPPED | [reinstate count] |

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

## 🟣 Verifier-dropped Findings (decision trail)
- `file.ts:N` — [original finding] — **Verifier reason:** [why dropped] — **Original source:** [reviewer]
> These are kept for transparency, NOT requested as changes. If the verifier was wrong, see Reinstated below.

## ♻️ Reinstated After Rebuttal (Step 4c)
- `file.ts:N` — [finding] — **Verifier dropped because:** [X] — **Reviewer reinstated because:** [concrete evidence Y]

---

## Final Verdict: APPROVE / REQUEST CHANGES / COMMENT
[1-2 sentence rationale based on highest severity found AFTER verifier+rebuttal. Cite cross-validation confidence: "verified by N/M sources, M-N dropped by verifier".]
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
/review-pr                       # review current branch's PR (full pipeline: 4a + 4b + 4c)
/review-pr 731                   # review PR #731
/review-pr security              # security-focused review only
/review-pr --no-fix              # analysis only, skip Step 5 local fixes
/review-pr --fast                # skip Step 4b verifier + 4c rebuttal (~10min faster, more noise)
/review-pr --no-rebuttal         # run verifier (4b) but skip rebuttal mini-loop (4c)
/review-pr remote test-check     # review remote branch's PR (no local checkout needed)
/review-pr 731 security          # review PR #731, security focus only
```

### Flag parsing addendum (extend Step 1a)

| `$ARGUMENTS` pattern | Action |
|---|---|
| Contains `--fast` | Set `FAST=true` → Step 4b and 4c are skipped. Strip flag, continue parsing. |
| Contains `--no-rebuttal` | Set `NO_REBUTTAL=true` → Step 4c is skipped. Strip flag, continue parsing. |
