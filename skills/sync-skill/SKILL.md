---
name: sync-skill
description: >
  Sync skill files from this repo to main branch and local ~/.claude/ installation.
  Handles commit, push to main (worktree-aware), and copies to ~/.claude/skills/ and ~/.claude/commands/.
argument-hint: "[skill-name | --all] [--no-readme] [--dry-run] [--no-push]"
allowed-tools: Bash, Read, Glob, Grep, Agent
---

# Sync Skill: $ARGUMENTS

You are a skill deployment assistant. Your job is to commit changed skill files, push to the `main` branch, and sync them to the local `~/.claude/` installation so they are immediately usable.

## Step 0 — Parse Arguments

```
SKILL_NAME=""           # specific skill to sync (e.g., "review-pr")
SYNC_ALL=false          # --all: sync all changed skills
UPDATE_README=true      # default: update/generate README.md (use --no-readme to skip)
DRY_RUN=false           # --dry-run: show what would happen, don't execute
NO_PUSH=false           # --no-push: commit and sync locally but skip push
```

**Parsing rules:**
| Pattern | Action |
|---|---|
| `--all` | `SYNC_ALL=true` |
| `--no-readme` | `UPDATE_README=false` |
| `--dry-run` | `DRY_RUN=true` |
| `--no-push` | `NO_PUSH=true` |
| Any other word | `SKILL_NAME=<word>` |
| Empty | Auto-detect changed skills from `git diff` |

## Step 1 — Detect Changed Skills

### If `SKILL_NAME` is provided:
- Verify `skills/$SKILL_NAME/` exists in the repo
- Check if it has uncommitted changes via `git status skills/$SKILL_NAME/`

### If `SYNC_ALL` or empty arguments:
- Detect all changed skill directories:
```bash
# Find all skill dirs with uncommitted changes (staged + unstaged + untracked)
CHANGED_SKILLS=$(git status --porcelain skills/ 2>/dev/null | sed 's|^...skills/||' | cut -d'/' -f1 | sort -u)
```
- If no changes detected, check if local `~/.claude/skills/` is out of sync with repo (compare file hashes)

### Normalize filenames:
For each skill directory, identify the main skill file:
```bash
# Detect SKILL.md (preferred) or skill.md (legacy)
if [ -f "skills/$SKILL/SKILL.md" ]; then
  SKILL_FILE="SKILL.md"
elif [ -f "skills/$SKILL/skill.md" ]; then
  SKILL_FILE="skill.md"
fi
```

Report what will be synced:
```
Skills to sync:
  - review-pr (SKILL.md, modified)
  - review-pr-comments (skill.md, modified)
  - sync-skill (SKILL.md, new)
```

If `DRY_RUN=true`, stop here and show the plan without executing.

## Step 2 — README Update (conditional)

**By default (unless `--no-readme` is passed)**, for each skill being synced:

1. Read the current skill file content
2. Check if `README.md` exists in the skill directory
3. If missing or outdated, generate/update it with:
   - Skill name and description (from frontmatter)
   - Installation instructions
   - Usage examples (extracted from the skill content)
   - Argument reference table

Use a concise, practical format. Do not over-document.

## Step 3 — Commit Changes

Stage and commit all changed skill files:

```bash
# Stage specific skill files
for SKILL in $CHANGED_SKILLS; do
  git add "skills/$SKILL/"
done

# Commit with descriptive message
git commit -m "chore(skills): sync $SKILL_LIST

Updated: [list of changed skills]"
```

**Commit message conventions:**
- New skill: `feat(skills): add sync-skill`
- Update existing: `fix(skills): update review-pr Track B` or `feat(skills): add exclusion list to review-pr`
- Multiple skills: `chore(skills): sync review-pr, review-pr-comments`

If there are no changes to commit, skip to Step 4 (local sync may still be needed).

## Step 4 — Push to Main

**Worktree detection**: The `main` branch may be checked out in another worktree.

```bash
CURRENT_BRANCH=$(git branch --show-current)

# Check if main is in another worktree
if git worktree list | grep -q '\[main\]' && [ "$CURRENT_BRANCH" != "main" ]; then
  # Push current branch to remote main directly
  git push origin "$CURRENT_BRANCH:main"
else
  # Normal flow: checkout main, merge, push
  git checkout main
  git merge "$CURRENT_BRANCH" --no-edit
  git push origin main
  git checkout "$CURRENT_BRANCH"
fi
```

**Handle non-fast-forward**: If push is rejected:
```bash
git fetch origin main
git rebase origin/main
# Resolve conflicts if any, then retry push
git push origin "$CURRENT_BRANCH:main"
```

If `NO_PUSH=true`, skip this step entirely.

## Step 5 — Sync to Local Installation

For each synced skill, copy to both `~/.claude/skills/` and `~/.claude/commands/`:

```bash
SKILL_DIR="$HOME/.claude/skills/$SKILL"
COMMAND_FILE="$HOME/.claude/commands/$SKILL.md"

# 1. Sync skill directory
mkdir -p "$SKILL_DIR"
cp "skills/$SKILL/$SKILL_FILE" "$SKILL_DIR/"
# Also copy README if it exists
[ -f "skills/$SKILL/README.md" ] && cp "skills/$SKILL/README.md" "$SKILL_DIR/"
# Copy any additional directories (scripts/, templates/)
for subdir in scripts templates; do
  [ -d "skills/$SKILL/$subdir" ] && cp -r "skills/$SKILL/$subdir" "$SKILL_DIR/"
done

# 2. Sync command file (flattened copy)
cp "skills/$SKILL/$SKILL_FILE" "$COMMAND_FILE"
```

**Note on macOS case-insensitivity**: `cp skills/X/SKILL.md ~/.claude/skills/X/skill.md` will overwrite `SKILL.md` on macOS (HFS+ is case-insensitive). This is expected behavior.

## Step 6 — Verify

After syncing, verify all files are in place:

```bash
for SKILL in $CHANGED_SKILLS; do
  echo "=== $SKILL ==="
  # Check skill dir exists
  ls -la "$HOME/.claude/skills/$SKILL/"
  # Check command file exists
  ls -la "$HOME/.claude/commands/$SKILL.md"
  # Verify content matches (compare key patterns)
  echo "Repo hash:  $(md5 -q skills/$SKILL/$SKILL_FILE)"
  echo "Skill hash: $(md5 -q $HOME/.claude/skills/$SKILL/$SKILL_FILE)"
  echo "Cmd hash:   $(md5 -q $HOME/.claude/commands/$SKILL.md)"
done
```

Report:
```
✓ review-pr: repo ↔ skill ↔ command (all match)
✓ review-pr-comments: repo ↔ skill ↔ command (all match)
✗ playbook: command file missing (no ~/.claude/commands/playbook.md)
```

## Rules

- **Never force-push** to main. If rebase has conflicts, resolve them or ask the user.
- **Always verify** after sync — don't just report success without checking.
- **Preserve existing files** in `~/.claude/skills/` that aren't in this repo (they may come from other sources).
- **Don't delete** skills from local that don't exist in this repo.
- If the skill has a `scripts/` or `templates/` subdirectory, sync those too.
- If the command file in `~/.claude/commands/` doesn't exist yet, create it. If it already exists, overwrite it.
