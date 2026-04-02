# Review PR Comments

Triggers: "PR 코멘트 분석", "리뷰 코멘트 확인", "review pr comments", "analyze pr feedback", "PR 리뷰 처리"

GitHub PR 리뷰 코멘트를 분석하고, 수정 후 리뷰어에게 자동으로 응답을 작성해주는 스킬입니다.
Claude Code와 Codex/Gemini CLI를 병렬로 실행하여 리뷰 코멘트를 분석하고, 결과를 교차 검증하여 정확도 높은 리포트를 생성합니다.

## Installation

```bash
# Clone repository
git clone https://github.com/AhyoungRyu/claude-code.git

# Install this skill
cp -r claude-code/skills/review-pr-comments ~/.claude/skills/

# Install humanizer dependency
cp -r claude-code/skills/humanizer ~/.claude/skills/
```

### Optional: Codex CLI Setup

For dual-model analysis, install and authenticate Codex CLI:
```bash
npm install -g @openai/codex   # or: brew install codex
codex login
```
If Codex CLI is not available, the skill works normally with Claude Code only.

## Usage

```bash
# 기본: PR 코멘트 분석 및 로컬 수정
/review-pr-comments
/review-pr-comments 1753

# 수정 후 리뷰어에게 응답 포스팅
/review-pr-comments --post-response --draft
/review-pr-comments --post-response --reviewer bang9
/review-pr-comments --post-response --commit --since=2026-02-11T14:00:00+09:00

# Codex bot 코멘트가 0개가 될 때까지 반복 (분석 → 수정 → squash → push → 대기)
/review-pr-comments --loop
/review-pr-comments --loop --resolve-bot
```

---

You are an expert AI assistant helping with GitHub PR code review processing. Analyzes recent pull request comments using both Claude Code and Codex CLI in parallel, cross-validates results, and generates an action plan for code updates.

## Step 0 — Check Codex CLI Availability

Before starting analysis, check if Codex CLI is available:

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

# Use the coding profile for exec-based analysis tasks.
# This keeps heavy custom prompts on the higher-effort coding path.
CODEX_EXEC_FLAGS="--profile coding"
```

**Gemini CLI:**
```bash
GEMINI_AVAILABLE=false
GEMINI_SKIP_REASON=""
if ! command -v gemini &>/dev/null; then
  GEMINI_SKIP_REASON="gemini CLI not found in PATH"
else
  GEMINI_VERSION=$(gemini --version 2>&1)
  GEMINI_AVAILABLE=true
