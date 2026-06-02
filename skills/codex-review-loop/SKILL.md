---
name: codex-review-loop
description: Use when a GitHub PR needs repeated chatgpt-codex-connector bot review handling for the current head commit, including waiting for CI, replying to bot comments, minimizing and resolving bot threads, and stopping when the bot leaves no new feedback or reacts with thumbs up on the PR body.
---

# Codex Review Loop

Run a closed loop for `chatgpt-codex-connector[bot]` feedback on the current PR or a supplied PR.

## Scope

Use this for Codex bot feedback only.
For human reviewer comments or one-shot review processing, use `/review-pr-comments`.

## Inputs

- First positional argument: optional PR number or full PR URL
- Optional flags:
  - `--max-iterations=N` default `5`
  - `--poll-seconds=N` default `30`
  - `--ci-timeout-minutes=N` default `30`
  - `--settle-seconds=N` default `90`

If no PR is passed, use this fallback order:

1. PR number or URL already established in the current conversation
2. Open PR for the current branch
3. Stop and ask for the PR reference if neither exists

## Preflight

1. Verify GitHub CLI availability and auth first.
   ```bash
   command -v gh
   gh auth status
   ```
2. Confirm the current branch is clean enough to work on.
3. If there are unpushed commits on the branch, push them before starting the loop.
4. Do not rewrite history inside this loop unless the user explicitly asked for it.
   - Do not use `git reset --soft`
   - Do not use `git push --force`
   - Prefer normal commits and `git push`

## Resolve PR Context

Prefer the explicit PR argument. Otherwise use the current conversation PR if one is already clear. If that is not available, use the PR for the current branch.

Suggested shell outline:

```bash
PR_ARG=""
MAX_ITERATIONS=5
POLL_SECONDS=30
CI_TIMEOUT_MINUTES=30
SETTLE_SECONDS=90
```

Resolve:

- `OWNER` and `REPO`
- `PR_NUMBER`
- `PR_URL`
- `HEAD_BRANCH`
- `HEAD_SHA`

Suggested commands:

```bash
gh repo view --json owner,name
gh pr view "$PR_ARG" --json number,url,headRefName,headRefOid
```

If the first positional argument is a full PR URL, parse owner, repo, and number from it.

## Current-Head Queries

Always reason about the current PR head commit, not older pushes. Use `HEAD_SHA` as the boundary instead of trying to infer a push timestamp.

### 1. Codex bot thumbs-up on the PR body

If the bot reacted with `+1` on the PR itself, treat that as a terminal signal and stop.

```bash
BOT_APPROVED=$(gh api "repos/$OWNER/$REPO/issues/$PR_NUMBER/reactions" \
  -H "Accept: application/vnd.github+json" \
  --paginate \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | select(.content == "+1")] | length > 0')
```

### 2. Current-head Codex bot inline comments

```bash
HEAD_COMMENTS_JSON=$(gh api "repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments" \
  --paginate \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | select((.commit_id == "'"$HEAD_SHA"'") or (.original_commit_id == "'"$HEAD_SHA"'"))]')
```

### 3. Current-head Codex bot top-level reviews

```bash
HEAD_REVIEWS_JSON=$(gh api "repos/$OWNER/$REPO/pulls/$PR_NUMBER/reviews" \
  --paginate \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | select(.commit_id == "'"$HEAD_SHA"'")]')
```

Track handled ids in memory during the run so the same comment or review is not processed twice.

- `HANDLED_COMMENT_IDS`
- `HANDLED_REVIEW_IDS`

## Main Loop

Repeat until one of the stop conditions fires:

1. Refresh `HEAD_SHA`.
2. Check for PR-body `+1` reaction from `chatgpt-codex-connector[bot]`.
   - If present, stop immediately.
3. Wait for CI on the current head commit.
4. After CI is green, check current-head Codex bot comments and reviews.
5. If nothing new exists, keep polling for the settle window.
6. If still nothing new exists after the settle window, stop.
7. If new current-head bot feedback exists, address it.
8. Reply to the handled bot comment threads.
9. Minimize handled bot inline comments and all un-minimized bot review bodies.
10. Resolve the handled review threads.
11. If code changed, push and start the next iteration on the new head.
12. If no code changed and all current-head bot items were resolved with explanation, stop.

## Waiting For CI

Use `gh pr checks` if available in the installed GitHub CLI version. Poll until all required checks for the current head are green or until the timeout is reached.

Recommended behavior:

- Poll every `POLL_SECONDS`
- Stop waiting once required checks are green
- If checks fail, inspect the failing job
- If the failure is clearly caused by the latest change and can be fixed safely, fix it and continue
- Otherwise stop and report the CI failure as the blocker

Do not continue to the "no new bot feedback" check while required CI is still red or pending, unless the PR-body thumbs-up signal already appeared.

## Analyzing And Applying Bot Feedback

Only work on current-head bot feedback that has not been handled yet.

For each current-head bot comment:

1. Read the referenced file and current diff.
2. Decide whether the bot feedback needs a real code change.
3. If a change is needed, make the smallest correct fix and verify it.
4. If no code change is needed, prepare a short rationale explaining why.

Good reply patterns:

