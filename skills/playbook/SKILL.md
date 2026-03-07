---
name: playbook
description: >
  Turn any natural-language goal into a structured, executable runbook.
  Classifies the task type first (code-change, code-cleanup, refactor, file-ops, research, config, docs, planning),
  then delegates to Codex to produce a phase-appropriate runbook with matching gates and constraints,
  optimized to use the user's available Claude Code skills.
allowed-tools: Bash, Read, Write, Edit, Glob, Grep, Agent, Skill
---

# playbook (user-level)

> **CRITICAL EXECUTION RULE**: You MUST complete ALL steps (R → A → B → C → C2 → D → E → E2 → F) in sequence during every invocation. Do NOT skip steps. Do NOT abandon the playbook workflow to work on the user's task directly. The user's task is executed IN Step F as part of the runbook — never outside the playbook flow. If you reset artifacts (Step R) but fail to complete subsequent steps, the playbook run is broken.

## What you do
Given the user's goal (any type), you will:
0) Clarify ONLY if truly critical (cannot infer + would fundamentally change execution). Otherwise proceed autonomously.
1) **Reset run artifacts** (Step R) — overwrite work.md, result.md, plan.md with a timestamped header.
2) Classify the task type to select the appropriate phase template and constraints.
3) Snapshot all available Claude Code skills by scanning standard skill directories (do NOT rely on `/skills`).
4) Load persistent steering context from `$PLAYBOOK_DIR/steering.md` if it exists.
5) Delegate to Codex (via `/oh-my-claudecode:omc-teams 1:codex`) to produce a high-quality runbook:
   - The runbook must match the detected task type (not always code-focused).
   - The runbook must be generic and repo-aware (detect available scripts/commands; do not assume).
   - The runbook must maximize correct use of available skills.
   - The runbook must include a cross-phase consistency check section.
6) Save the runbook to `$PLAYBOOK_DIR/work.md`.
7) For code tasks (`code-change`, `refactor`, `code-cleanup`): extract the Plan phase to `$PLAYBOOK_DIR/plan.md` BEFORE any implementation starts.
8) Execute immediately unless a critical gate is triggered (see Step F).

---

## Step R — Reset run artifacts (runs FIRST, every invocation, no exceptions)

Before any classification or planning, initialize this run's artifacts with fresh headers:

```bash
if [ -d ".omc" ]; then
  PLAYBOOK_DIR=".omc/playbook"
else
  PLAYBOOK_DIR=".context/playbook"
fi
mkdir -p "$PLAYBOOK_DIR"
RUN_TS=$(date -Iseconds)
printf "# work.md\nRun: %s\n\n*(runbook will be written here)*\n" "$RUN_TS" > "$PLAYBOOK_DIR/work.md"
printf "# result.md\nRun: %s\n\n*(run summary will be written here after execution)*\n" "$RUN_TS" > "$PLAYBOOK_DIR/result.md"
printf "# plan.md\nRun: %s\n\n*(plan will be written here before execution)*\n" "$RUN_TS" > "$PLAYBOOK_DIR/plan.md"
```

**Why**: `$PLAYBOOK_DIR` is `.omc/playbook` when `.omc/` exists (OMC users), or `.context/playbook` otherwise — so playbook works with or without OMC. Each invocation is a fresh run. Stale content from previous iterations must never bleed into the current runbook or be mistaken for current-run output. The timestamp header makes it trivially clear which run produced each file.

---

## Step 0 — Clarify (almost always skip)

**Default behavior: proceed autonomously.** Claude should infer the best interpretation and run.

When multiple valid interpretations exist, pick the most comprehensive / least lossy option:
- Scope ambiguous (e.g. "all" vs subset) → choose all
- Task type ambiguous (e.g. refactor vs code-change) → choose the type that satisfies the user's end goal
- Axis/area ambiguous → cover all relevant axes

**Only stop and ask if ALL of the following are true:**
1. The information cannot be reasonably inferred from context.
2. The two possible answers would lead to fundamentally different runbooks (not just different scope).
3. One of the answers could trigger irreversible, destructive, or externally visible actions.

**Examples — do NOT ask:**
- "which axis should I focus on?" → cover all axes
- "refactor or code-change?" → pick the type that satisfies the goal
- "runbook only or execute too?" → execute (user asked to fix things)
- "all files or just this one?" → all relevant files in scope

**Examples — MUST ask:**
- Target environment is unknown and the command differs completely (prod vs dev)
- The task would permanently delete data and the scope is unclear ("clean up old records" — which records?)
- External credential/URL is required and not present anywhere in the repo

If asking, keep to 1 question maximum. Wait for the answer before proceeding.

---

## Step A — Task Classification

