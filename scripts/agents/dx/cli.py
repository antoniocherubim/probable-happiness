"""CLI entrypoints used by run_task.sh and the systemd user unit."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .approval import (
    ApprovalError,
    compute_diff_hash,
    create_approval_request,
    enqueue_notification,
    verify_reviewed_snapshot,
    wait_for_decision,
)
from .bridge import Bridge, build_awaiting_summary, build_blocked_summary
from .config import ConfigError, human_approval_timeout_sec, load_bridge_config
from .paths import (
    PathConfigError,
    default_state_root,
    project_state_dir,
    render_systemd_unit,
)
from .telegram import TelegramClient


def _repo_root_from_here() -> Path:
    # scripts/agents/dx/cli.py -> repo root
    return Path(__file__).resolve().parents[3]


def _default_runs_root() -> Path:
    return _repo_root_from_here() / ".agents" / "runs"


def cmd_project_state_dir(args: argparse.Namespace) -> int:
    try:
        path = project_state_dir(args.repo, args.state_root)
    except PathConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


def cmd_render_systemd_unit(args: argparse.Namespace) -> int:
    tool_root = Path(args.tool_root).expanduser().resolve() if args.tool_root else _repo_root_from_here()
    state_root = Path(args.state_root).expanduser() if args.state_root else default_state_root()
    credentials = (
        Path(args.credentials_file).expanduser()
        if args.credentials_file
        else Path.home() / ".config" / "codex-cursor-agent-loop" / "telegram.env"
    )
    template = tool_root / "scripts" / "agents" / "telegram-bridge.service.in"
    try:
        rendered = render_systemd_unit(
            template=template,
            tool_root=tool_root,
            state_root=state_root,
            credentials_file=credentials,
        )
    except (OSError, PathConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


def cmd_compute_diff_hash(args: argparse.Namespace) -> int:
    digest = compute_diff_hash(Path(args.worktree), args.base_commit)
    print(digest)
    return 0


def cmd_create_request(args: argparse.Namespace) -> int:
    try:
        request = create_approval_request(
            run_dir=Path(args.run_dir),
            task=args.task,
            base_commit=args.base_commit,
            worktree=Path(args.worktree),
            review_report=args.review_report,
            diff_hash=args.diff_hash,
            task_id=args.task_id,
        )
    except ApprovalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    summary = build_awaiting_summary(request["task_id"], args.review_report)
    enqueue_notification(
        run_dir=Path(args.run_dir),
        kind="awaiting_human_approval",
        summary=summary,
        report_hint=Path(args.review_report).name,
    )
    print(request["run_id"])
    return 0


def cmd_notify_blocked(args: argparse.Namespace) -> int:
    summary = build_blocked_summary(args.reason, args.report_hint or "")
    enqueue_notification(
        run_dir=Path(args.run_dir),
        kind=args.kind,
        summary=summary,
        report_hint=args.report_hint or "",
    )
    return 0


def cmd_wait_decision(args: argparse.Namespace) -> int:
    timeout = args.timeout if args.timeout is not None else human_approval_timeout_sec()
    ok = wait_for_decision(Path(args.run_dir), timeout_sec=timeout, poll_interval=args.poll_interval)
    return 0 if ok else 2


def cmd_verify_reviewed_snapshot(args: argparse.Namespace) -> int:
    """Mandatory planner pre-integration check against the approved reviewed hash."""
    try:
        result = verify_reviewed_snapshot(Path(args.run_dir))
    except (ApprovalError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("matches") else 2


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[telegram-bridge] %(levelname)s %(message)s",
    )
    try:
        config = load_bridge_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    runs_root = Path(args.runs_root) if args.runs_root else (config.runs_root or _default_runs_root())
    client = TelegramClient(config.bot_token, api_base=config.api_base)
    bridge = Bridge(config, client, runs_root)
    logging.getLogger("agent_dx.bridge").info(
        "serving runs_root=%s allowlist_user=%s allowlist_chat=%s",
        runs_root,
        config.allowed_user_id,
        config.allowed_chat_id,
    )
    bridge.run_forever(max_cycles=args.max_cycles)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent-loop Telegram / human-approval helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("project-state-dir", help="Resolve collision-safe external state for a repository")
    p.add_argument("--repo", required=True)
    p.add_argument("--state-root", default=None)
    p.set_defaults(func=cmd_project_state_dir)

    p = sub.add_parser("render-systemd-unit", help="Render a path-safe user unit from the template")
    p.add_argument("--tool-root", default=None)
    p.add_argument("--state-root", default=None)
    p.add_argument("--credentials-file", default=None)
    p.add_argument("--output", default=None)
    p.set_defaults(func=cmd_render_systemd_unit)

    p = sub.add_parser("compute-diff-hash", help="Hash worktree diff vs base commit")
    p.add_argument("--worktree", required=True)
    p.add_argument("--base-commit", required=True)
    p.set_defaults(func=cmd_compute_diff_hash)

    p = sub.add_parser("create-request", help="Write approval request + awaiting notify outbox")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--base-commit", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--review-report", required=True)
    p.add_argument("--diff-hash", default=None)
    p.add_argument("--task-id", default=None)
    p.set_defaults(func=cmd_create_request)

    p = sub.add_parser("notify-blocked", help="Enqueue BLOCKED/failure notification (no button)")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--report-hint", default="")
    p.add_argument("--kind", choices=("blocked", "failure"), default="blocked")
    p.set_defaults(func=cmd_notify_blocked)

    p = sub.add_parser("wait-decision", help="Wait for human approval decision")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--timeout", type=int, default=None)
    p.add_argument("--poll-interval", type=float, default=1.0)
    p.set_defaults(func=cmd_wait_decision)

    p = sub.add_parser(
        "verify-reviewed-snapshot",
        help="Verify live worktree still matches the reviewed/approved diff_hash",
    )
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_verify_reviewed_snapshot)

    p = sub.add_parser("serve", help="Long-poll Telegram bridge")
    p.add_argument("--runs-root", default=None)
    p.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (tests)")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
