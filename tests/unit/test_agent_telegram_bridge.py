"""Unit tests for the local Telegram bridge (fake Bot API; no network)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

AGENTS_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agents"
sys.path.insert(0, str(AGENTS_DIR))

from dx.approval import (  # noqa: E402
    STATUS_APPROVED,
    enqueue_notification,
    read_status,
)
from dx.bridge import (  # noqa: E402
    BRIDGE_ALREADY_RUNNING_EXIT,
    Bridge,
    BridgeAlreadyRunning,
    bridge_instance_lock,
)
from dx.cli import cmd_serve  # noqa: E402
from dx.config import BridgeConfig, ConfigError, load_bridge_config  # noqa: E402
from dx.state_machine import RunEvent, transition_run  # noqa: E402
from dx.telegram import (  # noqa: E402
    FakeTelegramAPI,
    TelegramClient,
)


ALLOWED_USER = 1001
ALLOWED_CHAT = 1001
OTHER_USER = 2002
TOKEN = "123456:TEST-TOKEN-NOT-REAL"


def mark_review_approved(run_dir: Path) -> None:
    transition_run(run_dir, RunEvent.RUN_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_STARTED)
    transition_run(run_dir, RunEvent.REVIEW_APPROVED)


@pytest.mark.parametrize(
    ("api_base", "expected"),
    [
        (
            "https://api.telegram.org",
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        ),
        (
            "https://telegram-proxy.test/api/",
            f"https://telegram-proxy.test/api/bot{TOKEN}/sendMessage",
        ),
    ],
)
def test_api_url_keeps_base_when_bot_token_contains_colon(
    api_base: str,
    expected: str,
) -> None:
    client = TelegramClient(TOKEN, api_base=api_base)

    assert client._url("sendMessage") == expected


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
    mark_review_approved(run_dir)
    enqueue_notification(
        run_dir=run_dir,
        kind="approved",
        summary="technical review approved; integration remains manual",
        report_hint="review-1.json",
    )
    fake = FakeTelegramAPI(allowed_token=TOKEN)
    config = BridgeConfig(
        bot_token=TOKEN,
        allowed_chat_id=ALLOWED_CHAT,
    )
    client = TelegramClient(TOKEN, api_base="http://telegram.test", transport=fake.as_transport())
    bridge = Bridge(config, client, runs_root)
    return {
        "bridge": bridge,
        "fake": fake,
        "run_dir": run_dir,
        "worktree": worktree,
        "base": base,
        "runs_root": runs_root,
    }


def test_config_rejects_non_numeric_chat_id(tmp_path: Path) -> None:
    cred = tmp_path / "creds.env"
    cred.write_text(
        "AGENT_TELEGRAM_BOT_TOKEN=x\n"
        "AGENT_TELEGRAM_ALLOWED_CHAT_ID=@channel\n",
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
        "AGENT_TELEGRAM_ALLOWED_CHAT_ID=1\n",
        encoding="utf-8",
    )
    cfg = load_bridge_config({"AGENT_TELEGRAM_CREDENTIALS_FILE": str(cred)})
    redacted = cfg.redacted()
    assert "abcdefghijklmnop" not in json.dumps(redacted)
    assert redacted["allowed_chat_id"] == 1


def test_only_one_bridge_per_bot_even_for_different_state_roots(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    environ = {"XDG_RUNTIME_DIR": str(runtime_dir)}
    first = BridgeConfig(
        bot_token=TOKEN,
        allowed_chat_id=ALLOWED_CHAT,
    )
    second = BridgeConfig(
        bot_token=TOKEN,
        allowed_chat_id=ALLOWED_CHAT,
        runs_root=tmp_path / "some-other-state-root",
    )

    with bridge_instance_lock(first, environ=environ) as lock_path:
        assert lock_path.is_file()
        assert f"pid={os.getpid()}" in lock_path.read_text(encoding="ascii")
        with pytest.raises(BridgeAlreadyRunning, match="already using"):
            with bridge_instance_lock(second, environ=environ):
                pytest.fail("a duplicate bridge acquired the bot lock")

    with bridge_instance_lock(second, environ=environ):
        pass


def test_different_bots_can_use_the_same_runtime_directory(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    environ = {"XDG_RUNTIME_DIR": str(runtime_dir)}
    first = BridgeConfig(TOKEN, ALLOWED_CHAT)
    second = BridgeConfig("987654:OTHER-BOT", ALLOWED_CHAT)

    with bridge_instance_lock(first, environ=environ):
        with bridge_instance_lock(second, environ=environ):
            pass


def test_serve_refuses_duplicate_before_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("AGENT_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("AGENT_TELEGRAM_ALLOWED_CHAT_ID", str(ALLOWED_CHAT))
    config = BridgeConfig(TOKEN, ALLOWED_CHAT)

    with bridge_instance_lock(config):
        result = cmd_serve(
            SimpleNamespace(runs_root=str(tmp_path / "other-state"), max_cycles=1)
        )

    assert result == BRIDGE_ALREADY_RUNNING_EXIT
    assert "already using this Telegram bot" in capsys.readouterr().err


def test_terminal_notification_has_no_buttons_or_human_state(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    run_dir: Path = bridge_env["run_dir"]

    assert bridge.process_outbox_once() == 1
    assert len(fake.sent_messages) == 1
    assert "reply_markup" not in fake.sent_messages[0]
    assert read_status(run_dir) == STATUS_APPROVED
    assert not (run_dir / "human_approval_request.json").exists()


def test_bridge_run_forever_delivers_outbox_without_inbound_api(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]

    bridge.run_forever(max_cycles=1)

    assert len(fake.sent_messages) == 1


def test_api_failure_keeps_terminal_notification_pending(bridge_env: dict) -> None:
    bridge: Bridge = bridge_env["bridge"]
    fake: FakeTelegramAPI = bridge_env["fake"]
    run_dir: Path = bridge_env["run_dir"]
    fake.fail_methods.add("sendMessage")
    assert bridge.process_outbox_once() == 0
    notify = json.loads((run_dir / "telegram_notify.json").read_text(encoding="utf-8"))
    assert notify["sent_at"] is None
    assert read_status(run_dir) == STATUS_APPROVED


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
