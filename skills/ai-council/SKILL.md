---
name: ai-council
description: >
  Multi-AI collaboration using Codex CLI and Gemini CLI. Use when user says
  "AI들끼리 논의해봐", "여러 AI 의견", "다른 AI들 생각은", "AI council",
  or needs diverse AI perspectives for complex decisions, architecture reviews,
  or problem solving.
---

# AI Council

Orchestrate discussions between multiple AI systems (Claude, Codex, Gemini) to reach well-reasoned consensus on complex problems.

## Prerequisites

Verify both CLI tools are installed:

```bash
codex --version
gemini --version
```

If not installed:
```bash
# Codex CLI
npm install -g @openai/codex
codex auth

# Gemini CLI
npm install -g @google/gemini-cli
```

## AI Strengths

| AI | Strengths | Best For |
|----|-----------|----------|
| **Claude** | Nuanced reasoning, safety, detailed explanations | Complex analysis, edge cases |
| **Codex** (OpenAI) | Code generation, practical solutions | Implementation details, patterns |
| **Gemini** (Google) | Large context (1M tokens), security review | Codebase-wide analysis, architecture |

## Workflow

### 1. Frame the Problem
Clearly state the decision/problem and identify relevant context files.

### 2. Gather Perspectives (parallel when possible)

**Query Codex:**
```bash
# Default model: gpt-5-codex (complex tasks)
codex exec "What's your recommended approach for [problem]? Focus on implementation trade-offs." --context-file <file>

# For simple/fast queries
codex exec --model gpt-5-codex-mini "<simple question>"

# Always use suggest mode for safety
codex exec --approval-mode suggest "<task>" --context-file <file>
```

**Query Gemini:**
```bash
# Default or specify model for complex analysis
gemini -m gemini-3.1-pro-preview "Analyze from architecture and security standpoints: [problem]. Context: $(cat <file>)"

# Include entire directories for project-wide analysis (leverage 1M context)
gemini --include-directories ./src,./lib "<analyze project structure>"

# Enable web search for latest info
gemini --web-search "<latest library trends>"
```

### 3. Synthesize
- Compare all AI responses
- Identify agreements (high confidence)
- Identify disagreements (needs deeper analysis)
- Ask follow-ups if needed

### 4. Report Consensus

## Output Format

```markdown
## Multi-AI Consensus Report

### Problem Statement
[Description]

### AI Perspectives

**Claude:** [Your analysis]
**Codex:** [Summary]
**Gemini:** [Summary]

### Agreement
- [Point 1]
- [Point 2]

### Discussion Points
| Topic | Claude | Codex | Gemini |
|-------|--------|-------|--------|

### Recommendation
[Final synthesized recommendation]

### Next Steps
1. [Action 1]
2. [Action 2]
```

## Examples

### Architecture Decision
```bash
# Get Codex's view
codex exec "Should we use Redux or Context API? $(cat src/App.tsx | head -50)"

# Get Gemini's view (leverage directory inclusion)
gemini --include-directories ./src "Recommend Redux vs Context API with rationale"
```

### Code Review
```bash
codex exec "Review for bugs and improvements" --context-file src/utils.ts
gemini -m gemini-3.1-pro-preview "Security and performance review: $(cat src/utils.ts)"
```

### Bug Investigation
```bash
codex exec "What could cause this error? $(cat error.log | tail -30)"
gemini "Analyze potential root causes: $(cat error.log | tail -30)"
```

## Guidelines

- Summarize and synthesize, don't copy verbatim
- Give equal weight to well-reasoned arguments
- Acknowledge uncertainty when consensus isn't possible
- 2-3 rounds of discussion is usually sufficient
- Never send sensitive data (credentials, secrets) to external AIs
- Response times may vary (30+ seconds for complex queries)
- OMC ask skill (/oh-my-claudecode:ask codex/gemini) can also be used for individual queries