Classify the user's request into one of these types.
If a task spans multiple types, pick the dominant type and note the secondary type — apply constraints from both.

| Type | Examples | Phase template |
|------|----------|----------------|
| `code-change` | new features, bug fixes, type changes | Baseline → Plan → Implement → Proof |
| `refactor` | structural improvements, mixed file moves + code changes, module extraction | Baseline → Plan → Refactor → Proof |
| `code-cleanup` | dead code removal, unused imports/variables/functions | Scan → Plan → Remove → Proof |
| `file-ops` | file organization/deletion/move/rename (no code changes) | Inventory → Plan → Execute → Verify |
| `research` | code analysis, auditing, pattern identification | Context → Analyze → Report |
| `config` | package.json, CI, tsconfig, configuration changes | Baseline → Change → Verify |
| `docs` | documentation writing/editing, README, comments | Draft → Review → Finalize |
| `planning` | architecture, design decisions, tech choices | Context → Options → Recommend |

Record the chosen type (and secondary type if mixed) at the top of the runbook.

---

## Step B — Constraints (adaptive by task type)

**All tasks:**
- Minimal scope: do only what was asked, no extras.
- Produce trace artifacts under `$PLAYBOOK_DIR/`.

**`code-change` only (new features, bug fixes, type/API changes):**
- No `any`, no `as any`, no `ts-ignore`.
- No regressions — tests and build must stay green.
- No public API surface changes unless user explicitly allows it.
- Deterministic tests: no real sleep; use fake timers/injection when needed.

**`refactor` only:**
- No observable behavior change — this is the primary invariant.
- Tests must pass before and after with zero diff in outcomes.
- If files are moved: apply `file-ops` reference-check constraints too.
- No public API surface changes unless user explicitly allows it.
- No `any`, no `as any`, no `ts-ignore`.

**`code-cleanup` only:**
- Before removing any symbol (function, variable, type, import): verify zero references
  via LSP find-references or grep across the entire repo.
- Before deleting a file: apply `file-ops` reference-check constraints.
- Document each removal in `plan.md` with: what was removed + why it was unused.
- Tests must pass after all removals.
- No `any`, no `as any`, no `ts-ignore`.

**`file-ops` only:**
- Inventory files before any deletion (list what will be removed).
- Check for references/imports before deleting any file.
- Do not delete files that are imported or referenced anywhere.

**`research` / `planning` only:**
- Cite concrete file paths and line numbers as evidence.
- No code changes unless user explicitly requests them.

**`config` only:**
- Run relevant build/test gate before and after the change.
- Keep lock files consistent.

---

## Step C — Skills snapshot (deterministic)

Run the scan script to generate `$PLAYBOOK_DIR/skills_snapshot.md`:

```bash
bash ~/.claude/skills/playbook/scripts/scan-skills.sh "$PLAYBOOK_DIR/skills_snapshot.md"
```

The snapshot produces two sections:
1. **File-based skills** — slash commands derived from each `SKILL.md`'s `name:` field (e.g. `/playbook`, `/senior-frontend`)
2. **OMC built-in agents** — the `oh-my-claudecode:*` agent catalog (e.g. `oh-my-claudecode:executor`)

**Enforcement gates (do not skip):**

1. **Non-empty check**: After running the script, verify the snapshot contains at least one `|` table row. If it is empty, the scan failed — stop and report the error before proceeding.

2. **Step mapping rule**: Every step in `work.md` and `plan.md` MUST either:
   - Name a skill/agent from the snapshot by its exact command, **or**
   - State `"direct implementation — <one-line reason>"`.
   No step may be left unmapped or vaguely attributed to "an agent".

3. **Execution rule (Step F)**: When a runbook step is mapped to a skill, **invoke it using the `Skill` tool** (for file-based skills) or the **`Agent` tool** (for `oh-my-claudecode:*` agents). Do not inline what the skill would do — actually call it.

---

## Step C2 — Load Steering Context

Check if `$PLAYBOOK_DIR/steering.md` exists in the current repo.

- **If it exists:** read the full content. This will be injected into the Codex prompt as `STEERING CONTEXT`.
- **If it doesn't exist:** skip — no steering context will be passed.

Steering captures persistent project-level knowledge that should constrain every runbook:
- Architecture decisions already made (e.g. "we use Zustand, not Redux")
- Prohibited patterns (e.g. "never use barrel imports")
- Naming conventions and folder structure rules
- Outcomes from past playbook runs worth remembering

Users can edit `$PLAYBOOK_DIR/steering.md` directly at any time.
After a run, if significant decisions were made, offer to append a summary to steering.md.

---

## Step D — Author the runbook

**Primary path — Codex delegation** (when `oh-my-claudecode:omc-teams` is available):
- `/oh-my-claudecode:omc-teams 1:codex "<PROMPT>"`