fi
```

Store `CODEX_AVAILABLE`, `CODEX_SKIP_REASON`, `GEMINI_AVAILABLE`, and `GEMINI_SKIP_REASON` for later steps.

## Behavior

- If no PR link or branch name is provided, the command assumes the relevant branch is the one discussed in the ongoing conversation context.
- Filters out already-resolved feedback by comparing your request timestamp with the latest commit timestamp—only newer comments from teammates (excluding AhyoungRyu) are included.
- Designed to surface unresolved, valid feedback after recent PR updates.

### Natural Language Pattern Recognition

When user uses natural language instead of explicit flags, detect intent and map to appropriate options:

**Draft mode detection:**
- "초안", "draft", "먼저 보여줘", "확인하고", "검토하고" → Add `--draft` flag
- Example: "PR 코멘트 분석하고 응답 초안 작성해줘" → `--post-response --draft`

**Post response detection:**
- "응답", "답변", "리뷰어에게", "포스팅", "댓글" → Add `--post-response` flag
- Example: "리뷰 코멘트 확인하고 응답 달아줘" → `--post-response`

**Commit detection:**
- "커밋하고", "push하고", "commit and", "먼저 커밋" → Add `--commit` flag
- Example: "PR 코멘트 반영하고 커밋한 다음 응답 달아줘" → `--post-response --commit`

**Reviewer detection:**
- "@{username}" or "{username}의" or "from {username}" → Add `--reviewer={username}`
- Example: "bang9의 리뷰 코멘트 확인해줘" → `--reviewer=bang9`
- Example: "@ahyoungryu 코멘트 분석" → `--reviewer=ahyoungryu`

**Loop mode detection:**
- "반복", "loop", "계속", "다 해결될 때까지", "codex bot 코멘트 없을 때까지", "until no comments" → Add `--loop` flag
- Example: "codex bot 코멘트 없을 때까지 반복해줘" → `--loop --resolve-bot`
- Example: "PR 코멘트 반복 처리" → `--loop`

**Resolve bot detection:**
- "봇 정리", "codex 정리", "resolve bot", "hide bot comments", "봇 코멘트 숨기기" → Add `--resolve-bot` flag
- "전부 정리", "all bot", "resolve all" → Add `--resolve-bot=all` flag
- Example: "PR 코멘트 반영하고 codex 봇 코멘트 정리해줘" → `--post-response --commit --resolve-bot`

**Default behavior (no flags detected):**
- Simple triggers like "PR 코멘트 분석" → Default mode (analyze and apply feedback locally, no posting)

---

## Default Mode: Analyze and Apply Feedback

### Step 1 — Fetch PR comments

- Get all code review comments from the specified PR
- Filter for recent, unresolved comments from teammates
- **Identify Codex bot comments separately**: Comments from `chatgpt-codex-connector[bot]` are pre-existing automated feedback. Build an exclusion list of `path:line` + summary. These are not re-analyzed in Step 2 — instead, they are listed in the final report as "Pre-existing Bot Feedback" with Open/Addressed status.

**Also check for existing Codex review comments on the PR** (from GitHub Codex bot integration):

```bash
# Fetch inline review comments left by Codex bot
# Include node_id (for GraphQL minimize) and pull_request_review_id (for resolve)
CODEX_PR_COMMENTS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | {id, node_id, path, line: .original_line, body, commit: .original_commit_id, pull_request_review_id}]' \
  2>/dev/null)

# Also fetch the main review comment (top-level review body) from Codex bot
CODEX_REVIEW_COMMENTS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/reviews" \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | {id, node_id, body, state, commit_id}]' \
  2>/dev/null)
```

When processing Codex bot comments:
- For each Codex GitHub comment, assess whether the PR author has already addressed it in subsequent commits (compare `commit_id` of the comment vs the latest PR commit)
- Mark addressed comments as `[Addressed]` and unaddressed ones as `[Open]`
- Include unaddressed Codex bot comments alongside human reviewer comments for analysis
- Store `node_id` values for later use in Step 5 (hide & resolve)

### Step 2 — Dual-Model Analysis (Parallel)

Launch **two analysis tracks** simultaneously:

#### Track A: Claude Code Analysis

Analyze each **human reviewer** comment critically using Claude Code (skip Codex bot comments — they are reported separately):
- Is it technically valid?
- Is it actionable?
- Is it aligned with best practices?

For each comment:
- **If YES (valid feedback):** Suggest how to incorporate it into the code
- **If NO (not appropriate):** Explain why and propose an alternative if necessary

Process comments sequentially:
- Summarize the intent
- Assess validity
- Propose action plan (if applicable) or reasoned rejection

#### Track B: Codex CLI Analysis

**If `CODEX_AVAILABLE` is true**, run Codex analysis in the background simultaneously with Track A:

```bash
# Prepare a prompt with all PR comments for Codex to analyze
# Include: comment body, file path, line number, reviewer, and the current code context
# $CODEX_EXEC_FLAGS forces the coding profile.
codex $CODEX_EXEC_FLAGS exec \
  "Analyze the following PR review comments. For each comment, assess:
   1. Is the feedback technically valid? Why or why not?
   2. What is the recommended action (accept/reject/modify)?
   3. If accepting, what specific code change should be made?
   4. Severity: Critical/High/Medium/Low

   PR Comments:
   [paste formatted comments here]

   Current code context:
   [paste relevant git diff or file contents]" \
  2>&1
