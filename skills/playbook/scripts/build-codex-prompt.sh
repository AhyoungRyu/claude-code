#!/usr/bin/env bash
set -euo pipefail

USER_MESSAGE="${1:-}"
if [ -z "$USER_MESSAGE" ]; then
  echo "Usage: build-codex-prompt.sh '<user message>'" >&2
  exit 1
fi

OUT_DIR=".omc/spec-forge"
mkdir -p "$OUT_DIR"

TEMPLATE="${HOME}/.claude/skills/spec-forge/templates/codex_runbook_prompt.md"
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

# 너무 길어지는 것을 방지: 기본은 Top list 섹션만 추출해서 넣고,
# Top list가 없으면 전체를 넣되 4000라인까지만 제한.
SKILLS_BLOCK="$(awk '
  BEGIN{in=0}
  /^## Top list/{in=1; next}
  /^## /{if(in==1) exit}
  {if(in==1) print}
' "$SKILLS_SNAPSHOT")"

if [ -z "$SKILLS_BLOCK" ]; then
  SKILLS_BLOCK="$(head -n 4000 "$SKILLS_SNAPSHOT")"
fi

# 템플릿 치환 (단순 placeholder 치환)
# - sed에서 개행/특수문자 처리가 까다로워서 awk로 안전하게 처리
awk -v um="$USER_MESSAGE" -v skills="$SKILLS_BLOCK" '
  {
    gsub(/\{\{USER_MESSAGE\}\}/, um)
    gsub(/\{\{SKILLS_SNAPSHOT_OR_TOP_LIST\}\}/, skills)
    print
  }
' "$TEMPLATE" > "$OUT_PROMPT"

echo "$OUT_PROMPT"
