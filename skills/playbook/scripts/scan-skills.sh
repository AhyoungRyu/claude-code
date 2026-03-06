#!/usr/bin/env bash
# scan-skills.sh — produce a slash-command table from SKILL.md front matter,
# plus the OMC built-in agent catalog.
# Usage: scan-skills.sh [output-path]
set -euo pipefail

OUT="${1:-.omc/spec-forge/skills_snapshot.md}"
mkdir -p "$(dirname "$OUT")"

{
  echo "# Skills Snapshot"
  echo "Generated: $(date -Iseconds)"
  echo ""
  echo "## File-based skills (user + project level)"
  echo ""
  echo "| Slash Command | Description |"
  echo "|---------------|-------------|"
} > "$OUT"

emit_skill() {
  local FILE="$1"

  # Extract `name:` from YAML front matter
  local name
  name=$(grep -m1 '^name:' "$FILE" 2>/dev/null \
    | sed 's/^name:[[:space:]]*//' \
    | tr -d '"'"'" \
    | tr -d '[:space:]')
  [ -z "$name" ] && name="$(basename "$(dirname "$FILE")")"

  # Extract description: first inline value; strip block-scalar `>`, quotes
  local desc
  desc=$(grep -m1 '^description:' "$FILE" 2>/dev/null \
    | sed 's/^description:[[:space:]]*//' \
    | sed 's/^>[[:space:]]*//' \
    | tr -d '"' \
    | cut -c1-80)
  [ -z "$desc" ] && desc="(no description)"

  echo "| \`/${name}\` | ${desc} |" >> "$OUT"
}

for DIR in "$HOME/.claude/skills" ".claude/skills"; do
  [ -d "$DIR" ] || continue
  while IFS= read -r f; do
    emit_skill "$f"
  done < <(find "$DIR" -name "SKILL.md" ! -path "*/node_modules/*" | sort)
done

# OMC built-in agent catalog (not file-based; defined in CLAUDE.md plugin)
{
  echo ""
  echo "## OMC built-in agents (invoke via Agent tool with \`oh-my-claudecode:\` subagent_type)"
  echo ""
  echo "| Agent / Slash Command | Role |"
  echo "|----------------------|------|"
  echo "| \`oh-my-claudecode:explore\` | Codebase discovery, symbol/file mapping (haiku) |"
  echo "| \`oh-my-claudecode:analyst\` | Requirements clarity, acceptance criteria (opus) |"
  echo "| \`oh-my-claudecode:planner\` | Task sequencing, execution plans, risk flags (opus) |"
  echo "| \`oh-my-claudecode:architect\` | System design, long-horizon tradeoffs (opus) |"
  echo "| \`oh-my-claudecode:debugger\` | Root-cause analysis, regression isolation (sonnet) |"
  echo "| \`oh-my-claudecode:executor\` | Code implementation, refactoring, feature work (sonnet) |"
  echo "| \`oh-my-claudecode:deep-executor\` | Complex autonomous goal-oriented tasks (opus) |"
  echo "| \`oh-my-claudecode:verifier\` | Completion evidence, claim validation (sonnet) |"
  echo "| \`oh-my-claudecode:quality-reviewer\` | Logic defects, maintainability, anti-patterns (sonnet) |"
  echo "| \`oh-my-claudecode:security-reviewer\` | Vulnerabilities, trust boundaries (sonnet) |"
  echo "| \`oh-my-claudecode:test-engineer\` | Test strategy, coverage, flaky-test hardening (sonnet) |"
  echo "| \`oh-my-claudecode:build-fixer\` | Build/toolchain/type failures (sonnet) |"
  echo "| \`oh-my-claudecode:code-reviewer\` | Comprehensive review across concerns (opus) |"
  echo "| \`oh-my-claudecode:designer\` | UX/UI architecture, interaction design (sonnet) |"
  echo "| \`oh-my-claudecode:writer\` | Docs, migration notes, user guidance (haiku) |"
  echo "| \`oh-my-claudecode:git-master\` | Commit strategy, history hygiene (sonnet) |"
} >> "$OUT"

{
  echo ""
  echo "## Usage rules"
  echo "- Use the **exact** slash command or agent name from the tables above."
  echo "- If no skill/agent fits a step, write \"direct implementation\" with a one-line reason."
  echo "- Never invent names not present in this snapshot."
} >> "$OUT"
