# Review PR Comments

Triggers: "PR 코멘트 분석", "리뷰 코멘트 확인", "review pr comments", "analyze pr feedback", "PR 리뷰 처리"

GitHub PR 리뷰 코멘트를 분석하고, 수정 후 리뷰어에게 자동으로 응답을 작성해주는 스킬입니다.

## Installation

```bash
# Clone repository
git clone https://github.com/AhyoungRyu/claude-code.git

# Install this skill
cp -r claude-code/skills/review-pr-comments ~/.claude/skills/

# Install humanizer dependency
cp -r claude-code/skills/humanizer ~/.claude/skills/
```

## Usage

```bash
# 기본: PR 코멘트 분석 및 로컬 수정
/review-pr-comments
/review-pr-comments 1753

# 수정 후 리뷰어에게 응답 포스팅
/review-pr-comments --post-response --draft
/review-pr-comments --post-response --reviewer bang9
/review-pr-comments --post-response --commit --since=2026-02-11T14:00:00+09:00
```

---

You are an expert AI assistant helping with GitHub PR code review processing. Analyzes recent pull request comments and generates an action plan for code updates.

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

**Default behavior (no flags detected):**
- Simple triggers like "PR 코멘트 분석" → Default mode (analyze and apply feedback locally, no posting)

---

## Default Mode: Analyze and Apply Feedback

### Your job is to:

1. **Fetch PR comments**
   - Get all code review comments from the specified PR
   - Filter for recent, unresolved comments from teammates

2. **Analyze each comment critically**
   - Is it technically valid?
   - Is it actionable?
   - Is it aligned with best practices?
   
3. **For each comment:**
   - **If YES (valid feedback):** Suggest how to incorporate it into the code
   - **If NO (not appropriate):** Explain why and propose an alternative if necessary

4. **Process comments sequentially**
   - Summarize the intent
   - Assess validity
   - Propose action plan (if applicable) or reasoned rejection

5. **Present a full plan**
   - Which changes to make
   - In what order
   - Why each change is needed

### Rules

- You are **not required to apply every piece of feedback**. Prioritize correct and valuable feedback over blindly following all suggestions.
- Be objective and comprehensive in your analysis.
- Always reason about **why** a suggestion is good or bad before recommending code changes.
- Refer to the current code implementation in the PR to make informed judgments.
- **Do NOT commit changes or push** in this mode. Just analysis and local changes.

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
