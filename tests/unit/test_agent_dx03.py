"""DX-03 Telegram summary, documentation policy, and approved branch delivery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

AGENTS = Path(__file__).resolve().parents[2] / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.approval import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_BLOCKED,
    STATUS_DELIVERY_FAILED,
    STATUS_HUMAN_APPROVED,
    STATUS_PUSHED,
    apply_human_approval,
    apply_human_rejection,
    create_approval_request,
    enqueue_notification,
    read_status,
    write_status,
)
from dx.bridge import Bridge  # noqa: E402
from dx.config import BridgeConfig  # noqa: E402
from dx.delivery import DeliveryError, deliver_run, freeze_delivery_config  # noqa: E402
from dx.profile import ProfileError, load_project_profile  # noqa: E402
from dx.runstate import plan_resume, write_run_metadata  # noqa: E402
from dx.snapshot import (  # noqa: E402
    SnapshotError,
    build_snapshot_manifest,
    format_technical_summary,
    prepare_review_artifacts,
    split_telegram_message,
    validate_documentation,
)
from dx.telegram import FakeTelegramAPI, TelegramClient  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


PROFILE = """
schema_version = 1

[validation]
commands = [["python3", "-m", "pytest", "-q"]]

[documentation]
required = true
required_paths = ["docs/release/{task_id}.md"]