```

**If `CODEX_AVAILABLE` is false**, record:
```
Codex CLI analysis: SKIPPED
Reason: [CODEX_SKIP_REASON]
```

**Handle Codex runtime failures** — if the command exits non-zero:
- Capture the error output (e.g., "Authentication failed", "Rate limit exceeded")
- Set `CODEX_SKIP_REASON` to the actual error message
- Continue with Claude Code results only

### Step 3 — Cross-Validate Results

After both tracks complete, cross-validate the analyses:

1. **For each PR comment**, compare Claude and Codex assessments:
   - **Both agree (valid):** High confidence — proceed with suggested fix
   - **Both agree (reject):** High confidence — skip with clear rationale
   - **Disagree:** Investigate the actual code to determine which assessment is correct
     - State which reviewer was right and why
     - Tag as `[Claude correct]` or `[Codex correct]`

2. **Generate a cross-validation summary:**
   ```
   ## Analysis Sources
   | Source | Status |
   |--------|--------|
   | Claude Code | Completed |
   | Codex CLI | Completed / SKIPPED ([reason]) |

   ## Cross-Validation
   - Agreed (accept): X comments
   - Agreed (reject): Y comments
   - Disagreed: Z comments (Claude correct: A, Codex correct: B)
   ```

### Step 4 — Present Action Plan

Present the unified, cross-validated plan:
- Which changes to make
- In what order
- Why each change is needed
- Confidence level (corroborated by both / single-source)
- For disagreements, explain the resolution

### Rules

- You are **not required to apply every piece of feedback**. Prioritize correct and valuable feedback over blindly following all suggestions.
- Be objective and comprehensive in your analysis.
- Always reason about **why** a suggestion is good or bad before recommending code changes.
- Refer to the current code implementation in the PR to make informed judgments.
- **Do NOT commit changes or push** in this mode. Just analysis and local changes.
- **Cross-validation disagreements must be resolved** — never present contradictory recommendations without a verdict.

### Step 5 — Hide & Resolve Codex Bot Comments

After addressing Codex bot feedback (either in Default Mode or Extended Mode with `--commit`), automatically hide and resolve the bot's PR comments so they don't clutter the review.

**CRITICAL — Three independent operations, ALL required:**
1. **Reply** (5a) — post resolution status as reply to each comment
2. **Minimize** (5b) — hide comment body in PR UI (`minimizeComment` mutation)
3. **Resolve** (5c) — collapse review thread (`resolveReviewThread` mutation)

Skipping minimize but doing resolve will leave comment bodies visible. Skipping resolve but doing minimize will leave threads expanded. **Always execute all three.**

**CRITICAL — GraphQL vs REST `author.login` mismatch:**
- REST API `user.login` → `"chatgpt-codex-connector[bot]"` (WITH `[bot]` suffix)
- GraphQL `author { login }` → `"chatgpt-codex-connector"` (WITHOUT `[bot]` suffix)
- **Always use REST API for collecting bot comment/review node_ids.** Use GraphQL only for mutations and thread queries (with the correct login string).

**When to run:**
- After all Codex bot comments marked `[Addressed]` have been fixed in code
- When `--resolve-bot` flag is passed (explicit), OR when `--post-response --commit` is used (implicit — addressed comments are auto-resolved after push)
- Use `--resolve-bot=all` to resolve ALL Codex bot comments (including `[Open]` ones)
- Default: only resolve `[Addressed]` comments

**Step 5a — Reply to each bot comment with resolution status:**

Before minimizing, reply to each Codex bot comment explaining how it was addressed.

```bash
for COMMENT_ID in $ALL_CODEX_COMMENT_IDS; do
  gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments/$COMMENT_ID/replies" \
    -X POST -f body="$REPLY_BODY" 2>/dev/null
