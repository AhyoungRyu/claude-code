# Review PR Comments

Automates GitHub PR code review processing workflow.

## What it does

1. **Analyze PR comments** - Fetches and analyzes code review feedback
2. **Generate action plans** - Suggests how to address each comment
3. **Auto-respond to reviewers** - After fixing, posts summary back to PR with natural language
4. **Include GitHub links** - Adds clickable links to changed files and commits

## Installation

### Option 1: Standard Installation

```bash
# Clone repository
git clone https://github.com/AhyoungRyu/claude-code.git

# Install skills
cp -r claude-code/skills/review-pr-comments ~/.claude/skills/
cp -r claude-code/skills/humanizer ~/.claude/skills/
```

### Option 2: Using OMC (oh-my-claudecode)

```bash
# Clone repository
git clone https://github.com/AhyoungRyu/claude-code.git
cd claude-code

# Install using OMC
omc skill add skills/review-pr-comments
omc skill add skills/humanizer

# Or use symlinks for auto-updates
ln -s $(pwd)/skills/review-pr-comments ~/.claude/skills/review-pr-comments
ln -s $(pwd)/skills/humanizer ~/.claude/skills/humanizer
```

## Basic Usage

### Mode 1: Analyze Comments (Default)

Analyzes PR comments and suggests how to address them. Makes local changes but doesn't commit or push.

```bash
# Analyze comments on current branch
/review-pr-comments

# Analyze specific PR
/review-pr-comments 1753

# Analyze specific branch
/review-pr-comments feat/ai-agent-stats
```

**What it does:**
- Fetches PR comments from teammates (excludes your own)
- Filters only unresolved, recent comments
- Analyzes validity and actionability
- Suggests implementation approach
- Makes local code changes if appropriate

### Mode 2: Post Response to Reviewer

After addressing feedback, generates a summary and posts it back to the PR with GitHub links.

```bash
# Generate draft for review (recommended)
/review-pr-comments --post-response --draft

# Auto-post immediately
/review-pr-comments --post-response

# Post with specific reviewer
/review-pr-comments --post-response --reviewer bang9
```

## Response Format with Links

The generated response includes clickable GitHub links:

**Example:**
```markdown
@bang9

오늘 남겨주신 코멘트들 모두 반영했습니다!

### 반영 내용

**1. `_isSendable` 메서드 패턴 수정** ([#19](https://github.com/sendbird/chat-js/blob/feat/ai-agent-stats/src/stat/aiAgentStatCollector.ts#L19))
- DefaultStatCollector와 동일한 패턴으로 수정

**2. appendStat 문서화** ([#405](https://github.com/sendbird/chat-js/blob/feat/ai-agent-stats/src/module/aiAgentModule.ts#L405))
- 내부용 API라서 상세 설명 제거, `@experimental`만 남김

### 관련 커밋
- [24a163e](https://github.com/sendbird/chat-js/commit/24a163e27) - test: address PR review comments

---
<sub>Written by Claude Code</sub>
```

**Benefits:**
- ✅ Reviewers click links to see exact changes
- ✅ Direct navigation to modified lines
- ✅ Commit links for full change history
- ✅ Professional, verifiable responses

## Options Reference

### `--post-response`

Activates response posting mode. After addressing PR comments, generates a summary and posts it to the PR.

**Features:**
- Auto-detects reviewer from recent comments
- Auto-detects language (Korean/English) from reviewer's comments
- Uses humanizer for natural language
- **Automatically includes GitHub links to changes**
- Adds "Written by Claude Code" footer

**Example:**
```bash
/review-pr-comments --post-response
```

---

### `--draft`

Shows the generated comment draft before posting. Waits for your approval.

**Usage:** Must be combined with `--post-response`

**Example:**
```bash
/review-pr-comments --post-response --draft
```

**What happens:**
1. Generates the response summary with GitHub links
2. Shows you the draft
3. Asks for confirmation
4. Posts only after you approve

---

### `--commit`

Automatically commits and pushes changes before posting the response.

**Usage:** Must be combined with `--post-response`

**Example:**
```bash
/review-pr-comments --post-response --commit
```

**What it does:**
1. Stages all changes (`git add -A`)
2. Creates a commit with descriptive message
3. Pushes to remote
4. Gets commit hash for linking
5. Posts response with commit link included

---

### `--reviewer=USERNAME`

Specifies which reviewer's comments to check. If omitted, auto-detects from recent comments.

**Format:** `--reviewer=username` (no @ symbol)

**Examples:**
```bash
/review-pr-comments --post-response --reviewer=bang9
/review-pr-comments --post-response --reviewer=AhyoungRyu
```

---

### `--since=TIMESTAMP`

Checks only comments created after this timestamp. Default is today.

**Formats:**
- Date only: `YYYY-MM-DD`
- Date + time: `YYYY-MM-DDTHH:MM:SS`
- With timezone: `YYYY-MM-DDTHH:MM:SS+09:00` (recommended for Korea)
- UTC: `YYYY-MM-DDTHH:MM:SSZ`

**Examples:**
```bash
# Comments from Feb 11 onwards
/review-pr-comments --post-response --since=2026-02-11

# Comments since 2PM on Feb 11 (Korean time)
/review-pr-comments --post-response --since=2026-02-11T14:00:00+09:00
```

---

### `--pr=NUMBER`

Specifies the PR number. If omitted, auto-detects from current branch.

**Format:** `--pr=number` (just the number, no #)

**Examples:**
```bash
/review-pr-comments --post-response --pr=1753
/review-pr-comments --pr=1741
```

---

## Combined Examples

### Example 1: Full workflow with draft review and links
```bash
# Step 1: Analyze comments
/review-pr-comments

# Step 2: Make fixes manually or let Claude suggest changes
# (code changes happen here)

# Step 3: Generate draft response with GitHub links
/review-pr-comments --post-response --draft

# Step 4: Review draft (including links), approve, and post
```

### Example 2: Auto-commit and post with commit link
```bash
# Commits, pushes, and posts response with commit link
/review-pr-comments --post-response --commit --reviewer=bang9
```

The response will include:
- File/line links to each change
- Commit link (because of `--commit` flag)

### Example 3: Check specific PR and time range
```bash
/review-pr-comments --pr=1753 --post-response --since=2026-02-11T14:00:00+09:00
```

---

## Features

- ✅ Critical analysis of PR feedback (not blindly following all suggestions)
- ✅ Auto-detects Korean/English from reviewer's comments
- ✅ Natural language responses (uses humanizer skill)
- ✅ **Automatic GitHub links to changed files and commits**
- ✅ Draft mode for review before posting
- ✅ Auto-commit option with commit link
- ✅ Timestamp filtering with timezone support
- ✅ Multi-reviewer support
- ✅ Cross-PR support

## Requirements

- GitHub CLI (`gh`) installed and authenticated
- Git installed
- [humanizer skill](../humanizer/) installed

## Dependencies

- [humanizer](../humanizer/) - Required for natural language generation

## Troubleshooting

**"No comments found"**
- Check `--since` parameter - might be filtering out all comments
- Verify reviewer username is correct
- Make sure you're on the right branch/PR

**Auto-detection not working**
- Manually specify `--reviewer=username`
- Manually specify `--pr=number`
- Check that comments exist and are recent

**Links not working**
- Verify GitHub remote is set: `git remote -v`
- Check branch is pushed to GitHub
- Ensure file paths are correct

## Full Documentation

See [skill.md](./skill.md) for complete implementation details and advanced usage.
