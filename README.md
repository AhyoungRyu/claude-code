# Claude Code Skills & Commands

Personal collection of Claude Code skills and commands for productivity automation.

## Skills

### [review-pr-comments](./skills/review-pr-comments/)

Automates GitHub PR code review processing workflow.

**Features:**
- Analyzes PR review comments and generates action plans
- Automatically generates response summaries after addressing feedback
- Posts responses back to reviewers with natural language
- Auto-detects Korean/English based on reviewer's language

**Quick Start:**
```bash
# Install
git clone https://github.com/AhyoungRyu/claude-code.git
cp -r claude-code/skills/review-pr-comments ~/.claude/skills/

# Also install humanizer dependency
cp -r claude-code/skills/humanizer ~/.claude/skills/

# Use
/review-pr-comments
/review-pr-comments --post-response --draft
```

## Installation

### Clone the repository
```bash
git clone https://github.com/AhyoungRyu/claude-code.git
cd claude-code
```

### Install specific skill
```bash
# Copy skill to Claude Code skills directory
cp -r skills/[skill-name] ~/.claude/skills/

# Or create symlink for auto-updates
ln -s $(pwd)/skills/[skill-name] ~/.claude/skills/[skill-name]
```

### Install all skills
```bash
# Copy all skills
cp -r skills/* ~/.claude/skills/

# Or symlink all
for skill in skills/*; do
  ln -s $(pwd)/$skill ~/.claude/skills/$(basename $skill)
done
```

## Requirements

- [Claude Code](https://www.anthropic.com/claude/code)
- GitHub CLI (`gh`) for PR operations
- Git for version control operations

## Usage

After installation, skills are available as commands:

```bash
/review-pr-comments
/review-pr-comments --post-response --draft
```

For detailed usage, ask Claude Code:
```
review-pr-comments 사용법 알려줘
```

## Contributing

Feel free to open issues or submit PRs if you have suggestions or improvements!

## License

MIT