**Fallback path — Claude authors directly** (when OMC is not installed, OR when Codex delegation fails for any reason such as MCP errors, quota limits, tool unavailability):
1. Read the prompt template: `~/.claude/skills/playbook/templates/codex_runbook_prompt.md`
2. Fill in all `{{PLACEHOLDERS}}` with actual values from Steps A-C
3. Write the complete runbook yourself, following all the same constraints and phase templates
4. Use the **Write tool** to save to `$PLAYBOOK_DIR/work.md`

**IMPORTANT**: If the primary path fails, immediately fall back to the Claude-authors-directly path. Do NOT abandon the playbook workflow. Either path produces the same artifact: a complete runbook written to `$PLAYBOOK_DIR/work.md`.

Use the template at `.claude/skills/playbook/templates/codex_runbook_prompt.md`, substituting:
- `{{USER_MESSAGE}}` — the user's raw goal (verbatim, post-clarification)
- `{{TASK_TYPE}}` — classified type + secondary type if mixed
- `{{PHASE_TEMPLATE}}` — the matching phase template from Step A
- `{{CONSTRAINTS}}` — the constraints relevant to the detected type(s) from Step B
- `{{SKILLS_SNAPSHOT_OR_TOP_LIST}}` — skills snapshot from Step C (condense if too large)
- `{{STEERING_CONTEXT}}` — steering.md content from Step C2, or `(none)` if absent

**To generate the filled prompt automatically**, run:
```bash
bash ~/.claude/skills/playbook/scripts/build-codex-prompt.sh \
  '<user_message>' '<task_type>' '<phase_template>' '<constraints>'
```
This outputs the filled prompt to `$PLAYBOOK_DIR/codex_prompt.txt` and auto-loads `steering.md`.
All 6 placeholders are substituted. Pass the prompt content to Codex via `/oh-my-claudecode:omc-teams`.

The runbook Codex produces MUST satisfy:
- Matches the task type — do NOT apply code gates (test/build/API surface) to non-code tasks.
- Includes only phases relevant to the task type.
- For mixed types, includes phases and constraints from both types.
- Includes a **"Skill Orchestration"** section: maps each step to a specific skill from the snapshot; uses only skills that actually exist; prefers fewer skills if they cover the need.
- Is repo-agnostic: may inspect package.json scripts; must NOT assume specific workspace names.
- Creates trace artifacts under `$PLAYBOOK_DIR/`: `baseline.md` (if applicable), `plan.md`, `result.md`.
- Includes a **"Consistency Check"** section at the end (see Step D2).
- Scales complexity to the task: simple file-ops → concise runbook; complex code-change → full runbook.

---

## Step D2 — Cross-Phase Consistency Check

The Codex-generated runbook MUST include a `## Consistency Check` section as its final section.

This section must answer:
1. **Phase dependency**: Does each phase assume outputs from the previous phase? Are there any gaps?
2. **Constraint coverage**: Are all constraints from Step B reflected somewhere in the runbook phases?
3. **Scope alignment**: Does the runbook scope match the user's stated goal — no more, no less?
4. **Skill reachability**: Are all mapped skills actually available in the snapshot? Any unmapped steps?

If any issue is found, the runbook must note it inline as `⚠️ ISSUE: <description>` so the user can spot it before execution.

---

## Step E — Materialize

Write the runbook to `$PLAYBOOK_DIR/work.md` using the **Write tool** (or Bash printf/redirect). The file MUST contain the full runbook — not the placeholder from Step R.

**Verification gate**: After writing, confirm `work.md` no longer contains `*(runbook will be written here)*`. If it does, the write failed — retry.

If the Consistency Check section contains any `⚠️ ISSUE:` entries:
- Surface them to the user.
- Ask whether to proceed anyway or adjust the runbook first.
- Wait for the answer before continuing.

If no issues: proceed to Step E2 immediately without asking.

---

## Step E2 — Write plan.md before execution (code tasks only)

**Applies to: `code-change`, `refactor`, `code-cleanup` tasks.**
For other task types (research, docs, planning, file-ops, config): skip this step, proceed to Step F.

Extract the Plan phase from `work.md` and write it to `$PLAYBOOK_DIR/plan.md` using the **Write tool** (or Bash printf/redirect) **before any code is touched**.

**Verification gate**: After writing, confirm `plan.md` no longer contains `*(plan will be written here before execution)*`. If it does, the write failed — retry.

`plan.md` MUST include:
1. **Files to modify** — list each file path and the nature of change
2. **Change rationale** — why each change is needed
3. **Skill mapping** — for each implementation step, which skill from `skills_snapshot.md` will be used and why
   - Reference skills by their exact slash-command name (e.g. `/oh-my-claudecode:executor`)
   - If no skill applies to a step, explicitly note "direct implementation" with one-line justification