- `Addressed in <short-sha> - updated <what changed>.`
- `Already satisfied - no code change needed because <brief reason>.`
- `Deferred - <brief reason and next step>.`

Keep replies short and factual. One sentence is usually enough.

## Reply, Minimize, Resolve

Run these three operations together after the bot feedback for the current head has been handled.

### 1. Reply to each handled inline bot comment

```bash
gh api "repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments/$COMMENT_ID/replies" \
  -X POST \
  -f body="$REPLY_BODY"
```

Reply once per handled inline comment id. If several comments say the same thing, keep each reply brief instead of pasting a long summary repeatedly.

### 2. Minimize handled inline bot comments and all un-minimized bot review bodies

Use `RESOLVED` by default. Use `OUTDATED` only if the item clearly belongs to an outdated head and `RESOLVED` would be misleading.

Inline comment node ids:

```bash
COMMENT_NODE_IDS=$(gh api "repos/$OWNER/$REPO/pulls/$PR_NUMBER/comments" \
  --paginate \
  --jq '[.[] | select(.user.login == "chatgpt-codex-connector[bot]") | select((.commit_id == "'"$HANDLED_HEAD_SHA"'") or (.original_commit_id == "'"$HANDLED_HEAD_SHA"'")) | .node_id] | .[]')
```

Review summary body node ids — sweep ALL un-minimized bot review bodies, not just the
handled head. The bot posts a new "Codex Review" summary on every review pass; minimizing
only the current head's leaves earlier heads' summaries expanded, so they pile up. Use
GraphQL (REST `/reviews` has no `isMinimized` field) and filter to un-minimized, non-empty bodies:

```bash
REVIEW_NODE_IDS=$(gh api graphql -f query='
{
  repository(owner: "'"$OWNER"'", name: "'"$REPO"'") {
    pullRequest(number: '"$PR_NUMBER"') {
      reviews(first: 100) { nodes { author { login } isMinimized body id } }
    }
  }
}' --jq '[.data.repository.pullRequest.reviews.nodes[]
  | select(.author.login == "chatgpt-codex-connector")
  | select(.isMinimized == false)
  | select(.body != "")
  | .id] | .[]')
```

Minimize. Iterate with `while IFS= read -r` over a newline-separated list — `for NODE_ID in
$VAR` does NOT word-split under zsh and would send the whole blob as one `subjectId`:

```bash
printf '%s\n%s\n' "$COMMENT_NODE_IDS" "$REVIEW_NODE_IDS" | while IFS= read -r NODE_ID; do
  [ -z "$NODE_ID" ] && continue
  gh api graphql -f query='
    mutation {
      minimizeComment(input: {subjectId: "'"$NODE_ID"'", classifier: RESOLVED}) {
        minimizedComment { isMinimized }
      }
    }'
done
```

### 3. Resolve handled current-head bot review threads

Use GraphQL review threads filtered to the handled head commit.

```bash
THREAD_IDS=$(gh api graphql -f query='
{
  repository(owner: "'"$OWNER"'", name: "'"$REPO"'") {
    pullRequest(number: '"$PR_NUMBER"') {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes {
              author { login }
              originalCommit { oid }
            }
          }
        }
      }
    }
  }
}' --jq '[.data.repository.pullRequest.reviewThreads.nodes[]
  | select(.isResolved == false)
  | select([.comments.nodes[] | select(.author.login == "chatgpt-codex-connector" and .originalCommit.oid == "'"$HANDLED_HEAD_SHA"'")] | length > 0)
  | .id] | .[]')
```

Then resolve:

```bash
for THREAD_ID in $THREAD_IDS; do
  gh api graphql -f query='
    mutation {
      resolveReviewThread(input: {threadId: "'"$THREAD_ID"'"}) {
        thread { isResolved }
      }
    }'
done
```

### 4. Verify cleanup

Re-query the bot comments, reviews, and threads.

- Inline comments should be minimized
- Current-head bot review threads should be resolved
- No un-minimized bot review bodies should remain (re-run the `REVIEW_NODE_IDS` query — it must return empty)

If a cleanup operation fails because of permissions, report that explicitly and stop pretending the thread was fully cleaned up.

## Push Behavior

If code changed:

1. Verify locally when practical.
2. Commit with a normal commit message that fits the repository conventions.
3. Push the branch.
4. Store the new `HEAD_SHA`.
5. Start the next iteration on the new head.

If code did not change:

1. Reply, minimize, and resolve the handled current-head items.
2. Do not create a no-op commit.
3. Stop once there is no remaining current-head bot work.

## Stop Conditions

Stop the loop when one of these is true:

- `chatgpt-codex-connector[bot]` reacted with `+1` on the PR body
- CI is green and no new current-head bot comments or reviews appear during the settle window
- Current-head bot feedback was handled and no new push was required
- Maximum iterations reached
- CI failed and the loop is blocked on a non-trivial fix
- GitHub permissions prevent replying, minimizing, or resolving

## Output Summary

End with a short summary:

- PR url
- Iteration count
- Last processed head sha
- Bot feedback handled count
- Whether threads were replied to, minimized, and resolved
- Stop reason

If the loop stopped because of a blocker, say exactly what is blocked.
