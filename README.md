# Claude Code Skills

Personal collection of Claude Code skills for productivity automation.

## Skills

| Skill | Description |
|-------|-------------|
| [playbook](./skills/playbook/) | Turn any goal into a structured, executable runbook — classifies task type, generates a Codex-authored plan, executes autonomously |
| [review-pr](./skills/review-pr/) | Comprehensive PR review — initializes playbook artifacts, routes to parallel specialist agents (code, security, quality, performance, API), aggregates severity-rated report, applies local fixes |
| [review-pr-comments](./skills/review-pr-comments/) | Process GitHub PR review comments — generates action plans, addresses feedback, posts responses back to reviewers |

## Install

```bash
git clone https://github.com/AhyoungRyu/claude-code.git

# Install a specific skill
cp -r claude-code/skills/<skill-name> ~/.claude/skills/

# Install all skills
cp -r claude-code/skills/* ~/.claude/skills/
```

Then invoke in Claude Code:
```
/playbook <your goal>
/review-pr [PR number | branch]
/review-pr-comments
```
