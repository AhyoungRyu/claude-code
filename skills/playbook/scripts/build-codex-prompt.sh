#!/usr/bin/env bash
# build-codex-prompt.sh — fill all template placeholders and write codex_prompt.txt
# Usage: build-codex-prompt.sh '<user message>' '<task_type>' '<phase_template>' '<constraints>'
# Steering context is loaded automatically from $OUT_DIR/steering.md if it exists.
set -euo pipefail

USER_MESSAGE="${1:-}"
TASK_TYPE="${2:-}"
PHASE_TEMPLATE="${3:-}"
CONSTRAINTS="${4:-}"

if [ -z "$USER_MESSAGE" ]; then
  echo "Usage: build-codex-prompt.sh '<user message>' '<task_type>' '<phase_template>' '<constraints>'" >&2
  exit 1
fi

if [ -d ".omc" ]; then
  OUT_DIR=".omc/playbook"
else
  OUT_DIR=".context/playbook"
fi
mkdir -p "$OUT_DIR"

TEMPLATE="${HOME}/.claude/skills/playbook/templates/codex_runbook_prompt.md"
SKILLS_SNAPSHOT="${OUT_DIR}/skills_snapshot.md"
OUT_PROMPT="${OUT_DIR}/codex_prompt.txt"
STEERING="${OUT_DIR}/steering.md"

if [ ! -f "$TEMPLATE" ]; then
  echo "Template not found: $TEMPLATE" >&2
  exit 1
fi

if [ ! -f "$SKILLS_SNAPSHOT" ]; then
  echo "Skills snapshot not found: $SKILLS_SNAPSHOT" >&2
  echo "Run: bash ~/.claude/skills/playbook/scripts/scan-skills.sh $SKILLS_SNAPSHOT" >&2
  exit 1
fi

# Load steering context if it exists
STEERING_CONTENT="(none)"
if [ -f "$STEERING" ]; then
  STEERING_CONTENT="$(cat "$STEERING")"
fi

# Prefer the "Top list" section only to keep the prompt concise;
# fall back to the full snapshot if no Top list section exists.
SKILLS_BLOCK="$(awk '
  BEGIN{in=0}
  /^## Top list/{in=1; next}
  /^## /{if(in==1) exit}
  {if(in==1) print}
' "$SKILLS_SNAPSHOT")"

if [ -z "$SKILLS_BLOCK" ]; then
  SKILLS_BLOCK="$(head -n 4000 "$SKILLS_SNAPSHOT")"
fi

# Write all values to temp files for safe multi-line substitution
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

printf '%s' "$USER_MESSAGE"     > "$TMP_DIR/user_message.txt"
printf '%s' "$TASK_TYPE"        > "$TMP_DIR/task_type.txt"
printf '%s' "$PHASE_TEMPLATE"   > "$TMP_DIR/phase_template.txt"
printf '%s' "$CONSTRAINTS"      > "$TMP_DIR/constraints.txt"
printf '%s' "$SKILLS_BLOCK"     > "$TMP_DIR/skills.txt"
printf '%s' "$STEERING_CONTENT" > "$TMP_DIR/steering.txt"

python3 - "$TEMPLATE" "$OUT_PROMPT" "$TMP_DIR" <<'PYEOF'
import sys

template_path, out_path, tmp_dir = sys.argv[1], sys.argv[2], sys.argv[3]

def read(name):
    with open(f"{tmp_dir}/{name}") as f:
        return f.read()

with open(template_path) as f:
    content = f.read()

replacements = {
    "{{USER_MESSAGE}}":                read("user_message.txt"),
    "{{TASK_TYPE}}":                   read("task_type.txt"),
    "{{PHASE_TEMPLATE}}":              read("phase_template.txt"),
    "{{CONSTRAINTS}}":                 read("constraints.txt"),
    "{{SKILLS_SNAPSHOT_OR_TOP_LIST}}": read("skills.txt"),
    "{{STEERING_CONTEXT}}":            read("steering.txt"),
}

for placeholder, value in replacements.items():
    content = content.replace(placeholder, value)

with open(out_path, "w") as f:
    f.write(content)

print(out_path)
PYEOF
