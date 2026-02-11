# Review PR Comments

Automates GitHub PR code review processing workflow.

## What it does

1. **Analyze PR comments** - Fetches and analyzes code review feedback
2. **Generate action plans** - Suggests how to address each comment
3. **Auto-respond to reviewers** - After fixing, posts summary back to PR with natural language

## Installation

```bash
git clone https://github.com/AhyoungRyu/claude-code.git
cp -r claude-code/skills/review-pr-comments ~/.claude/skills/
cp -r claude-code/skills/humanizer ~/.claude/skills/
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

After addressing feedback, generates a summary and posts it back to the PR.

```bash
# Generate draft for review (recommended)
/review-pr-comments --post-response --draft

# Auto-post immediately
/review-pr-comments --post-response

# Post with specific reviewer
/review-pr-comments --post-response --reviewer bang9
```

## Options Reference

### `--post-response`

Activates response posting mode. After addressing PR comments, generates a summary and posts it to the PR.

**Features:**
- Auto-detects reviewer from recent comments
- Auto-detects language (Korean/English) from reviewer's comments
- Uses humanizer for natural language
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
1. Generates the response summary
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
4. Then posts the response to PR

**When to use:** When you want to ensure the response is posted only after changes are pushed to GitHub.

---

### `--reviewer=USERNAME`

Specifies which reviewer's comments to check. If omitted, auto-detects from recent comments.

**Format:** `--reviewer=username` (no @ symbol)

**Examples:**
```bash
/review-pr-comments --post-response --reviewer=bang9
/review-pr-comments --post-response --reviewer=AhyoungRyu
```

**When to use:**
- Multiple reviewers have commented
- You want to respond to a specific reviewer
- Auto-detection is picking the wrong reviewer

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

# Comments since 5AM UTC
/review-pr-comments --post-response --since=2026-02-11T05:00:00Z

# Comments from yesterday
/review-pr-comments --post-response --since=2026-02-10
```

**When to use:**
- You've already addressed some older comments
- You only want to check comments from a specific date/time
- You're doing multiple review cycles

**Timezone tips:**
- Korea: Use `+09:00` (e.g., `2026-02-11T14:00:00+09:00`)
- UTC: Use `Z` suffix (e.g., `2026-02-11T05:00:00Z`)
- If no timezone specified, uses local system time

---

### `--pr=NUMBER`

Specifies the PR number. If omitted, auto-detects from current branch.

**Format:** `--pr=number` (just the number, no #)

**Examples:**
```bash
/review-pr-comments --post-response --pr=1753
/review-pr-comments --pr=1741
```

**When to use:**
- Current branch doesn't have a PR yet
- You want to check a different PR
- Auto-detection isn't working

---

## Combined Examples

### Example 1: Full workflow with draft review
```bash
# Step 1: Analyze comments
/review-pr-comments

# Step 2: Make fixes manually or let Claude suggest changes
# (code changes happen here)

# Step 3: Generate draft response
/review-pr-comments --post-response --draft

# Step 4: Review draft, approve, and post
```

### Example 2: Auto-commit and post for specific reviewer
```bash
# Address bang9's comments from Feb 11 onwards
# Automatically commit, push, and post response
/review-pr-comments --post-response --commit --reviewer=bang9 --since=2026-02-11
```

### Example 3: Check specific PR and time range
```bash
# Check PR #1753 for comments since 2PM today
/review-pr-comments --pr=1753 --post-response --since=2026-02-11T14:00:00+09:00
```

### Example 4: Draft mode with all options
```bash
# Most cautious approach - review everything before posting
/review-pr-comments --post-response --draft --reviewer=bang9 --since=2026-02-11T09:00:00+09:00
```

---

## Response Format

The generated response follows this format:

**Korean (auto-detected):**
```
@reviewer

오늘 남겨주신 코멘트들 모두 반영했습니다!

### 반영 내용

**1. Description** (line 42)
- Change description ending with: 수정 / 반영 / 제거

**2. Description** (line 105)
- Another change description

---
<sub>Written by Claude Code</sub>
```

**English (auto-detected):**
```
@reviewer

All comments have been addressed!

### Changes Made

**1. Description** (line 42)
- Updated implementation to match pattern

**2. Description** (line 105)
- Fixed type definitions

---
<sub>Written by Claude Code</sub>
```

---

## Features

- ✅ Critical analysis of PR feedback (not blindly following all suggestions)
- ✅ Auto-detects Korean/English from reviewer's comments
- ✅ Natural language responses (uses humanizer skill)
- ✅ Draft mode for review before posting
- ✅ Auto-commit option
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

**Language detection wrong**
- The skill auto-detects based on 80%+ threshold
- If reviewer mixes languages heavily, it defaults to English

## Full Documentation

See [skill.md](./skill.md) for complete implementation details and advanced usage.