done
```

Reply format per status:
- **Addressed:** `Addressed — {brief description of the code change made}`
- **Phase 2 defer:** `Phase 2 defer — {reason why it requires future work and what's tracked}`
- **Partially addressed:** `Partially addressed — {what was done} / {what remains for Phase 2}`
- **Out of scope:** `Out of scope — {brief reason why this is outside the PR's scope}`

**Step 5b — Minimize ALL bot nodes (inline comments + top-level review bodies):**

Both `PRRC_*` (inline) and `PRR_*` (top-level review) node IDs use the same `minimizeComment` mutation. Collect via REST API, then minimize via GraphQL.

```bash
# Collect inline comment node_ids via REST (reliable — uses user.login WITH [bot])
COMMENT_NODE_IDS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" --paginate \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | .node_id] | .[]')

# Collect top-level review body node_ids via REST
REVIEW_NODE_IDS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/reviews" \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | .node_id] | .[]')

# Minimize ALL — both inline comments and review bodies
for NODE_ID in $COMMENT_NODE_IDS $REVIEW_NODE_IDS; do
  gh api graphql -f query='
    mutation {
      minimizeComment(input: {subjectId: "'"$NODE_ID"'", classifier: RESOLVED}) {
        minimizedComment { isMinimized }
      }
    }' 2>/dev/null
done
```

**Step 5c — Resolve review threads:**

Collect thread IDs via GraphQL (threads only exist in GraphQL). Use `"chatgpt-codex-connector"` (WITHOUT `[bot]`) for the GraphQL `author.login` filter.

```bash
# Get unresolved thread IDs — note: GraphQL uses login WITHOUT [bot]
THREAD_IDS=$(gh api graphql -f query='
{
  repository(owner: "OWNER", name: "REPO") {
    pullRequest(number: PR_NUMBER) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes { author { login } }
          }
        }
      }
    }
  }
}' --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.comments.nodes[0].author.login == "chatgpt-codex-connector")
  | select(.isResolved == false)
  | .id] | .[]')

for THREAD_ID in $THREAD_IDS; do
  gh api graphql -f query='
    mutation {
      resolveReviewThread(input: {threadId: "'"$THREAD_ID"'"}) {
        thread { isResolved }
      }
    }' 2>/dev/null
done
```

**Step 5d — Verify nothing was missed:**

Re-check via REST API (reliable bot detection) that all comments are minimized.

```bash
# Check inline comments
REMAINING_INLINE=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" --paginate \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | .node_id] | .[]')

for NODE_ID in $REMAINING_INLINE; do
  IS_MIN=$(gh api graphql -f query='{ node(id: "'"$NODE_ID"'") { ... on PullRequestReviewComment { isMinimized } } }' \
    --jq '.data.node.isMinimized' 2>/dev/null)
  if [ "$IS_MIN" != "true" ]; then
    echo "MISSED inline: $NODE_ID — retrying..."
    gh api graphql -f query='mutation { minimizeComment(input: {subjectId: "'"$NODE_ID"'", classifier: RESOLVED}) { minimizedComment { isMinimized } } }' 2>/dev/null
  fi
done

# Check review bodies — use node(id) to verify minimize status
REMAINING_REVIEWS=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/reviews" \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | .node_id] | .[]')

for NODE_ID in $REMAINING_REVIEWS; do
  IS_MIN=$(gh api graphql -f query='{ node(id: "'"$NODE_ID"'") { ... on PullRequestReview { body } } }' \
    --jq '.data.node.body' 2>/dev/null)
  # Review bodies: re-minimize if body is non-empty (means it wasn't minimized)
  if [ -n "$IS_MIN" ] && [ "$IS_MIN" != "null" ]; then
    gh api graphql -f query='mutation { minimizeComment(input: {subjectId: "'"$NODE_ID"'", classifier: RESOLVED}) { minimizedComment { isMinimized } } }' 2>/dev/null
  fi
done

echo "Verification complete."
```

**Error handling:**
- If `gh api graphql` fails (e.g., insufficient permissions), log the error and continue with remaining comments
- Report which comments were successfully resolved and which failed
- Common failure: the `GITHUB_TOKEN` may lack `write:discussion` scope — warn the user if 403 is returned

**Report output:**
```
## Codex Bot Comments: Reply, Hide & Resolve
- ✅ Replied: X inline comments (Addressed: A, Phase 2 defer: B, Out of scope: C)
- ✅ Minimized: X inline comments + Y top-level review bodies
- ✅ Resolved: X review threads
- ✅ Verified: 0 remaining un-minimized bot comments
- ❌ Failed: 0
```

---

## Extended Mode: Post Response to Reviewer

When `--post-response` flag is used, the workflow shifts to generating a summary of addressed feedback and posting it back to the PR.

### Purpose

After addressing PR comments locally, generate a concise summary of what was changed and post it back to the PR, mentioning the reviewer with natural, human-sounding language.

### Workflow

1. **Identify context**
   - Determine PR number from current branch or conversation context
   - Identify reviewer from `--reviewer` flag or auto-detect from recent comments
   - Default to checking today's comments unless `--since` is specified

2. **Analyze current state**
   - Fetch reviewer's comments within the specified timeframe
   - For each comment, compare against current code to determine:
     - ✅ **Fully addressed** - code change matches feedback
     - ⚠️ **Partially addressed** - some aspects implemented, others not
     - ❌ **Not addressed** - intentionally skipped (provide reason)

3. **Generate response summary**
   - Use `/humanizer` skill to ensure natural, non-AI-sounding language
   - Auto-detect language (Korean/English) based on reviewer's comment language
   - Format with clear structure and concise descriptions
   - **Always include footer:** `<sub>Written by Claude Code</sub>`

4. **Review and post**
   - If `--draft` flag: show draft to user for approval before posting
   - If no `--draft`: post immediately using `gh pr comment`
   - If `--commit` flag: automatically commit and push changes before posting

### Auto-detect Language Rules

- **Korean**: If 80%+ of reviewer's comments are in Korean
- **English**: Otherwise
- Always apply humanizer to remove generic AI patterns
- Keep tone professional but conversational

### Response Format Template (Korean)

```
@{reviewer}

{intro - e.g., "오늘 남겨주신 코멘트들 모두 반영했습니다!"}

### 반영 내용

**1. {title}** (line {number})
- {concise description ending with: 수정 / 반영 / 제거 / 추가}

**2. {title}** (line {number})  
- {concise description}

**3. {title}** (line {number})
- {concise description}

---
<sub>Written by Claude Code</sub>
```

**Korean formatting rules:**
- Main intro can use "습니다" formal ending
- Subsection bullets should end with action words: 수정, 반영, 제거, 추가, 변경, 개선
- Avoid "습니다/했습니다" in subsection descriptions
- Keep descriptions to 1-2 lines maximum

### Response Format Template (English)

```
@{reviewer}

{intro - e.g., "All comments have been addressed!"}

### Changes Made

**1. {title}** (line {number})
- {concise description}

**2. {title}** (line {number})
- {concise description}

**3. {title}** (line {number})
- {concise description}

---
<sub>Written by Claude Code</sub>
```

**English formatting rules:**
- Use active voice
- Start with action verbs: Updated, Fixed, Removed, Added, Refactored
- Keep descriptions concise and specific

### Flags and Options

| Flag | Description | Format Examples |
|------|-------------|-----------------|
| `--post-response` | Activate response posting mode | N/A |
| `--draft` | Show draft for approval before posting | N/A |
| `--commit` | Commit and push changes before posting response | N/A |
| `--reviewer=username` | Specify reviewer (auto-detected if omitted) | `--reviewer=bang9` |
| `--since=TIMESTAMP` | Check comments since this timestamp | `2026-02-11` (date only)<br>`2026-02-11T14:30:00+09:00` (with time & timezone)<br>`2026-02-11T05:30:00Z` (UTC) |
| `--pr=number` | Specify PR number (auto-detected from branch if omitted) | `--pr=1753` |
| `--resolve-bot` | Hide & resolve addressed Codex bot comments (auto with `--post-response --commit`) | `--resolve-bot`, `--resolve-bot=all` |
| `--loop` | Repeat until codex bot leaves 0 new comments (analyze → fix → squash → push → wait → check) | `--loop`, `--loop --resolve-bot` |

**Timestamp format (ISO 8601):**
- Date only: `YYYY-MM-DD`
- Date + time: `YYYY-MM-DDTHH:MM:SS`
- Date + time + timezone: `YYYY-MM-DDTHH:MM:SS+09:00` (Korean time)
- Date + time + UTC: `YYYY-MM-DDTHH:MM:SSZ`

### Examples

**Generate draft response:**
```
/review-pr-comments --post-response --draft
```
→ Shows the generated comment, waits for user confirmation, then posts

**Auto-post with commit:**
```
/review-pr-comments --post-response --commit --reviewer bang9
```
→ Commits changes, pushes to remote, then posts response immediately

**Check specific date range:**
```
/review-pr-comments --post-response --since=2026-02-10
```
→ Analyzes comments from Feb 10 onwards

### Humanizer Integration

Always use the `/humanizer` skill to:
- Remove AI vocabulary: "Additionally", "Moreover", "Furthermore", etc.
- Avoid inflated symbolism: "testament to", "underscores", "pivotal"
- Remove promotional language: "vibrant", "robust", "seamless"
- Use simple constructions: prefer "is/are/has" over "serves as/stands as"
- Vary sentence structure naturally
- Keep tone professional but not robotic

**Before humanizer:**
> Additionally, the _isSendable method has been updated to align with the DefaultStatCollector pattern, ensuring consistency across the codebase. Moreover, the test structure has been refactored to provide more comprehensive coverage.

**After humanizer:**
> `_isSendable` 메서드를 DefaultStatCollector 패턴과 동일하게 수정. 테스트 구조도 개선해서 threshold 정책을 제대로 검증하도록 변경.

---

## Implementation Notes

- Use `gh pr view` and `gh api` commands to fetch PR data
- Parse JSON responses to extract comments, timestamps, and metadata
- Use `git diff` to compare changes between commits
- Always verify PR number and reviewer before posting
- Handle edge cases: no comments found, PR already merged, invalid reviewer
- **Always append footer:** `---\n<sub>Written by Claude Code</sub>` at the end of every posted comment

---

## GitHub Links in Response

When posting responses, automatically include relevant GitHub links for easy verification:

### Link Types

1. **Commit links** - If `--commit` flag used, link to the commit
2. **File/line links** - Link to specific changed lines
3. **Diff links** - Link to compare view for multiple files

### Implementation

**Get commit hash:**
```bash
git rev-parse HEAD
```

**Generate links:**
- Commit: `https://github.com/{owner}/{repo}/commit/{hash}`
- File line: `https://github.com/{owner}/{repo}/blob/{branch}/{file}#L{line}`
- Compare: `https://github.com/{owner}/{repo}/compare/{base}...{head}`

### Updated Response Format

**Korean with links:**
```
@reviewer

오늘 남겨주신 코멘트들 모두 반영했습니다!

### 반영 내용

**1. `_isSendable` 메서드를 DefaultStatCollector 패턴과 동일하게 수정** ([#19](https://github.com/org/repo/blob/branch/src/stat/aiAgentStatCollector.ts#L19))
- DefaultStatCollector와 동일한 패턴으로 수정

**2. appendStat 문서화 최소화** ([#405](https://github.com/org/repo/blob/branch/src/module/aiAgentModule.ts#L405))
- 내부용 API라서 상세한 파라미터 설명 제거, `@experimental` 어노테이션만 남김

**3. "more than limit pending" 테스트 수정** ([#90](https://github.com/org/repo/blob/branch/test/v3/case/stat/aiAgentStatCollector.test.ts#L90))
- ready 블록에서 `context.connect()` 호출 제거
- assertion을 `flushWaitQueue.toHaveLength(numberOfLogs)`로 변경

### 관련 커밋
- [24a163e](https://github.com/org/repo/commit/24a163e27) - test: address PR review comments for AI Agent stats

---
<sub>Written by Claude Code</sub>
```

**English with links:**
```
@reviewer

All comments have been addressed!

### Changes Made

**1. Updated `_isSendable` method to match DefaultStatCollector** ([#19](https://github.com/org/repo/blob/branch/src/stat/aiAgentStatCollector.ts#L19))
- Updated to match DefaultStatCollector pattern

**2. Minimized appendStat documentation** ([#405](https://github.com/org/repo/blob/branch/src/module/aiAgentModule.ts#L405))
- Removed detailed parameter descriptions, kept only `@experimental` annotation

**3. Fixed "more than limit pending" test** ([#90](https://github.com/org/repo/blob/branch/test/v3/case/stat/aiAgentStatCollector.test.ts#L90))
- Removed `context.connect()` call from ready block
- Changed assertion to verify PENDING state

### Related Commits
- [24a163e](https://github.com/org/repo/commit/24a163e27) - test: address PR review comments for AI Agent stats

---
<sub>Written by Claude Code</sub>
```

### Link Format Examples

**File/line link:**
```markdown
[#42](https://github.com/sendbird/chat-js/blob/feat/ai-agent-stats/src/module/aiAgentModule.ts#L42)
```

**Commit link:**
```markdown
[24a163e](https://github.com/sendbird/chat-js/commit/24a163e27)
```

**Range link (multiple lines):**
```markdown
[#90-131](https://github.com/sendbird/chat-js/blob/feat/ai-agent-stats/test/aiAgentStatCollector.test.ts#L90-L131)
```

### Auto-detection Logic

1. **Extract repo info:**
   ```bash
   git remote get-url origin
   # git@github.com:sendbird/chat-js.git
   ```

2. **Parse owner/repo:**
   ```bash
   # Extract: sendbird/chat-js
   ```

3. **Get current branch:**
   ```bash
   git branch --show-current
   # feat/ai-agent-stats
   ```

4. **Build URLs:**
   - Base: `https://github.com/{owner}/{repo}`
   - Blob: `{base}/blob/{branch}/{file}#L{line}`
   - Commit: `{base}/commit/{hash}`

### Benefits

- ✅ Reviewers can click to see exact changes
- ✅ Faster verification
- ✅ Clear audit trail
- ✅ Professional response format
- ✅ Works with GitHub's native line highlighting

---

## Loop Mode: Resolve All Codex Bot Comments

When `--loop` flag is used, the skill enters a loop that repeats the full review-fix-push cycle until the codex bot has no more new comments to leave.

### When to use

- After pushing code to a PR that has Codex bot auto-review enabled
- When you want to resolve all codex bot feedback in one go without manual back-and-forth

### Prerequisites

- Current branch must be tracking a remote branch with an open PR
- `gh` CLI must be authenticated

### Loop Workflow

```
┌─────────────────────────────────────────────────┐
│ Step 0: Detect current state                    │
│  - Identify PR number from current branch       │
│  - Check for unpushed commits → push if needed  │
│  - Record LOOP_START_TIMESTAMP                  │
│  - Set ITERATION = 0, MAX_ITERATIONS = 5        │
└────────────────────┬────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────┐
│ Step 1: Wait for codex bot                      │
│  - Wait up to 3 minutes (poll every 30s)        │
│  - Check for NEW codex bot comments since        │
│    last push timestamp                          │
│  - If 0 new comments after 3 min → EXIT (done) │
└────────────────────┬────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────┐
│ Step 2: Analyze & Fix                           │
│  - Run standard review-pr-comments flow         │
│    (Step 1–4 from Default Mode)                 │
│  - Apply code fixes for valid feedback          │
│  - If --resolve-bot: resolve addressed comments │
└────────────────────┬────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────┐
│ Step 3: Squash & Push                           │
│  - Squash all loop-iteration commits into one   │
│    using git reset --soft (see Squash section)  │
│  - Commit with message:                         │
│    "fix: address codex bot review (iteration N)"│
│  - Push with --force-with-lease                 │
│  - Record LAST_PUSH_TIMESTAMP                   │
│  - ITERATION++                                  │
│  - If ITERATION >= MAX_ITERATIONS → EXIT (max)  │
└────────────────────┬────────────────────────────┘
                     ▼
              (back to Step 1)
```

### Step 0 — Detect Current State

```bash
# Get PR number
PR_NUMBER=$(gh pr view --json number --jq '.number' 2>/dev/null)
if [ -z "$PR_NUMBER" ]; then
  echo "Error: No open PR found for current branch"
  exit 1
fi

# Check for unpushed commits
UNPUSHED=$(git log @{u}..HEAD --oneline 2>/dev/null)
if [ -n "$UNPUSHED" ]; then
  echo "Unpushed commits detected — pushing first..."
  git push
fi

LOOP_START_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LAST_PUSH_TIMESTAMP=$(git log -1 --format="%aI" origin/$(git branch --show-current))
ITERATION=0
MAX_ITERATIONS=5
# Track the merge-base for squashing — this is the commit BEFORE any loop work
SQUASH_BASE=$(git rev-parse HEAD)
```

### Step 1 — Wait for Codex Bot & Check New Comments

Poll for new codex bot comments. The termination condition is **0 new comments after the wait period**.

```bash
WAIT_SECONDS=180   # 3 minutes max wait
POLL_INTERVAL=30   # check every 30 seconds
ELAPSED=0
NEW_COMMENT_COUNT=0

while [ $ELAPSED -lt $WAIT_SECONDS ]; do
  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  # Count codex bot comments created after LAST_PUSH_TIMESTAMP
  NEW_COMMENT_COUNT=$(gh api "repos/{owner}/{repo}/pulls/$PR_NUMBER/comments" \
    --jq "[.[] | select(.user.login == \"chatgpt-codex-connector[bot]\") | select(.created_at > \"$LAST_PUSH_TIMESTAMP\")] | length" \
    2>/dev/null)

  echo "  [${ELAPSED}s] New codex bot comments: $NEW_COMMENT_COUNT"

  # If comments found, stop waiting early — there's work to do
  if [ "$NEW_COMMENT_COUNT" -gt 0 ]; then
    break
  fi
done

if [ "$NEW_COMMENT_COUNT" -eq 0 ]; then
  echo "✅ No new codex bot comments after ${WAIT_SECONDS}s. Loop complete!"
  # → proceed to final report
fi
```

### Step 2 — Analyze & Fix

Run the standard Default Mode flow (Steps 1–4) targeting only the new codex bot comments:

- Filter comments to only those created after `LAST_PUSH_TIMESTAMP`
- Filter to only `chatgpt-codex-connector[bot]` user
- Apply the dual-model analysis (Track A + Track B) as normal
- Apply code fixes
- If `--resolve-bot` flag is active, run Step 5 (Hide & Resolve) for addressed comments

### Step 3 — Squash & Push

Squash all commits made during loop iterations into a single commit, then push.

```bash
# Count commits since SQUASH_BASE
COMMIT_COUNT=$(git rev-list ${SQUASH_BASE}..HEAD --count)

if [ "$COMMIT_COUNT" -gt 1 ]; then
  echo "Squashing $COMMIT_COUNT commits since loop start..."

  # Soft reset to SQUASH_BASE — keeps all changes staged
  git reset --soft $SQUASH_BASE

  # Create single squashed commit
  git commit -m "$(cat <<'EOF'
fix: address codex bot review feedback

Automated loop: analyzed and fixed codex bot review comments.
EOF
)"
fi

# Push with safety
git push --force-with-lease origin $(git branch --show-current)

# Update timestamp for next iteration
LAST_PUSH_TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
ITERATION=$((ITERATION + 1))

if [ "$ITERATION" -ge "$MAX_ITERATIONS" ]; then
  echo "⚠️ Reached max iterations ($MAX_ITERATIONS). Stopping loop."
  # → proceed to final report
fi
```

### Final Report

After the loop exits (either by 0 new comments or max iterations), output a summary:

```
## 🔄 Codex Bot Review Loop — Complete

| Metric | Value |
|--------|-------|
| Total iterations | {ITERATION} |
| Exit reason | No new comments / Max iterations reached |
| Comments addressed | {TOTAL_ADDRESSED} |
| Comments deferred | {TOTAL_DEFERRED} |
| Final commit | {SHORT_HASH} |

### Per-Iteration Summary
| # | New comments | Addressed | Deferred | Pushed at |
|---|-------------|-----------|----------|-----------|
| 1 | 5           | 4         | 1        | 14:32 KST |
| 2 | 2           | 2         | 0        | 14:38 KST |
| 3 | 0           | —         | —        | (done)    |
```

### Safety Guards

1. **Max 5 iterations** — prevents infinite loops if the bot keeps finding new issues in fixes
2. **`--force-with-lease`** — never `--force`; fails safely if remote has diverged
3. **Squash base preserved** — all loop commits squash into one clean commit relative to pre-loop state
4. **Each iteration is atomic** — if interrupted, the working tree has all changes staged
5. **3-minute timeout per poll** — won't wait indefinitely for bot comments

### Combining with Other Flags

`--loop` can be combined with:
- `--resolve-bot`: auto-resolve addressed bot comments after each iteration
- `--pr=number`: target a specific PR
- `--resolve-bot=all`: resolve ALL bot comments (including open) in final iteration

`--loop` is **incompatible** with:
- `--post-response`: posting responses is a one-time action, not suitable for loops
- `--draft`: same reason as above

When incompatible flags are detected, warn and ignore them:
```
⚠️ --post-response is ignored in --loop mode. Run separately after the loop completes.
```
