#!/usr/bin/env bash
# loop-tasks.sh — Stop hook that refuses to let Claude exit while the
# TaskList still has pending or in_progress entries.
#
# Mechanism:
#   * Claude Code persists each task as ~/.claude/tasks/<session>/<id>.json
#     with fields { id, subject, status, ... }. Status values are
#     "pending" | "in_progress" | "completed" | "deleted".
#   * On every Stop, read the session_id from stdin, scan the task files,
#     count pending + in_progress. If > 0, emit
#     {"decision":"block","reason":...} so Claude keeps working.
#   * If every task is completed/deleted, exit 0 silently → Claude is
#     free to stop.
#   * Like loop-improvements.sh, we do NOT honour stop_hook_active —
#     natural termination is "no open tasks", not turn count.
#
# Failure modes (all → exit 0 / allow stop):
#   - No session_id on stdin
#   - No tasks directory for this session
#   - jq not installed
#
# Smoke tests:
#   printf '{}' | loop-tasks.sh                   → exit 0 silent
#   printf '{"session_id":"<bogus>"}' | loop-tasks.sh   → exit 0 silent
#   printf '{"session_id":"<real-id-with-open-tasks>"}' | loop-tasks.sh
#       → JSON with decision=block and the count of open tasks

set -uo pipefail

input="$(cat)"
session_id="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null)"
if [ -z "$session_id" ]; then
    exit 0
fi

tasks_dir="$HOME/.claude/tasks/${session_id}"
if [ ! -d "$tasks_dir" ]; then
    exit 0
fi

# Collect open tasks (pending or in_progress, excluding deleted).
mapfile -t open_lines < <(
    for f in "$tasks_dir"/*.json; do
        [ -f "$f" ] || continue
        jq -r '
          select(.status == "pending" or .status == "in_progress")
          | "\(.status)\t#\(.id) \(.subject)"
        ' "$f" 2>/dev/null
    done
)

open_count=${#open_lines[@]}
if [ "$open_count" -eq 0 ]; then
    exit 0
fi

# Build the continuation prompt. Show at most 8 task subjects so the
# prompt stays compact even on a big backlog.
preview="$(printf '%s\n' "${open_lines[@]}" | head -n 8)"
extra=""
if [ "$open_count" -gt 8 ]; then
    extra=$'\n…and '"$((open_count - 8))"$' more.'
fi

reason=$(cat <<EOF
${open_count} task(s) still pending or in_progress in the TaskList:

${preview}${extra}

Keep working through them. Use TaskList to see them, TaskUpdate to flip
status as you go. If a task is genuinely blocked (e.g. waiting on a
print to finish, a remote build, or user approval), reword its subject
to make the blocker explicit (e.g. "blocked-on-print-finish: …") and
move on to another item — don't get stuck.

Only stop when every task is completed or explicitly resolved.
EOF
)

printf '%s' "$reason" | jq -Rs '{decision:"block", reason:.}'
exit 0
