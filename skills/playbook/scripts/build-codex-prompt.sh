#!/usr/bin/env bash
set -euo pipefail

USER_MESSAGE="${1:-}"
if [ -z "$USER_MESSAGE" ]; then
  echo "Usage: build-codex-prompt.sh '<user message>'" >&2
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

if [ ! -f "$TEMPLATE" ]; then
  echo "Template not found: $TEMPLATE" >&2
  exit 1
fi

if [ ! -f "$SKILLS_SNAPSHOT" ]; then
  echo "Skills snapshot not found: $SKILLS_SNAPSHOT" >&2
  echo "Run: scan-skills.sh $SKILLS_SNAPSHOT" >&2
  exit 1
fi

# Prefer the "Top list" section only to keep the prompt concise;
# fall back to the full snapshot (up to 4000 lines) if no Top list exists.
SKILLS_BLOCK="$(awk '
  BEGIN{in=0}
  /^## Top list/{in=1; next}
  /^## /{if(in==1) exit}
  {if(in==1) print}
' "$SKILLS_SNAPSHOT")"

if [ -z "$SKILLS_BLOCK" ]; then
  SKILLS_BLOCK="$(head -n 4000 "$SKILLS_SNAPSHOT")"
fi

# Substitute template placeholders.
# awk is used instead of sed to safely handle newlines and special characters.
awk -v um="$USER_MESSAGE" -v skills="$SKILLS_BLOCK" '
  {
    gsub(/\{\{USER_MESSAGE\}\}/, um)
    gsub(/\{\{SKILLS_SNAPSHOT_OR_TOP_LIST\}\}/, skills)
    print
  }
' "$TEMPLATE" > "$OUT_PROMPT"

echo "$OUT_PROMPT"
