#!/usr/bin/env bash
# scan-skills.sh — produce a slash-command table from SKILL.md front matter,
# plus the OMC built-in agent catalog.
# Usage: scan-skills.sh [output-path]
set -euo pipefail

OUT="${1:-.omc/playbook/skills_snapshot.md}"
mkdir -p "$(dirname "$OUT")"

# Cache check: if snapshot exists and no SKILL.md is newer than it (and this
# script hasn't changed), skip regeneration.
if [ -f "$OUT" ]; then
  _script_changed=false
  [ "$0" -nt "$OUT" ] 2>/dev/null && _script_changed=true || true
  _newer_skill=$(find "$HOME/.claude/skills" ".claude/skills" -name "SKILL.md" \
    -newer "$OUT" 2>/dev/null | head -1 || true)
  if ! $_script_changed && [ -z "$_newer_skill" ]; then
    echo "Skills snapshot up-to-date ($(basename "$OUT")), skipping scan." >&2
    exit 0
  fi
fi

{
  echo "# Skills Snapshot"
  echo "Generated: $(date -Iseconds)"
  echo ""
  echo "## File-based skills (user + project level)"
  echo ""
  echo "| Slash Command | Description |"
  echo "|---------------|-------------|"
} > "$OUT"

extract_desc() {
  local FILE="$1"
  # Try inline value first (description: some text)
  local desc
  desc=$(grep -m1 '^description:' "$FILE" 2>/dev/null \
    | sed 's/^description:[[:space:]]*//' \
    | sed 's/^[>|][[:space:]]*//' \
    | tr -d '"' \
    | tr -d "'" \
    | cut -c1-80)
  # If empty or was a block scalar marker, grab next non-empty indented line
  if [ -z "$desc" ]; then
    desc=$(awk '
      /^description:[[:space:]]*(>|\||-)?[[:space:]]*$/ { found=1; next }
      found && /^[[:space:]]+[^[:space:]]/ {
        gsub(/^[[:space:]]+/, "")
        # strip leading quotes
        gsub(/^["'"'"']/, "")
        print substr($0, 1, 80)
        exit
      }
      found && /^[^[:space:]]/ { exit }
    ' "$FILE" 2>/dev/null)
  fi
  echo "${desc:-(no description)}"
}

emit_skill() {
  local FILE="$1"

  # Extract `name:` from YAML front matter
  local name
  name=$(grep -m1 '^name:' "$FILE" 2>/dev/null \
    | sed 's/^name:[[:space:]]*//' \
    | tr -d '"'"'" \
    | tr -d '[:space:]')
  [ -z "$name" ] && name="$(basename "$(dirname "$FILE")")"

  local desc
  desc=$(extract_desc "$FILE")

  echo "| \`/${name}\` | ${desc} |" >> "$OUT"
}

for DIR in "$HOME/.claude/skills" ".claude/skills"; do
  [ -d "$DIR" ] || continue
  while IFS= read -r f; do
    emit_skill "$f"
  done < <(find "$DIR" -name "SKILL.md" ! -path "*/node_modules/*" | sort)
done

# Custom agents from ~/.claude/agents/
AGENTS_DIR="$HOME/.claude/agents"
if [ -d "$AGENTS_DIR" ]; then
  {
    echo ""
    echo "## Custom agents (~/.claude/agents/)"
    echo ""
    echo "| Agent | Description |"
    echo "|-------|-------------|"
  } >> "$OUT"

  while IFS= read -r f; do
    local_name=$(grep -m1 '^name:' "$f" 2>/dev/null \
      | sed 's/^name:[[:space:]]*//' \
      | tr -d '"'"'" \
      | tr -d '[:space:]')
    [ -z "$local_name" ] && local_name="$(basename "$f" .md)"

    local_desc=$(extract_desc "$f")

    echo "| \`${local_name}\` | ${local_desc} |" >> "$OUT"
  done < <(find "$AGENTS_DIR" -maxdepth 1 -name "*.md" | sort)
fi

# OMC built-in agent catalog (not file-based; defined in CLAUDE.md plugin)
{
  echo ""
  echo "## OMC built-in agents (invoke via Agent tool with \`oh-my-claudecode:\` subagent_type)"
  echo ""
  echo "### Build / Analysis Lane"
  echo ""
  echo "| Agent | Role |"
  echo "|-------|------|"
  echo "| \`oh-my-claudecode:explore\` | Codebase discovery, symbol/file mapping (haiku) |"
  echo "| \`oh-my-claudecode:analyst\` | Requirements clarity, acceptance criteria (opus) |"
  echo "| \`oh-my-claudecode:planner\` | Task sequencing, execution plans, risk flags (opus) |"
  echo "| \`oh-my-claudecode:architect\` | System design, boundaries, interfaces, long-horizon tradeoffs (opus) |"
  echo "| \`oh-my-claudecode:debugger\` | Root-cause analysis, regression isolation, failure diagnosis (sonnet) |"
  echo "| \`oh-my-claudecode:executor\` | Code implementation, refactoring, feature work (sonnet) |"
  echo "| \`oh-my-claudecode:deep-executor\` | Complex autonomous goal-oriented tasks (opus) |"
  echo "| \`oh-my-claudecode:verifier\` | Completion evidence, claim validation, test adequacy (sonnet) |"
  echo ""
  echo "### Review Lane"
  echo ""
  echo "| Agent | Role |"
  echo "|-------|------|"
  echo "| \`oh-my-claudecode:style-reviewer\` | Formatting, naming, idioms, lint conventions (haiku) |"
  echo "| \`oh-my-claudecode:quality-reviewer\` | Logic defects, maintainability, anti-patterns (sonnet) |"
  echo "| \`oh-my-claudecode:api-reviewer\` | API contracts, versioning, backward compatibility (sonnet) |"
  echo "| \`oh-my-claudecode:security-reviewer\` | Vulnerabilities, trust boundaries, authn/authz (sonnet) |"
  echo "| \`oh-my-claudecode:performance-reviewer\` | Hotspots, complexity, memory/latency optimization (sonnet) |"
  echo "| \`oh-my-claudecode:code-reviewer\` | Comprehensive review across concerns (opus) |"
  echo ""
  echo "### Domain Specialists"
  echo ""
  echo "| Agent | Role |"
  echo "|-------|------|"
  echo "| \`oh-my-claudecode:dependency-expert\` | External SDK/API/package evaluation (sonnet) |"
  echo "| \`oh-my-claudecode:test-engineer\` | Test strategy, coverage, flaky-test hardening (sonnet) |"
  echo "| \`oh-my-claudecode:quality-strategist\` | Quality strategy, release readiness, risk assessment (sonnet) |"
  echo "| \`oh-my-claudecode:build-fixer\` | Build/toolchain/type failures (sonnet) |"
  echo "| \`oh-my-claudecode:designer\` | UX/UI architecture, interaction design (sonnet) |"
  echo "| \`oh-my-claudecode:writer\` | Docs, migration notes, user guidance (haiku) |"
  echo "| \`oh-my-claudecode:qa-tester\` | Interactive CLI/service runtime validation (sonnet) |"
  echo "| \`oh-my-claudecode:scientist\` | Data/statistical analysis (sonnet) |"
  echo "| \`oh-my-claudecode:git-master\` | Commit strategy, history hygiene (sonnet) |"
  echo ""
  echo "### Product Lane"
  echo ""
  echo "| Agent | Role |"
  echo "|-------|------|"
  echo "| \`oh-my-claudecode:product-manager\` | Problem framing, personas/JTBD, PRDs (sonnet) |"
  echo "| \`oh-my-claudecode:ux-researcher\` | Heuristic audits, usability, accessibility (sonnet) |"
  echo "| \`oh-my-claudecode:information-architect\` | Taxonomy, navigation, findability (sonnet) |"
  echo "| \`oh-my-claudecode:product-analyst\` | Product metrics, funnel analysis, experiments (sonnet) |"
  echo ""
  echo "### Coordination"
  echo ""
  echo "| Agent | Role |"
  echo "|-------|------|"
  echo "| \`oh-my-claudecode:critic\` | Plan/design critical challenge (opus) |"
  echo "| \`oh-my-claudecode:vision\` | Image/screenshot/diagram analysis (sonnet) |"
} >> "$OUT"

# OMC skills / slash commands
{
  echo ""
  echo "## OMC skills (/oh-my-claudecode:<name>)"
  echo ""
  echo "### Workflow Skills"
  echo ""
  echo "| Slash Command | Description |"
  echo "|---------------|-------------|"
  echo "| \`/oh-my-claudecode:autopilot\` | Full autonomous execution from idea to working code |"
  echo "| \`/oh-my-claudecode:ralph\` | Self-referential loop until task completion with verifier verification |"
  echo "| \`/oh-my-claudecode:ultrawork\` | Maximum parallelism with parallel agent orchestration |"
  echo "| \`/oh-my-claudecode:ultrapilot\` | Parallel autopilot with file ownership partitioning |"
  echo "| \`/oh-my-claudecode:ecomode\` | Token-efficient execution using haiku and sonnet |"
  echo "| \`/oh-my-claudecode:team\` | N coordinated agents on shared task list using Claude Code native teams |"
  echo "| \`/oh-my-claudecode:pipeline\` | Sequential agent chaining with data passing |"
  echo "| \`/oh-my-claudecode:ultraqa\` | QA cycling — test, verify, fix, repeat until goal met |"
  echo "| \`/oh-my-claudecode:plan\` | Strategic planning with optional interview workflow |"
  echo "| \`/oh-my-claudecode:ralph-init\` | Initialize a PRD for structured ralph-loop execution |"
  echo "| \`/oh-my-claudecode:deep-interview\` | Socratic deep interview with mathematical ambiguity gating |"
  echo "| \`/oh-my-claudecode:deepinit\` | Deep codebase initialization with hierarchical AGENTS.md documentation |"
  echo ""
  echo "### Agent Shortcuts"
  echo ""
  echo "| Slash Command | Delegates To | Trigger |"
  echo "|---------------|--------------|---------|"
  echo "| \`/oh-my-claudecode:analyze\` | debugger | analyze, debug, investigate |"
  echo "| \`/oh-my-claudecode:tdd\` | test-engineer | tdd, test first, red green |"
  echo "| \`/oh-my-claudecode:build-fix\` | build-fixer | fix build, type errors |"
  echo "| \`/oh-my-claudecode:code-review\` | code-reviewer | review code |"
  echo "| \`/oh-my-claudecode:security-review\` | security-reviewer | security review |"
  echo "| \`/oh-my-claudecode:git-master\` | git-master | git/commit work |"
  echo ""
  echo "### Utilities"
  echo ""
  echo "| Slash Command | Description |"
  echo "|---------------|-------------|"
  echo "| \`/oh-my-claudecode:cancel\` | Cancel any active OMC mode |"
  echo "| \`/oh-my-claudecode:note\` | Save notes to notepad for compaction resilience |"
  echo "| \`/oh-my-claudecode:omc-setup\` | Setup and configure oh-my-claudecode |"
  echo "| \`/oh-my-claudecode:mcp-setup\` | Configure popular MCP servers |"
  echo "| \`/oh-my-claudecode:hud\` | Configure HUD display options |"
  echo "| \`/oh-my-claudecode:omc-doctor\` | Diagnose and fix oh-my-claudecode installation issues |"
  echo "| \`/oh-my-claudecode:omc-help\` | Guide on using oh-my-claudecode |"
  echo "| \`/oh-my-claudecode:trace\` | Show agent flow trace timeline and summary |"
  echo "| \`/oh-my-claudecode:learner\` | Extract a learned skill from the current conversation |"
  echo "| \`/oh-my-claudecode:skill\` | Manage local skills — list, add, remove, search, edit |"
  echo "| \`/oh-my-claudecode:writer-memory\` | Agentic memory system for writers |"
  echo "| \`/oh-my-claudecode:project-session-manager\` | Manage isolated dev environments with git worktrees and tmux sessions |"
  echo "| \`/oh-my-claudecode:configure-notifications\` | Configure notification integrations (Telegram, Discord, Slack) |"
  echo "| \`/oh-my-claudecode:sciomc\` | Orchestrate parallel scientist agents for comprehensive analysis |"
  echo "| \`/oh-my-claudecode:ccg\` | Claude-Codex-Gemini tri-model orchestration |"
  echo "| \`/oh-my-claudecode:ask-codex\` | Ask Codex via local CLI and capture a reusable artifact |"
  echo "| \`/oh-my-claudecode:ask-gemini\` | Ask Gemini via local CLI and capture a reusable artifact |"
  echo "| \`/oh-my-claudecode:omc-teams\` | Spawn claude, codex, or gemini CLI workers in tmux panes |"
  echo "| \`/oh-my-claudecode:release\` | Automated release workflow for oh-my-claudecode |"
} >> "$OUT"

{
  echo ""
  echo "## Usage rules"
  echo "- Use the **exact** slash command or agent name from the tables above."
  echo "- If no skill/agent fits a step, write \"direct implementation\" with a one-line reason."
  echo "- Never invent names not present in this snapshot."
} >> "$OUT"