4. **Test/build gates** — which commands will be run after implementation to prove correctness

**Hard gate**: `$PLAYBOOK_DIR/plan.md` MUST be written and non-empty before Step F begins.
The plan.md must reference at least one skill from `skills_snapshot.md` by name.
Do NOT start modifying source files until plan.md exists on disk.

---

## Step F — Execute autonomously (stop only on critical gates)

**Mandatory: invoke tools, do not inline.** Before executing each runbook step, check its skill mapping from `plan.md`:
- File-based skill (e.g. `/playbook`, `/senior-frontend`) → invoke via **`Skill` tool**
- OMC agent (e.g. `oh-my-claudecode:executor`) → invoke via **`Agent` tool**
- Never describe what a skill/agent would do and then do it yourself inline. Actually call the tool.

**Default: execute the runbook without interruption.** Do not ask for confirmation between phases.

Proceed through all phases automatically unless a **critical gate** is triggered:

| Critical gate | Action |
|---|---|
| `⚠️ ISSUE:` found in Consistency Check | Surface issues, ask before proceeding |
| Analyze/Scan phase reveals scope ≥3x larger than expected | Report scope, ask before implementing |
| A phase would perform irreversible deletion of data/files | List what will be deleted, ask for confirmation |
| Build/test baseline fails before any changes | Report failure, ask whether to continue |
| A planned change would alter a public API surface | Flag it, ask for explicit approval |

For all other situations — including ambiguous task types, multiple valid approaches, incomplete information that can be reasonably inferred — **make the best decision and proceed.**

**Mandatory result.md gate — write this before declaring completion:**

Write the run summary to `$PLAYBOOK_DIR/result.md` using the **Write tool** (or Bash printf/redirect), containing:

```markdown
# result.md
Run: <ISO timestamp>
Task type: <classified type>

## Changes / Findings
<summary of what was done, or findings for research tasks>

## Skills invoked
<list each by exact slash-command / agent name, or "none" for research tasks>

## Artifacts produced
<list of files written under $PLAYBOOK_DIR/>

## Risks / TODOs
<any open items, or "none">
```

**Do not present the final summary to the user until `result.md` has been confirmed written to disk.**

Then present a brief summary to the user (what changed, artifacts produced, any TODOs).

---

## Output directory (MANDATORY)

Write ALL artifacts under `$PLAYBOOK_DIR/` (determined in Step R: `.omc/playbook/` or `.context/playbook/`).

Expected artifacts:
- `$PLAYBOOK_DIR/skills_snapshot.md`
- `$PLAYBOOK_DIR/steering.md` (persistent across runs; user-managed)
- `$PLAYBOOK_DIR/work.md`
- `$PLAYBOOK_DIR/baseline.md` (if applicable)
- `$PLAYBOOK_DIR/plan.md` ← **REQUIRED before execution for code-change / refactor / code-cleanup**
- `$PLAYBOOK_DIR/result.md` ← **REQUIRED at end of every run**

Do NOT write to `docs/` or `.ai/`.

---

## Success criteria

- Runs autonomously end-to-end with minimal user interruption.
- Only stops when a critical gate is triggered (irreversible action, unexpected scope, build failure, API surface change).
- Task type is correctly classified (including mixed types) and drives the runbook structure.
- Steering context loaded and injected when available.
- Generated runbook is complete and appropriate for the task type.
- Consistency Check passes (no `⚠️ ISSUE:` entries), or issues are surfaced and resolved.
- **For code tasks: `plan.md` is written to disk BEFORE the first source file is touched.**
- **Skills from `skills_snapshot.md` are referenced by exact name in `work.md` and `plan.md` — and actually invoked via `Skill`/`Agent` tool during Step F execution (not just mentioned in text).**
- `$PLAYBOOK_DIR/result.md` is written and non-empty at the end of every run (verifiable on disk).
- `$PLAYBOOK_DIR/work.md` contains the current run's timestamp (not a stale previous run's content).

## Final completion checkpoint

Before presenting the summary to the user, verify ALL of these are true:
1. `$PLAYBOOK_DIR/skills_snapshot.md` — exists and has the current run's timestamp
2. `$PLAYBOOK_DIR/work.md` — contains the full runbook (NOT the Step R placeholder)
3. `$PLAYBOOK_DIR/plan.md` — contains the plan (for code tasks) OR is at least updated with findings (for non-code tasks)
4. `$PLAYBOOK_DIR/result.md` — contains the run summary (NOT the Step R placeholder)

If ANY file still contains its Step R placeholder text, the playbook run is incomplete. Go back and complete the missing steps.
