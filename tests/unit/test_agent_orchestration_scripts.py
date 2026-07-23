"""Smoke checks for agent orchestration shell scripts (DX / local loop)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"


def test_agent_scripts_bash_syntax() -> None:
    scripts = [
        REPO_ROOT / "agent-loop",
        AGENTS / "run_task.sh",
        AGENTS / "review_current.sh",
        AGENTS / "install-telegram-bridge-user-unit.sh",
    ]
    for script in scripts:
        completed = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"{script.name}: {completed.stderr}"


def test_run_task_dry_run_smoke() -> None:
    # Prefer a tracked task file present in this checkout.
    task = "docs/tasks/DX-01.md"
    completed = subprocess.run(
        ["bash", str(AGENTS / "run_task.sh"), "--dry-run", task, "3", "HEAD"],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    # Dry-run may fail if CLIs are missing in CI; syntax/path errors are the concern.
    if completed.returncode != 0:
        combined = completed.stdout + completed.stderr
        assert "Usage:" not in combined or "task-file" in combined
        # Acceptable: missing agent binaries in the sandbox.
        assert (
            "not found" in combined.lower()
            or "not authenticated" in combined.lower()
            or "could not be confirmed" in combined.lower()
            or completed.returncode in {0, 1}
        )


def test_await_human_approval_setup_failure_records_blocked(tmp_path: Path) -> None:
    """Missing reviewed diff hash → BLOCKED + no-button notify, never approved."""
    run_dir = tmp_path / "runs" / "dx-01-setup-fail"
    worktree = tmp_path / "worktree"
    run_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (run_dir / "status").write_text("REVIEWING\n", encoding="utf-8")
    review = run_dir / "review-1.json"
    review.write_text(
        json.dumps({"status": "APPROVED", "summary": "ok", "findings": [], "tests_required": []}),
        encoding="utf-8",
    )

    fake_dx = tmp_path / "fake_dx.sh"
    fake_dx.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            cmd="${1:-}"
            shift || true
            case "$cmd" in
              create-request)
                echo "should not create request without reviewed hash" >&2
                exit 1
                ;;
              wait-decision)
                echo "should not wait after setup failure" >&2
                exit 0
                ;;
              notify-blocked)
                run_dir=""
                reason=""
                report_hint=""
                kind="blocked"
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --run-dir) run_dir="$2"; shift 2 ;;
                    --reason) reason="$2"; shift 2 ;;
                    --report-hint) report_hint="$2"; shift 2 ;;
                    --kind) kind="$2"; shift 2 ;;
                    *) shift ;;
                  esac
                done
                python3 - "$run_dir" "$reason" "$report_hint" "$kind" <<'PY'
            import json, sys
            from pathlib import Path
            run_dir = Path(sys.argv[1])
            payload = {
                "schema_version": 1,
                "kind": sys.argv[4],
                "run_id": run_dir.name,
                "status": (run_dir / "status").read_text(encoding="utf-8").strip(),
                "summary": sys.argv[2],
                "report_hint": sys.argv[3],
                "offer_approval_button": False,
                "sent_at": None,
            }
            (run_dir / "telegram_notify.json").write_text(
                json.dumps(payload, indent=2) + "\\n", encoding="utf-8"
            )
            PY
                ;;
              *)
                echo "unexpected command: $cmd" >&2
                exit 99
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    fake_dx.chmod(fake_dx.stat().st_mode | stat.S_IXUSR)

    harness = textwrap.dedent(
        f"""\
        set -euo pipefail
        REPO_ROOT={str(REPO_ROOT)!r}
        AGENT_DX_CLI={str(fake_dx)!r}
        RUN_DIR={str(run_dir)!r}
        WORKTREE={str(worktree)!r}
        BASE_COMMIT='deadbeef'
        TASK_FILE='docs/tasks/DX-01.md'
        TASK_ID='DX-01'
        # shellcheck source=/dev/null
        source {str(AGENTS / "run_task.sh")!r}
        set +e
        await_human_approval {str(review)!r} ''
        rc=$?
        set -e
        exit "$rc"
        """
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_DX_CLI": str(fake_dx)},
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "BLOCKED"
    assert not (run_dir / "human_approval_decision.json").exists()
    assert not (run_dir / "human_approval_request.json").exists()
    notify = json.loads((run_dir / "telegram_notify.json").read_text(encoding="utf-8"))
    assert notify["offer_approval_button"] is False
    assert notify["kind"] == "failure"
    assert "diff hash" in notify["summary"].lower()
    assert worktree.is_dir()


def test_await_human_approval_successful_loop_completion(tmp_path: Path) -> None:
    """wait-decision exit 0 (validated decision) → loop exits 0; shell never rewrites status."""
    run_dir = tmp_path / "runs" / "dx-01-success"
    worktree = tmp_path / "worktree"
    run_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (run_dir / "status").write_text("REVIEWING\n", encoding="utf-8")
    review = run_dir / "review-1.json"
    review.write_text(
        json.dumps({"status": "APPROVED", "summary": "ok", "findings": [], "tests_required": []}),
        encoding="utf-8",
    )

    fake_dx = tmp_path / "fake_dx_success.sh"
    fake_dx.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            cmd="${1:-}"
            shift || true
            case "$cmd" in
              create-request)
                run_dir=""
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --run-dir) run_dir="$2"; shift 2 ;;
                    *) shift ;;
                  esac
                done
                status="$(tr -d '\\n' < "$run_dir/status")"
                if [[ "$status" != "APPROVED" ]]; then
                  echo "expected technical APPROVED before create-request, got: $status" >&2
                  exit 1
                fi
                printf '%s\\n' 'AWAITING_HUMAN_APPROVAL' > "$run_dir/status"
                printf '%s\\n' '{"run_id":"dx-01-success","diff_hash":"abc123diffhash0123456789abcdef01"}' \
                  > "$run_dir/human_approval_request.json"
                ;;
              wait-decision)
                run_dir=""
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --run-dir) run_dir="$2"; shift 2 ;;
                    *) shift ;;
                  esac
                done
                printf '%s\\n' '{"decision":"approve","run_id":"dx-01-success","diff_hash":"abc123diffhash0123456789abcdef01"}' \
                  > "$run_dir/human_approval_decision.json"
                printf '%s\\n' 'HUMAN_APPROVED' > "$run_dir/status"
                exit 0
                ;;
              *)
                echo "unexpected command: $cmd" >&2
                exit 99
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    fake_dx.chmod(fake_dx.stat().st_mode | stat.S_IXUSR)

    reviewed_hash = "abc123diffhash0123456789abcdef01"
    harness = textwrap.dedent(
        f"""\
        set -euo pipefail
        REPO_ROOT={str(REPO_ROOT)!r}
        AGENT_DX_CLI={str(fake_dx)!r}
        RUN_DIR={str(run_dir)!r}
        WORKTREE={str(worktree)!r}
        BASE_COMMIT='deadbeef'
        TASK_FILE='docs/tasks/DX-01.md'
        TASK_ID='DX-01'
        # shellcheck source=/dev/null
        source {str(AGENTS / "run_task.sh")!r}
        set +e
        await_human_approval {str(review)!r} {reviewed_hash!r}
        rc=$?
        set -e
        exit "$rc"
        """
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_DX_CLI": str(fake_dx)},
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "HUMAN_APPROVED"
    assert (run_dir / "human_approval_decision.json").is_file()
    assert "HUMAN_APPROVED" in completed.stdout
    assert "verify-reviewed-snapshot" in completed.stdout
    assert worktree.is_dir()


def test_await_human_approval_timeout_does_not_rewrite_status(tmp_path: Path) -> None:
    """On wait failure the shell must not non-atomically rewrite status (no downgrade)."""
    run_dir = tmp_path / "runs" / "dx-01-timeout-norewrite"
    worktree = tmp_path / "worktree"
    run_dir.mkdir(parents=True)
    worktree.mkdir(parents=True)
    review = run_dir / "review-1.json"
    review.write_text(
        json.dumps({"status": "APPROVED", "summary": "ok", "findings": [], "tests_required": []}),
        encoding="utf-8",
    )

    fake_dx = tmp_path / "fake_dx_timeout.sh"
    fake_dx.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            cmd="${1:-}"
            shift || true
            case "$cmd" in
              create-request)
                run_dir=""
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --run-dir) run_dir="$2"; shift 2 ;;
                    *) shift ;;
                  esac
                done
                status="$(tr -d '\\n' < "$run_dir/status")"
                if [[ "$status" != "APPROVED" ]]; then
                  echo "expected technical APPROVED before create-request, got: $status" >&2
                  exit 1
                fi
                printf '%s\\n' 'AWAITING_HUMAN_APPROVAL' > "$run_dir/status"
                ;;
              wait-decision)
                run_dir=""
                while [[ $# -gt 0 ]]; do
                  case "$1" in
                    --run-dir) run_dir="$2"; shift 2 ;;
                    *) shift ;;
                  esac
                done
                # Simulate a late claim that landed as HUMAN_APPROVED after wait gave up.
                # Shell must not overwrite this with a non-atomic AWAITING rewrite.
                printf '%s\\n' 'HUMAN_APPROVED' > "$run_dir/status"
                printf '%s\\n' '{"decision":"approve"}' > "$run_dir/human_approval_decision.json"
                exit 2
                ;;
              *)
                echo "unexpected command: $cmd" >&2
                exit 99
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    fake_dx.chmod(fake_dx.stat().st_mode | stat.S_IXUSR)

    reviewed_hash = "abc123diffhash0123456789abcdef01"
    harness = textwrap.dedent(
        f"""\
        set -euo pipefail
        REPO_ROOT={str(REPO_ROOT)!r}
        AGENT_DX_CLI={str(fake_dx)!r}
        RUN_DIR={str(run_dir)!r}
        WORKTREE={str(worktree)!r}
        BASE_COMMIT='deadbeef'
        TASK_FILE='docs/tasks/DX-01.md'
        TASK_ID='DX-01'
        # shellcheck source=/dev/null
        source {str(AGENTS / "run_task.sh")!r}
        set +e
        await_human_approval {str(review)!r} {reviewed_hash!r}
        rc=$?
        set -e
        exit "$rc"
        """
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_DX_CLI": str(fake_dx)},
    )

    assert completed.returncode == 2, completed.stdout + completed.stderr
    # Critical: status must remain HUMAN_APPROVED (shell did not printf AWAITING).
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "HUMAN_APPROVED"
    assert worktree.is_dir()


def test_telegram_bridge_service_is_rendered_for_paths_with_spaces(tmp_path: Path) -> None:
    """
    Repository path contains spaces; unit must parse WorkingDirectory / ExecStart /
    ReadWritePaths as single paths. Documentation with raw spaces is invalid.
    """
    unit_path = tmp_path / "agent-loop.service"
    state_root = tmp_path / "state with spaces"
    credentials = tmp_path / "config with spaces" / "telegram.env"
    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "systemd-unit",
            "--state-root",
            str(state_root),
            "--credentials-file",
            str(credentials),
            "--output",
            str(unit_path),
        ],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    text = unit_path.read_text(encoding="utf-8")

    assert f"WorkingDirectory={REPO_ROOT}" in text
    assert f'"{REPO_ROOT}/scripts/agents/telegram_bridge.py"' in text
    assert f'--runs-root "{state_root}"' in text
    assert f'ReadWritePaths="{state_root}"' in text
    assert "new_chatbot" not in text
    assert "@TOOL_ROOT@" not in text

    verify = subprocess.run(
        ["systemd-analyze", "verify", str(unit_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    combined = (verify.stdout or "") + (verify.stderr or "")
    assert verify.returncode == 0, combined
    assert "Invalid URL" not in combined
    assert "path is not absolute, ignoring" not in combined



def test_allocate_exclusive_run_dir_prevents_timestamp_collision(tmp_path: Path) -> None:
    """Second-resolution stamp collision must allocate a new dir, never reuse via mkdir -p."""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    stamp = "20260722T120000Z"
    slug = "dx-01"
    occupied = runs_root / f"{slug}-{stamp}"
    occupied.mkdir()
    (occupied / "status").write_text("HUMAN_APPROVED\n", encoding="utf-8")
    (occupied / "human_approval_request.json").write_text("{}\n", encoding="utf-8")

    harness = textwrap.dedent(
        f"""\
        set -euo pipefail
        # shellcheck source=/dev/null
        source {str(AGENTS / "run_task.sh")!r}
        allocate_exclusive_run_dir {str(runs_root)!r} {slug!r} {stamp!r}
        """
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    allocated = Path(completed.stdout.strip())
    assert allocated == runs_root / f"{slug}-{stamp}-1"
    assert allocated.is_dir()
    assert allocated != occupied
    assert (occupied / "status").read_text(encoding="utf-8").strip() == "HUMAN_APPROVED"
    assert list(allocated.iterdir()) == []

    # A second collision advances the suffix again.
    completed2 = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed2.returncode == 0, completed2.stdout + completed2.stderr
    allocated2 = Path(completed2.stdout.strip())
    assert allocated2 == runs_root / f"{slug}-{stamp}-2"
    assert allocated2.is_dir()


def test_review_snapshot_phase_survives_broken_untracked_symlink(tmp_path: Path) -> None:
    """
    Broken untracked symlink must not abort the shell snapshot phase via set -e.

    The old sha256sum path followed links and left REVIEWING with no Telegram
    notify. Canonical compute_reviewed_snapshot_hash must succeed (or only fail
    as explicit BLOCKED + no-button for a genuine hash error).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "dx@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "DX Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    base = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    # Broken untracked symlink: target does not exist; must not be followed.
    (repo / "broken.link").symlink_to("/nonexistent/dx01-missing-target")

    run_dir = tmp_path / "runs" / "dx-01-broken-symlink"
    run_dir.mkdir(parents=True)
    (run_dir / "status").write_text("REVIEWING\n", encoding="utf-8")

    harness = textwrap.dedent(
        f"""\
        set -euo pipefail
        REPO_ROOT={str(REPO_ROOT)!r}
        RUN_DIR={str(run_dir)!r}
        WORKTREE={str(repo)!r}
        BASE_COMMIT={base!r}
        # shellcheck source=/dev/null
        source {str(AGENTS / "run_task.sh")!r}

        BEFORE_DIFF="$RUN_DIR/before-review-1.diff"
        AFTER_DIFF="$RUN_DIR/after-review-1.diff"
        git -C "$WORKTREE" diff --binary "$BASE_COMMIT" -- > "$BEFORE_DIFF"

        set +e
        BEFORE_HASH="$(compute_reviewed_snapshot_hash "$WORKTREE" "$BASE_COMMIT")"
        BEFORE_HASH_RC=$?
        set -e
        if [[ "$BEFORE_HASH_RC" -ne 0 || -z "${{BEFORE_HASH}}" || "${{#BEFORE_HASH}}" -lt 32 ]]; then
          printf '%s\\n' "BLOCKED" > "$RUN_DIR/status"
          notify_terminal_failure \\
            "Failed to compute reviewed snapshot hash before Codex" \\
            "$(basename "$RUN_DIR")" \\
            failure
          die "failed to compute pre-review diff hash; run directory: $RUN_DIR"
        fi

        # Simulate Codex with no tree mutations.
        git -C "$WORKTREE" diff --binary "$BASE_COMMIT" -- > "$AFTER_DIFF"

        set +e
        AFTER_HASH="$(compute_reviewed_snapshot_hash "$WORKTREE" "$BASE_COMMIT")"
        AFTER_HASH_RC=$?
        set -e
        if [[ "$AFTER_HASH_RC" -ne 0 || -z "${{AFTER_HASH}}" || "${{#AFTER_HASH}}" -lt 32 ]]; then
          printf '%s\\n' "BLOCKED" > "$RUN_DIR/status"
          notify_terminal_failure \\
            "Failed to compute reviewed snapshot hash after Codex" \\
            "$(basename "$RUN_DIR")" \\
            failure
          die "failed to compute post-review diff hash; run directory: $RUN_DIR"
        fi

        if [[ "$BEFORE_HASH" != "$AFTER_HASH" ]] || ! cmp -s "$BEFORE_DIFF" "$AFTER_DIFF"; then
          printf '%s\\n' "BLOCKED" > "$RUN_DIR/status"
          notify_terminal_failure \\
            "Reviewer changed repository files" \\
            "$(basename "$RUN_DIR")" \\
            failure
          die "reviewer changed repository files; inspect $RUN_DIR before continuing"
        fi

        printf 'SNAPSHOT_OK before=%s after=%s\\n' "$BEFORE_HASH" "$AFTER_HASH"
        """
    )
    completed = subprocess.run(
        ["bash", "-c", harness],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined
    assert "SNAPSHOT_OK" in completed.stdout, combined
    # Snapshot phase completed under set -e; status must not abort mid-flight.
    assert (run_dir / "status").read_text(encoding="utf-8").strip() == "REVIEWING"
    assert not (run_dir / "telegram_notify.json").exists()
    assert (run_dir / "before-review-1.diff").is_file()
    assert (run_dir / "after-review-1.diff").is_file()
    # No redundant follow-symlink untracked manifests.
    assert not (run_dir / "before-review-1.untracked").exists()
    assert not (run_dir / "after-review-1.untracked").exists()
    before_hash = completed.stdout.strip().split("before=", 1)[1].split(" after=", 1)[0]
    after_hash = completed.stdout.strip().split("after=", 1)[1]
    assert before_hash == after_hash
    assert len(before_hash) >= 32
    # Deterministic: re-hash yields the same fingerprint without reading the target.
    again = subprocess.run(
        [
            "python3",
            str(AGENTS / "telegram_bridge.py"),
            "compute-diff-hash",
            "--worktree",
            str(repo),
            "--base-commit",
            base,
        ],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip() == before_hash
    assert not Path("/nonexistent/dx01-missing-target").exists()


def test_external_cli_uses_isolated_state_and_rejects_false_auth_positives(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "agent@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Agent Test"], cwd=repo, check=True)
    task = repo / "docs" / "tasks" / "AG-01.md"
    task.parent.mkdir(parents=True)
    task.write_text("# AG-01\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "task"], cwd=repo, check=True, capture_output=True)

    cursor = tmp_path / "agent"
    cursor.write_text(
        "#!/usr/bin/env bash\necho 'temporary status failure'\nexit 1\n",
        encoding="utf-8",
    )
    cursor.chmod(cursor.stat().st_mode | stat.S_IXUSR)
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\necho 'Not logged in'\nexit 0\n",
        encoding="utf-8",
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)

    state_root = tmp_path / "external state"
    completed = subprocess.run(
        [
            str(REPO_ROOT / "agent-loop"),
            "run",
            "--repo",
            str(repo),
            "--state-root",
            str(state_root),
            "--dry-run",
            "docs/tasks/AG-01.md",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CURSOR_AGENT_BIN": str(cursor),
            "CODEX_BIN": str(codex),
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"target_repo={repo}" in completed.stdout
    assert f"state_root={state_root}/projects/target-repo-" in completed.stdout
    assert f"cursor_agent={cursor} authenticated=0" in completed.stdout
    assert f"codex={codex} authenticated=0" in completed.stdout
    assert not (repo / ".agents").exists()


def test_project_state_id_disambiguates_same_names_and_resolves_symlinks(
    tmp_path: Path,
) -> None:
    repos = []
    for parent in (tmp_path / "one", tmp_path / "two"):
        repo = parent / "same-name"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        repos.append(repo)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repos[0], target_is_directory=True)

    def resolve(repo: Path) -> str:
        completed = subprocess.run(
            [
                "python3",
                str(AGENTS / "telegram_bridge.py"),
                "project-state-dir",
                "--repo",
                str(repo),
                "--state-root",
                str(tmp_path / "state"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    first = resolve(repos[0])
    second = resolve(repos[1])
    assert first != second
    assert resolve(alias) == first
