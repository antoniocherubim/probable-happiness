"""Unit tests for the local Telegram bridge (fake Bot API; no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from dx.approval import (  # noqa: E402
    STATUS_APPROVED,
    STATUS_AWAITING,
    STATUS_HUMAN_APPROVED,
    create_approval_request,
    enqueue_notification,
    load_decision,
    read_status,
    write_status,
)
from dx.bridge import Bridge  # noqa: E402
from dx.config import BridgeConfig, ConfigError, load_bridge_config  # noqa: E402
from dx.telegram import FakeTelegramAPI, TelegramClient  # noqa: E402


ALLOWED_USER = 1001
ALLOWED_CHAT = 1001
OTHER_USER = 2002
TOKEN = "123456:TEST-TOKEN-NOT-REAL"


@pytest.fixture
def git_worktree(tmp_path: Path) -> tuple[Path, str]:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True)

    git("init")
    git("config", "user.email", "dx@example.com")
    git("config", "user.name", "DX Test")
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD").strip()
    (repo / "f.txt").write_text("two\n", encoding="utf-8")
    return repo, base


@pytest.fixture
def bridge_env(tmp_path: Path, git_worktree: tuple[Path, str]) -> dict:
    worktree, base = git_worktree
    runs_root = tmp_path / "runs"
    run_dir = runs_root / "dx-01-bridge"
    run_dir.mkdir(parents=True)
    write_status(run_dir, STATUS_APPROVED)
    request = create_approval_request(
        run_dir=run_dir,
        task="docs/tasks/DX-01.md",
        base_commit=base,
        worktree=worktree,
        review_report="review-1.json",
    )
    enqueue_notification(
        run_dir=run_dir,
        kind="awaiting_human_approval",
        summary="awaiting",
        report_hint="review-1.json",
    )
    fake = FakeTelegramAPI(allowed_token=TOKEN)
    config = BridgeConfig(
        bot_token=TOKEN,
        allowed_user_id=ALLOWED_USER,
        allowed_chat_id=ALLOWED_CHAT,
        poll_timeout_sec=1,
    )
    client = TelegramClient(TOKEN, api_base="http://telegram.test", transport=fake.as_transport())
    bridge = Bridge(config, client, runs_root)
    return {
        "bridge": bridge,
        "fake": fake,
        "run_dir": run_dir,
        "request": request,
        "worktree": worktree,
        "base": base,
        "runs_root": runs_root,
    }


def test_config_rejects_non_numeric_identity(tmp_path: Path) -> None:
    cred = tmp_path / "creds.env"
    cred.write_text(
        "AGENT_TELEGRAM_BOT_TOKEN=x\n"
        "AGENT_TELEGRAM_ALLOWED_USER_ID=@someone\n"
        "AGENT_TELEGRAM_ALLOWED_CHAT_ID=1\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_bridge_config(
            {
                "AGENT_TELEGRAM_CREDENTIALS_FILE": str(cred),
            }
        )


def test_config_redacts_token(tmp_path: Path) -> None:
    cred = tmp_path / "creds.env"
    cred.write_text(
        "AGENT_TELEGRAM_BOT_TOKEN=abcdefghijklmnop\n"
        "AGENT_TELEGRAM_ALLOWED_USER_ID=1\n"
        "AGENT_TELEGRAM_ALLOWED_CHAT_ID=1\n",
        encoding="utf-8",
    )
    cfg = load_bridge_config({"AGENT_TELEGRAM_CREDENTIALS_FILE": str(cred)})
    redacted = cfg.redacted()
    assert "abcdefghijklmnop" not in json.dumps(redacted)
    assert redacted["allowed_user_id"] == 1


def test_authorized_callback_approves(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    request = bridge_env["request"]
    run_dir: Path = bridge_env["run_dir"]

    assert bridge.process_outbox_once() == 1
    assert fake.sent_messages
    markup = fake.sent_messages[0]["reply_markup"]
    assert markup["inline_keyboard"][0][0]["callback_data"] == request["callback_token"]

    fake.push_callback(
        user_id=ALLOWED_USER,
        chat_id=ALLOWED_CHAT,
        data=request["callback_token"],
        callback_query_id="cb-ok",
    )
    assert bridge.process_updates_once() == 1
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir)["diff_hash"] == request["diff_hash"]


def test_unauthorized_sender_cannot_approve(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    request = bridge_env["request"]
    run_dir: Path = bridge_env["run_dir"]

    fake.push_callback(
        user_id=OTHER_USER,
        chat_id=OTHER_USER,
        data=request["callback_token"],
        callback_query_id="cb-bad",
    )
    bridge.process_updates_once()
    assert read_status(run_dir) == STATUS_AWAITING
    assert load_decision(run_dir) is None
    # Neutral ack — no run paths in callback answers for strangers.
    assert fake.answered_callbacks
    assert "dx-01" not in json.dumps(fake.answered_callbacks).lower()
    assert str(run_dir) not in json.dumps(fake.answered_callbacks)


def test_unauthorized_message_is_neutral(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    run_dir: Path = bridge_env["run_dir"]
    fake.push_message(user_id=OTHER_USER, chat_id=OTHER_USER, text="/start")
    bridge.process_updates_once()
    assert fake.sent_messages
    body = fake.sent_messages[-1]["text"]
    assert body == "OK."
    assert "agents" not in body
    assert run_dir.name not in body


def test_replay_and_foreign_callback(bridge_env: dict, tmp_path: Path) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    request = bridge_env["request"]
    run_dir: Path = bridge_env["run_dir"]
    worktree: Path = bridge_env["worktree"]
    base: str = bridge_env["base"]

    fake.push_callback(
        user_id=ALLOWED_USER,
        chat_id=ALLOWED_CHAT,
        data=request["callback_token"],
        callback_query_id="cb-1",
    )
    bridge.process_updates_once()
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED

    # Replay same callback — idempotent, state unchanged.
    decision = load_decision(run_dir)
    fake.push_callback(
        user_id=ALLOWED_USER,
        chat_id=ALLOWED_CHAT,
        data=request["callback_token"],
        callback_query_id="cb-1-replay",
    )
    bridge.process_updates_once()
    assert load_decision(run_dir) == decision

    # Other run token must not alter this run.
    other = bridge_env["runs_root"] / "other-run"
    other.mkdir()
    write_status(other, STATUS_APPROVED)
    other_req = create_approval_request(
        run_dir=other,
        task="docs/tasks/OTHER.md",
        base_commit=base,
        worktree=worktree,
        review_report="other.json",
    )
    fake.push_callback(
        user_id=ALLOWED_USER,
        chat_id=ALLOWED_CHAT,
        data=other_req["callback_token"],
        callback_query_id="cb-other",
    )
    bridge.process_updates_once()
    assert load_decision(run_dir) == decision
    assert read_status(other) == STATUS_HUMAN_APPROVED


def test_worktree_drift_still_approves_reviewed_hash_via_bridge(bridge_env: dict) -> None:
    """Callback approves the immutable reviewed hash; live drift is a planner verify concern."""
    from dx.approval import verify_reviewed_snapshot

    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    request = bridge_env["request"]
    run_dir: Path = bridge_env["run_dir"]
    worktree: Path = bridge_env["worktree"]
    (worktree / "f.txt").write_text("tampered\n", encoding="utf-8")
    fake.push_callback(
        user_id=ALLOWED_USER,
        chat_id=ALLOWED_CHAT,
        data=request["callback_token"],
        callback_query_id="cb-hash",
    )
    bridge.process_updates_once()
    assert read_status(run_dir) == STATUS_HUMAN_APPROVED
    assert load_decision(run_dir)["diff_hash"] == request["diff_hash"]
    assert verify_reviewed_snapshot(run_dir)["matches"] is False


def test_api_failure_does_not_approve(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    run_dir: Path = bridge_env["run_dir"]
    fake.fail_methods.add("sendMessage")
    assert bridge.process_outbox_once() == 0
    notify = json.loads((run_dir / "telegram_notify.json").read_text(encoding="utf-8"))
    assert notify["sent_at"] is None
    assert read_status(run_dir) == STATUS_AWAITING
    assert load_decision(run_dir) is None


def test_api_timeout_does_not_approve(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    run_dir: Path = bridge_env["run_dir"]
    fake.timeout_methods.add("getUpdates")
    assert bridge.process_updates_once() == 0
    assert read_status(run_dir) == STATUS_AWAITING


def test_blocked_notification_without_button(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    runs_root: Path = bridge_env["runs_root"]
    blocked = runs_root / "blocked-run"
    blocked.mkdir()
    (blocked / "status").write_text("BLOCKED\n", encoding="utf-8")
    enqueue_notification(
        run_dir=blocked,
        kind="blocked",
        summary="reviewer blocked",
        report_hint="review-2.json",
    )
    # Clear awaiting outbox first by marking sent without sending approval path noise
    awaiting = bridge_env["run_dir"] / "telegram_notify.json"
    payload = json.loads(awaiting.read_text(encoding="utf-8"))
    payload["sent_at"] = "already"
    awaiting.write_text(json.dumps(payload), encoding="utf-8")

    assert bridge.process_outbox_once() == 1
    assert len(fake.sent_messages) == 1
    assert "reply_markup" not in fake.sent_messages[0]
    assert "BLOCKED" in fake.sent_messages[0]["text"] or "blocked" in fake.sent_messages[0]["text"]
