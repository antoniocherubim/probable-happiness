#!/usr/bin/env bash
set -euo pipefail

TOOL_ROOT="${AGENT_LOOP_TOOL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
source "$TOOL_ROOT/scripts/agents/codex_bin.sh"
PYTHON_BIN="${AGENT_LOOP_PYTHON:-python3}"
DX_CLI="$TOOL_ROOT/scripts/agents/telegram_bridge.py"

usage() {
  printf '%s\n' \
    "Usage: agent-loop yolo --repo <path> [--state-root <path>] [--env-file <path>] [--base-ref <ref>] <task-file> [<task-file> ...]" \
    "" \
    "Every task must already be tracked. The project profile at each task base must use approval.mode='yolo'."
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '[agent-loop] %s\n' "$*"
}

safe_task_path() {
  local task="$1"
  [[ -n "$task" && "$task" != /* && "$task" != *".."* ]]
}

allocate_chain_dir() {
  local root="$1"
  local stamp base candidate suffix
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$root"
  base="$root/yolo-$stamp"
  candidate="$base"
  suffix=0
  while ! mkdir "$candidate" 2>/dev/null; do
    suffix=$((suffix + 1))
    [[ "$suffix" -le 10000 ]] || die "could not allocate YOLO sequence directory"
    candidate="$base-$suffix"
  done
  printf '%s\n' "$candidate"
}

token_exhausted_in() {
  local path="$1"
  local pattern='(token|context|usage)[[:space:]_-]*(budget|window|length|limit|quota)?[^[:cntrl:]]{0,80}(exhausted|exceeded|reached|used[[:space:]]*up|full|try[[:space:]]+again|reset)|((maximum|max)[[:space:]_-]*)?(token|context)[[:space:]_-]*(budget|window|length|limit|quota)[^[:cntrl:]]{0,40}(exhausted|exceeded|reached|full)|(hit|reached|exceeded)[^[:cntrl:]]{0,40}(token|context|usage)[[:space:]_-]*(budget|window|length|limit|quota)|out[[:space:]]+of[^[:cntrl:]]{0,20}tokens|0[^[:cntrl:]]{0,20}tokens[^[:cntrl:]]{0,20}left'
  [[ -f "$path" ]] && grep -Eqi "$pattern" "$path"
}

REPO=""
STATE_ROOT=""
ENV_FILE=""
BASE_REF="HEAD"
TASKS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo requires a path"; REPO="$2"; shift 2 ;;
    --state-root) [[ $# -ge 2 ]] || die "--state-root requires a path"; STATE_ROOT="$2"; shift 2 ;;
    --env-file) [[ $# -ge 2 ]] || die "--env-file requires a path"; ENV_FILE="$2"; shift 2 ;;
    --base-ref) [[ $# -ge 2 ]] || die "--base-ref requires a ref"; BASE_REF="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) die "unknown yolo option: $1" ;;
    *) TASKS+=("${1#./}"); shift ;;
  esac
done

[[ -n "$REPO" ]] || die "yolo requires --repo"
[[ "${#TASKS[@]}" -gt 0 ]] || die "yolo requires at least one task file"
REPO_ROOT="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null)" || \
  die "target is not a Git repository: $REPO"
[[ -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)" ]] || \
  die "YOLO requires a clean target repository"
BRANCH="$(git -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null)" || \
  die "YOLO requires a checked-out local branch"
START_COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify "${BASE_REF}^{commit}" 2>/dev/null)" || \
  die "invalid base ref: $BASE_REF"
CURRENT_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$START_COMMIT" == "$CURRENT_HEAD" ]] || \
  die "YOLO base ref must resolve to the current local HEAD"

MODE="$("$PYTHON_BIN" "$DX_CLI" approval-mode --repo "$REPO_ROOT" \
  --base-commit "$START_COMMIT" --missing-policy deny)" || \
  die "invalid or missing tracked project profile"
[[ "$MODE" == "yolo" ]] || die "agent-loop yolo requires approval.mode='yolo'"

declare -A SEEN_TASKS=()
for task in "${TASKS[@]}"; do
  safe_task_path "$task" || die "unsafe task path: $task"
  [[ -z "${SEEN_TASKS[$task]:-}" ]] || die "duplicate task in YOLO sequence: $task"
  SEEN_TASKS[$task]=1
  git -C "$REPO_ROOT" cat-file -e "${START_COMMIT}:${task}" 2>/dev/null || \
    die "$task must be tracked before the YOLO sequence starts"
done

STATE_ARGS=()
if [[ -n "$STATE_ROOT" ]]; then STATE_ARGS=(--state-root "$STATE_ROOT"); fi
PROJECT_STATE="$("$PYTHON_BIN" "$DX_CLI" project-state-dir --repo "$REPO_ROOT" "${STATE_ARGS[@]}")" || \
  die "could not resolve external project state"
mkdir -p "$PROJECT_STATE/runs"
CHAIN_DIR="$(allocate_chain_dir "$PROJECT_STATE/chains")"
printf '%s\n' "$START_COMMIT" > "$CHAIN_DIR/start_commit"
printf '%s\n' "$BRANCH" > "$CHAIN_DIR/branch"
printf '%s\n' "${TASKS[@]}" > "$CHAIN_DIR/tasks"
printf '%s\n' RUNNING > "$CHAIN_DIR/status"
note "YOLO sequence started: $CHAIN_DIR"
note "branch=$BRANCH tasks=${#TASKS[@]} iteration_limit=unlimited"

RUN_ARGS=(--repo "$REPO_ROOT")
if [[ -n "$STATE_ROOT" ]]; then RUN_ARGS+=(--state-root "$STATE_ROOT"); fi
if [[ -n "$ENV_FILE" ]]; then RUN_ARGS+=(--env-file "$ENV_FILE"); fi
RUN_ARGS+=(--require-profile)

EXPECTED_HEAD="$START_COMMIT"
LAST_RUN_DIR=""
for task in "${TASKS[@]}"; do
  [[ -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)" ]] || {
    printf '%s\n' BLOCKED_REPOSITORY_DIRTY > "$CHAIN_DIR/status"
    die "repository became dirty before task $task"
  }
  [[ "$(git -C "$REPO_ROOT" rev-parse HEAD)" == "$EXPECTED_HEAD" ]] || {
    printf '%s\n' BLOCKED_HEAD_DRIFT > "$CHAIN_DIR/status"
    die "repository HEAD drifted before task $task"
  }
  BEFORE_RUNS="$(find "$PROJECT_STATE/runs" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)"
  note "YOLO starting task=$task base=$EXPECTED_HEAD"
  set +e
  "$TOOL_ROOT/agent-loop" run "${RUN_ARGS[@]}" "$task" 1 "$EXPECTED_HEAD"
  TASK_EXIT=$?
  set -e
  AFTER_RUNS="$(find "$PROJECT_STATE/runs" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort)"
  LAST_RUN_NAME="$(comm -13 <(printf '%s\n' "$BEFORE_RUNS") <(printf '%s\n' "$AFTER_RUNS") | tail -n 1)"
  if [[ -n "$LAST_RUN_NAME" ]]; then
    LAST_RUN_DIR="$PROJECT_STATE/runs/$LAST_RUN_NAME"
    printf '%s\n' "$LAST_RUN_DIR" >> "$CHAIN_DIR/runs"
  fi
  if [[ "$TASK_EXIT" -ne 0 ]]; then
    printf '%s\n' BLOCKED_TASK > "$CHAIN_DIR/status"
    note "YOLO sequence stopped at task=$task exit=$TASK_EXIT"
    [[ -z "$LAST_RUN_DIR" ]] || note "run preserved: $LAST_RUN_DIR"
    exit "$TASK_EXIT"
  fi
  NEW_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  [[ "$NEW_HEAD" != "$EXPECTED_HEAD" ]] || {
    printf '%s\n' BLOCKED_NOT_INTEGRATED > "$CHAIN_DIR/status"
    die "approved task did not advance the local branch: $task"
  }
  [[ "$(git -C "$REPO_ROOT" rev-parse "${NEW_HEAD}^")" == "$EXPECTED_HEAD" ]] || {
    printf '%s\n' BLOCKED_NON_LINEAR > "$CHAIN_DIR/status"
    die "task integration was not exactly one local commit: $task"
  }
  [[ -z "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)" ]] || {
    printf '%s\n' BLOCKED_REPOSITORY_DIRTY > "$CHAIN_DIR/status"
    die "repository is dirty after task integration: $task"
  }
  printf '%s %s\n' "$NEW_HEAD" "$task" >> "$CHAIN_DIR/commits"
  EXPECTED_HEAD="$NEW_HEAD"
  note "YOLO committed task=$task commit=$NEW_HEAD"
done

CODEX_BIN="$(resolve_codex_bin || true)"
[[ -n "$CODEX_BIN" && -x "$CODEX_BIN" ]] || {
  printf '%s\n' BLOCKED_FINAL_REVIEW > "$CHAIN_DIR/status"
  die "Codex CLI not found for final technical review"
}
FINAL_REPORT="$CHAIN_DIR/final-review.md"
FINAL_CANDIDATE="$CHAIN_DIR/.final-review.candidate.md"
TASK_LIST="$(printf '%s, ' "${TASKS[@]}")"
FINAL_PROMPT="Act only as the final technical reviewer. Do not edit repository files and do not invoke or request an executor. Review the cumulative locally committed change from $START_COMMIT through $EXPECTED_HEAD on branch $BRANCH for these tasks: $TASK_LIST. Inspect the task specifications, commits, implementation, documentation, and relevant tests. Focus on cross-task integration defects, regressions, unmet acceptance criteria, and concrete technical risks. Output Markdown only, starting with '# Final technical review'. List findings in severity order with file paths and actionable details. If no findings exist, state that explicitly. This report is informational: do not request another implementation iteration and do not propose push, PR, merge, deploy, rollback, or history rewriting."

set +e
"$PYTHON_BIN" "$DX_CLI" yolo-final-review \
  --repo "$REPO_ROOT" --run-dir "$CHAIN_DIR" \
  --start-commit "$START_COMMIT" --end-commit "$EXPECTED_HEAD" \
  --report "$FINAL_CANDIDATE" -- \
  "$CODEX_BIN" exec --ephemeral --sandbox read-only -C "$REPO_ROOT" \
    --output-last-message "$FINAL_CANDIDATE" "$FINAL_PROMPT"
FINAL_EXIT=$?
set -e
if [[ "$(git -C "$REPO_ROOT" rev-parse HEAD)" != "$EXPECTED_HEAD" ]] || \
   [[ -n "$(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=all)" ]]; then
  printf '%s\n' BLOCKED_FINAL_REVIEW_MUTATION > "$CHAIN_DIR/status"
  die "final reviewer changed the target repository; inspect it before continuing"
fi
if [[ "$FINAL_EXIT" -ne 0 || ! -s "$FINAL_CANDIDATE" ]]; then
  if token_exhausted_in "$CHAIN_DIR/final-review.log" || token_exhausted_in "$FINAL_CANDIDATE"; then
    printf '%s\n' BLOCKED_FINAL_REVIEW_TOKEN_EXHAUSTED > "$CHAIN_DIR/status"
    die "final reviewer exhausted its token/context budget; sequence ended"
  fi
  printf '%s\n' BLOCKED_FINAL_REVIEW > "$CHAIN_DIR/status"
  die "final technical review failed; integrated commits were preserved"
fi
mv "$FINAL_CANDIDATE" "$FINAL_REPORT"
printf '%s\n' "$EXPECTED_HEAD" > "$CHAIN_DIR/end_commit"
printf '%s\n' COMPLETE > "$CHAIN_DIR/status"
note "YOLO sequence complete; local commits=${#TASKS[@]} push=none branch_created=none"
note "final technical review: $FINAL_REPORT"
