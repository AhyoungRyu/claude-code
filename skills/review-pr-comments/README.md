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

## Quick Start

```bash
# Analyze comments
/review-pr-comments

# Post response after fixes
/review-pr-comments --post-response --draft
```

## Features

- ✅ Critical analysis of PR feedback (not blindly following all suggestions)
- ✅ Auto-detects Korean/English from reviewer's comments
- ✅ Natural language responses (uses humanizer skill)
- ✅ Draft mode for review before posting
- ✅ Auto-commit option
- ✅ Timestamp filtering

## Dependencies

- [humanizer](../humanizer/) - Required for natural language generation

## Full Documentation

See [skill.md](./skill.md) for complete usage and options.
