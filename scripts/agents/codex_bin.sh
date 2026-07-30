#!/usr/bin/env bash

# Shared Codex CLI resolution for run_task.sh and review_current.sh.
# The strictly confined Snap cannot read external agent-loop worktrees/state.

codex_bin_is_snap() {
  local candidate="$1"
  local resolved=""

  case "$candidate" in
    /snap/*) return 0 ;;
  esac
  resolved="$(readlink -f -- "$candidate" 2>/dev/null || true)"
  [[ "$resolved" == "/usr/bin/snap" || "$resolved" == /snap/* ]]
}

resolve_codex_bin() {
  local candidate=""
  local npm_candidate="${HOME}/.local/npm/bin/codex"
  local saw_snap=0

  if [[ -n "${CODEX_BIN:-}" ]]; then
    if [[ ! -x "$CODEX_BIN" ]]; then
      printf 'ERROR: configured CODEX_BIN is not executable: %s\n' "$CODEX_BIN" >&2
      return 1
    fi
    if codex_bin_is_snap "$CODEX_BIN"; then
      printf 'ERROR: refusing Snap Codex from CODEX_BIN: %s; use the npm or editor-bundled CLI\n' \
        "$CODEX_BIN" >&2
      return 1
    fi
    printf '%s\n' "$CODEX_BIN"
    return 0
  fi

  if [[ -x "$npm_candidate" ]] && ! codex_bin_is_snap "$npm_candidate"; then
    printf '%s\n' "$npm_candidate"
    return 0
  fi

  while IFS= read -r candidate; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if codex_bin_is_snap "$candidate"; then
      saw_snap=1
      continue
    fi
    printf '%s\n' "$candidate"
    return 0
  done < <(type -aP codex 2>/dev/null || true)

  while IFS= read -r candidate; do
    if [[ -x "$candidate" ]] && ! codex_bin_is_snap "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(
    find "$HOME/.vscode/extensions" "$HOME/.cursor/extensions" \
      -path '*/openai.chatgpt-*/bin/*/codex' -type f 2>/dev/null | sort -Vr
  )

  if [[ "$saw_snap" -eq 1 ]]; then
    printf 'ERROR: only the incompatible Snap Codex was found; install/use the npm CLI\n' >&2
  fi
  return 1
}