[delivery]
mode = "push_branch"
remote = "origin"
base_branch = "main"
branch_template = "{task_slug}"
commit_message_template = "{task_id}: {task_title}"
push_after_human_approval = true
"""


def make_delivery_run(
    tmp_path: Path,
    *,
    approve: bool = True,
    prepare: bool = True,
) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "init", "--initial-branch=main")
    git(repo, "config", "user.email", "dx03@example.com")
    git(repo, "config", "user.name", "DX-03 Test")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / ".gitignore").write_text(".agent-op/\n.venv/\n", encoding="utf-8")
    (repo / ".agent-loop").mkdir()
    (repo / ".agent-loop" / "project.toml").write_text(
        textwrap.dedent(PROFILE),
        encoding="utf-8",
    )
    task = repo / "docs" / "tasks" / "CP-00.md"
    task.parent.mkdir(parents=True)
    task.write_text("# CP-00 — Entrega segura\n", encoding="utf-8")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "origin", "main:main")

    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "app.txt").write_text("base\nfeature\n", encoding="utf-8")
    documentation = worktree / "docs" / "release" / "CP-00.md"
    documentation.parent.mkdir(parents=True)
    documentation.write_text(
        "Behavior: feature\nTests: pytest\nResidual risks: none\n",
        encoding="utf-8",
    )

    run_dir = tmp_path / "state" / "runs" / "cp-00-run"
    run_dir.mkdir(parents=True)
    profile = load_project_profile(worktree)
    frozen = freeze_delivery_config(
        repo=repo.resolve(),
        worktree=worktree.resolve(),
        base_commit=base,
        task_file="docs/tasks/CP-00.md",
        task_id="CP-00",
        task_slug="cp-00",
        profile=profile,
    )
    write_run_metadata(
        run_dir,
        {
            "repo": str(repo.resolve()),
            "task_file": "docs/tasks/CP-00.md",
            "base_commit": base,
            "worktree": str(worktree.resolve()),
            "max_iterations": 3,
            "env_file": None,
            "profile": profile.public_dict(),
            "delivery": frozen,
        },
    )
    (run_dir / "iteration").write_text("1\n", encoding="utf-8")
    executor = run_dir / "cursor-1.json"
    executor.write_text(
        json.dumps(
            {
                "summary": "Implemented feature; 2 passed, 0 failed.",
                "risks": ["No known residual risk"],
            }
        ),
        encoding="utf-8",
    )
    reviewer = run_dir / "review-1.json"
    reviewer.write_text(
        json.dumps(
            {
                "status": "APPROVED",
                "summary": "Implementation and documentation are accurate.",
                "findings": [],
                "tests_required": [],
            }
        ),
        encoding="utf-8",
    )
    reviewed_hash = build_snapshot_manifest(worktree, base)["snapshot_hash"]
    if prepare:
        prepare_review_artifacts(
            run_dir=run_dir,
            repo=repo,
            worktree=worktree,
            task_file="docs/tasks/CP-00.md",
            task_id="CP-00",
            task_slug="cp-00",
            base_commit=base,
            iteration=1,
            max_iterations=3,
            executor_report=executor,
            reviewer_report=reviewer,
            reviewed_hash=reviewed_hash,
            profile=profile,
        )
    write_status(run_dir, STATUS_APPROVED)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/CP-00.md",
        task_id="CP-00",
        base_commit=base,
        worktree=worktree,
        review_report=str(reviewer),
        diff_hash=reviewed_hash,
    )
    if approve:
        result, _decision = apply_human_approval(
            run_dir=run_dir,
            callback_token=request["callback_token"],
            telegram_user_id=7,
            telegram_chat_id=7,
            allowed_user_id=7,
            allowed_chat_id=7,
        )
        assert result == "accepted"
    return {
        "repo": repo,
        "remote": remote,
        "worktree": worktree,
        "run_dir": run_dir,
        "base": base,
        "profile": profile,
        "request": request,
    }


def remote_ref(remote: Path, branch: str) -> str | None:
    result = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", f"refs/heads/{branch}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() or None


def test_profile_documentation_and_delivery_schema_is_strict(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    profile = env["profile"]
    assert profile.documentation_required is True
    assert profile.documentation_paths == ("docs/release/{task_id}.md",)
    assert profile.delivery_mode == "push_branch"
    assert profile.delivery_branch_template == "{task_slug}"

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / ".agent-loop").mkdir()
    (bad / ".agent-loop" / "project.toml").write_text(
        "schema_version = 1\n[documentation]\n"
        'required = true\nrequired_paths = ["docs/{unknown}.md"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError, match="unknown placeholder"):
        load_project_profile(bad)


def test_executor_and_reviewer_prompts_include_documentation_contract() -> None:
    script = (AGENTS / "run_task.sh").read_text(encoding="utf-8")
    assert "update every document required by [documentation]" in script
    assert "behavior, test evidence, and residual risks" in script
    assert "Do not insert a commit hash or branch URL" in script
    assert "explicitly verify that every configured required documentation path" in script


@pytest.mark.parametrize(
    "path",
    ["../ROADMAP.md", "docs/{task_slug}/../outside.md", "/tmp/report.md"],
)
def test_documentation_path_traversal_is_rejected(tmp_path: Path, path: str) -> None:
    repo = tmp_path / "repo"
    (repo / ".agent-loop").mkdir(parents=True)
    (repo / ".agent-loop" / "project.toml").write_text(
        f'schema_version = 1\n[documentation]\nrequired = true\nrequired_paths = ["{path}"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ProfileError):
        load_project_profile(repo)


def test_required_documentation_created_or_updated_and_missing_blocks(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    manifest = build_snapshot_manifest(env["worktree"], env["base"])
    assert validate_documentation(
        env["profile"], manifest, task_id="CP-00", task_slug="cp-00"
    ) == ["docs/release/CP-00.md"]

    os.unlink(env["worktree"] / "docs" / "release" / "CP-00.md")
    with pytest.raises(SnapshotError, match="required documentation"):
        validate_documentation(
            env["profile"],
            build_snapshot_manifest(env["worktree"], env["base"]),
            task_id="CP-00",
            task_slug="cp-00",
        )


def test_ignored_operational_symlink_is_not_in_manifest_and_special_file_is_rejected(
    tmp_path: Path,
) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    ignored = env["worktree"] / ".agent-op"
    ignored.mkdir()
    (ignored / "current").symlink_to("/tmp/not-followed")
    manifest = build_snapshot_manifest(env["worktree"], env["base"])
    assert ".agent-op/current" not in {entry["path"] for entry in manifest["entries"]}

    fifo = env["worktree"] / "unsafe.fifo"
    os.mkfifo(fifo)
    try:
        with pytest.raises(SnapshotError, match="special file"):
            build_snapshot_manifest(env["worktree"], env["base"])
    finally:
        fifo.unlink()


def test_summary_is_plain_sanitized_complete_and_chunked() -> None:
    summary = {
        "task_id": "CP-00",
        "task_title": "<b>Unsafe</b>",
        "repository": "repo",
        "base_commit": "a" * 40,
        "reviewer_status": "APPROVED",
        "iteration": 2,
        "max_iterations": 3,
        "file_count": 2,
        "additions": 20,
        "deletions": 3,
        "test_counts": {"passed": 47, "skipped": 1, "failed": 0, "errors": 0},
        "validation_status": "passed",
        "reviewed_diff_hash": "b" * 64,
        "files": ["app.py", "docs/CP-00.md"],
        "executor_summary": "token=abc password:secret https://user:pw@example.test/x",
        "test_commands": ["pytest -q"],
        "reviewer_summary": "\n".join(["reviewed safely"] * 400),
        "findings": [
            {"severity": "low", "title": "One", "details": "detail"},
            {"severity": "medium", "title": "Two", "details": "detail"},
        ],
        "residual_risks": ["none"],
        "documentation": ["docs/CP-00.md"],
    }
    text = format_technical_summary(summary)
    chunks = split_telegram_message(text, limit=600)
    rendered = "\n".join(chunks)
    assert len(chunks) > 1
    assert all(chunk.startswith(f"({index}/{len(chunks)})") for index, chunk in enumerate(chunks, 1))
    assert "token=[REDACTED]" in rendered
    assert "password=[REDACTED]" in rendered
    assert "abc password:secret" not in rendered
    assert "https://user:pw@" not in rendered
    assert "Findings:" in rendered
    assert "Documentação:" in rendered


def test_multipart_retry_does_not_duplicate_completed_chunks(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    run_dir = env["run_dir"]
    messages = ["(1/3)\nfirst", "(2/3)\nsecond", "(3/3)\nlast"]
    enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="first",
        messages=messages,
    )
    fake = FakeTelegramAPI(allowed_token="123:fake", fail_send_after=1)
    client = TelegramClient(
        "123:fake",
        api_base="http://telegram.test",
        transport=fake.as_transport(),
    )
    bridge = Bridge(
        BridgeConfig(
            bot_token="123:fake",
            allowed_user_id=7,
            allowed_chat_id=7,
            poll_timeout_sec=1,
        ),
        client,
        run_dir.parent,
    )
    assert bridge.process_outbox_once() == 0
    payload = json.loads((run_dir / "telegram_notify.json").read_text(encoding="utf-8"))
    assert len(payload["sent_message_ids"]) == 1
    assert len(fake.sent_messages) == 1

    fake.fail_send_after = None
    assert bridge.process_outbox_once() == 1
    assert [item["text"] for item in fake.sent_messages] == messages
    assert "reply_markup" not in fake.sent_messages[0]
    assert "reply_markup" not in fake.sent_messages[1]
    buttons = fake.sent_messages[2]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in buttons] == [
        "Aprovar e publicar branch",
        "Rejeitar",
    ]
    assert all("parse_mode" not in item for item in fake.sent_messages)


def test_authenticated_callback_delivers_configured_branch(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    fake = FakeTelegramAPI(allowed_token="123:fake")
    client = TelegramClient(
        "123:fake",
        api_base="http://telegram.test",
        transport=fake.as_transport(),
    )
    bridge = Bridge(
        BridgeConfig(
            bot_token="123:fake",
            allowed_user_id=7,
            allowed_chat_id=7,
            poll_timeout_sec=1,
        ),
        client,
        env["run_dir"].parent,
    )
    fake.push_callback(
        user_id=7,
        chat_id=7,
        data=env["request"]["callback_token"],
    )
    assert bridge.process_updates_once() == 1
    assert read_status(env["run_dir"]) == STATUS_HUMAN_APPROVED
    assert (env["run_dir"] / "delivery-job.json").is_file()
    assert remote_ref(env["remote"], "cp-00") is None
    assert fake.answered_callbacks[-1]["text"] == "Aprovado; entrega enfileirada."
    from dx.delivery_job import process_delivery_run

    result = process_delivery_run(env["run_dir"])
    assert result["status"] == STATUS_PUSHED
    assert remote_ref(env["remote"], "cp-00") is not None


def test_delivery_creates_exact_single_commit_branch_without_changing_main(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path)
    result = deliver_run(env["run_dir"])
    assert result["status"] == STATUS_PUSHED
    assert result["branch"] == "cp-00"
    assert result["remote_oid"] == result["commit_oid"]
    assert remote_ref(env["remote"], "cp-00") == result["commit_oid"]
    assert remote_ref(env["remote"], "main") == env["base"]
    assert git(env["repo"], "rev-parse", f"{result['commit_oid']}^") == env["base"]
    assert git(env["repo"], "rev-parse", f"{result['commit_oid']}^{{tree}}") == result["tree_oid"]
    changed = set(
        git(
            env["repo"],
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            result["commit_oid"],
        ).splitlines()
    )
    assert changed == {"app.txt", "docs/release/CP-00.md"}
    assert read_status(env["run_dir"]) == STATUS_PUSHED


def test_remote_collision_blocks_and_resume_retries_only_delivery(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path)
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(env["remote"]),
            "update-ref",
            "refs/heads/cp-00",
            str(env["base"]),
        ],
        check=True,
    )
    with pytest.raises(DeliveryError, match="remote_branch_exists"):
        deliver_run(env["run_dir"])
    assert read_status(env["run_dir"]) == STATUS_DELIVERY_FAILED
    assert remote_ref(env["remote"], "cp-00") == env["base"]
    assert plan_resume(env["run_dir"])["resume_phase"] == "delivery"

    subprocess.run(
        [
            "git",
            "--git-dir",
            str(env["remote"]),
            "update-ref",
            "-d",
            "refs/heads/cp-00",
        ],
        check=True,
    )
    result = deliver_run(env["run_dir"])
    assert result["status"] == STATUS_PUSHED
    assert remote_ref(env["remote"], "cp-00") == result["commit_oid"]


def test_existing_identical_remote_commit_is_idempotent(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path)
    first = deliver_run(env["run_dir"])
    write_status(env["run_dir"], STATUS_DELIVERY_FAILED)
    document = json.loads((env["run_dir"] / "delivery.json").read_text(encoding="utf-8"))
    document["status"] = STATUS_DELIVERY_FAILED
    (env["run_dir"] / "delivery.json").write_text(json.dumps(document), encoding="utf-8")
    second = deliver_run(env["run_dir"])
    assert second["commit_oid"] == first["commit_oid"]
    assert second["push_result"] == "idempotent"


def test_new_file_after_approval_blocks_delivery(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path)
    (env["worktree"] / "late.txt").write_text("not reviewed\n", encoding="utf-8")
    with pytest.raises(DeliveryError, match="approved_snapshot_changed"):
        deliver_run(env["run_dir"])
    assert read_status(env["run_dir"]) == STATUS_DELIVERY_FAILED
    assert remote_ref(env["remote"], "cp-00") is None
    assert remote_ref(env["remote"], "main") == env["base"]


def test_remote_or_profile_change_after_run_start_blocks_delivery(tmp_path: Path) -> None:
    remote_case = make_delivery_run(tmp_path / "remote-case")
    replacement = tmp_path / "replacement.git"
    subprocess.run(["git", "init", "--bare", str(replacement)], check=True, capture_output=True)
    git(remote_case["repo"], "remote", "set-url", "--push", "origin", str(replacement))
    with pytest.raises(DeliveryError, match="delivery_remote_changed"):
        deliver_run(remote_case["run_dir"])
    assert read_status(remote_case["run_dir"]) == STATUS_DELIVERY_FAILED

    profile_case = make_delivery_run(tmp_path / "profile-case")
    profile_path = profile_case["worktree"] / ".agent-loop" / "project.toml"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            'branch_template = "{task_slug}"',
            'branch_template = "review/{task_slug}"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(DeliveryError, match="project profile changed"):
        deliver_run(profile_case["run_dir"])
    assert read_status(profile_case["run_dir"]) == STATUS_DELIVERY_FAILED


def test_missing_git_authorship_fails_preflight_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    subprocess.run(
        ["git", "-C", str(env["repo"]), "config", "--unset-all", "user.name"],
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(env["repo"]), "config", "--unset-all", "user.email"],
        check=False,
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    with pytest.raises(DeliveryError, match="identity is not configured"):
        freeze_delivery_config(
            repo=env["repo"],
            worktree=env["worktree"],
            base_commit=env["base"],
            task_file="docs/tasks/CP-00.md",
            task_id="CP-00",
            task_slug="cp-00",
            profile=env["profile"],
        )


def test_malicious_task_slug_is_refused_by_git_ref_validation(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    with pytest.raises(DeliveryError, match="git command failed: check-ref-format"):
        freeze_delivery_config(
            repo=env["repo"],
            worktree=env["worktree"],
            base_commit=env["base"],
            task_file="docs/tasks/CP-00.md",
            task_id="CP-00",
            task_slug="cp-00..lock",
            profile=env["profile"],
        )


def test_rejection_never_creates_branch(tmp_path: Path) -> None:
    env = make_delivery_run(tmp_path, approve=False)
    request = env["request"]
    result = apply_human_rejection(
        run_dir=env["run_dir"],
        callback_token=request["callback_token"],
        telegram_user_id=7,
        telegram_chat_id=7,
        allowed_user_id=7,
        allowed_chat_id=7,
    )
    assert result == "rejected"
    assert read_status(env["run_dir"]) == STATUS_BLOCKED
    assert remote_ref(env["remote"], "cp-00") is None
