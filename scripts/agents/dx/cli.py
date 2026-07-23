"""CLI entrypoints used by run_task.sh and the systemd user unit."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import logging
import os
import re
import stat
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
from .delivery import DeliveryError, deliver_run, freeze_delivery_config
from .atomic import atomic_write_json
from .paths import (
    PathConfigError,
    default_state_root,
    project_state_dir,
    render_systemd_unit,
)
from .profile import (
    ProfileError,
    ProjectProfile,
    build_authorized_environment,
    load_instruction_text,
    load_project_profile,
)
from .runstate import (
    RunStateError,
    attach_evidence,
    plan_resume,
    validate_run,
    write_run_metadata,
)
from .runtime import phase_settings, supervise_command, tracked_worktree_clean
from .snapshot import (
    SUMMARY_FILENAME,
    SnapshotError,
    build_snapshot_manifest,
    prepare_review_artifacts,
    reject_nonignored_special_files,
    validate_documentation,
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
    messages = None
    summary_path = Path(args.run_dir) / SUMMARY_FILENAME
    if summary_path.is_file():
        try:
            technical = json.loads(summary_path.read_text(encoding="utf-8"))
            configured = technical.get("telegram_messages")
            if isinstance(configured, list) and configured and all(
                isinstance(item, str) for item in configured
            ):
                messages = configured
                summary = configured[0]
        except (OSError, UnicodeError, json.JSONDecodeError):
            messages = None
    enqueue_notification(
        run_dir=Path(args.run_dir),
        kind="awaiting_human_approval",
        summary=summary,
        report_hint=Path(args.review_report).name,
        messages=messages,
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


def _profile_for(args: argparse.Namespace) -> ProjectProfile:
    return load_project_profile(Path(args.repo), missing_policy=getattr(args, "missing_policy", "allow"))


def cmd_profile(args: argparse.Namespace) -> int:
    try:
        profile = _profile_for(args)
    except ProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(profile.public_dict(), indent=2, sort_keys=True))
    return 0


def cmd_instructions(args: argparse.Namespace) -> int:
    try:
        repo = Path(args.repo).resolve()
        profile = load_project_profile(repo)
        print(load_instruction_text(profile, repo, args.phase), end="")
    except (OSError, UnicodeError, ProfileError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _runtime_context(args: argparse.Namespace) -> dict[str, str]:
    values = {
        "AGENT_LOOP_TARGET_REPO": args.repo,
        "AGENT_LOOP_WORKTREE": args.worktree,
        "AGENT_LOOP_RUN_DIR": args.run_dir,
        "AGENT_LOOP_TASK_FILE": args.task_file,
        "AGENT_LOOP_BASE_COMMIT": args.base_commit,
    }
    return {key: str(value) for key, value in values.items()}


def _run_profile_command(
    args: argparse.Namespace,
    command: tuple[str, ...] | list[str],
    *,
    phase: str,
    iteration: int,
    report: Path | None = None,
    artifacts: list[Path] | None = None,
) -> int:
    try:
        profile = load_project_profile(Path(args.worktree))
        environment_profile = profile
        if phase == "reviewer":
            environment_profile = dataclasses.replace(profile, required_environment=())
        environment, diagnostics = build_authorized_environment(
            environment_profile,
            args.env_file if phase != "reviewer" else None,
            context=_runtime_context(args),
        )
        for name, state in sorted(diagnostics.items()):
            print(f"[agent-loop] environment {name}={state}")
        secrets = {
            name: environment[name]
            for name in environment_profile.required_environment
            if name in environment
        }
        timeout, heartbeat = phase_settings(profile, phase)
        return supervise_command(
            command=command,
            phase=phase,
            iteration=iteration,
            cwd=Path(args.worktree),
            run_dir=Path(args.run_dir),
            environment=environment,
            secret_values=secrets,
            timeout_seconds=timeout,
            heartbeat_seconds=heartbeat,
            terminate_grace_seconds=profile.terminate_grace_seconds,
            report_path=report,
            sanitize_artifacts=artifacts or (),
        )
    except (OSError, ProfileError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_run_bootstrap(args: argparse.Namespace) -> int:
    try:
        profile = load_project_profile(Path(args.worktree))
    except ProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if profile.bootstrap_command is None:
        print("[agent-loop] bootstrap not configured; continuing with safe defaults")
        return 0
    result = _run_profile_command(
        args,
        profile.bootstrap_command,
        phase="bootstrap",
        iteration=0,
    )
    if result == 0 and not tracked_worktree_clean(Path(args.worktree), args.base_commit):
        print("ERROR: bootstrap modified tracked repository files", file=sys.stderr)
        return 125
    if result == 0:
        try:
            reject_nonignored_special_files(Path(args.worktree))
        except SnapshotError as exc:
            print(f"ERROR: bootstrap produced unsafe repository artifacts: {exc}", file=sys.stderr)
            return 125
    return result


def cmd_run_validations(args: argparse.Namespace) -> int:
    try:
        profile = load_project_profile(Path(args.worktree))
    except ProfileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    for index, command in enumerate(profile.validation_commands, 1):
        result = _run_profile_command(
            args,
            command,
            phase="validation",
            iteration=index,
        )
        if result != 0:
            return result
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("ERROR: supervised command is required", file=sys.stderr)
        return 2
    return _run_profile_command(
        args,
        command,
        phase=args.phase,
        iteration=args.iteration,
        report=Path(args.report) if args.report else None,
        artifacts=[Path(value) for value in args.artifact],
    )


def cmd_init_run(args: argparse.Namespace) -> int:
    try:
        profile = load_project_profile(Path(args.worktree))
        task_id = Path(args.task_file).stem
        task_slug = re.sub(r"[^a-z0-9._-]+", "-", task_id.lower()).strip("-")
        if not task_slug:
            raise DeliveryError("cannot derive a safe task slug")
        delivery = freeze_delivery_config(
            repo=Path(args.repo).resolve(),
            worktree=Path(args.worktree).resolve(),
            base_commit=args.base_commit,
            task_file=args.task_file,
            task_id=task_id,
            task_slug=task_slug,
            profile=profile,
        )
        payload = write_run_metadata(
            Path(args.run_dir),
            {
                "repo": str(Path(args.repo).resolve()),
                "task_file": args.task_file,
                "base_commit": args.base_commit,
                "worktree": str(Path(args.worktree).resolve()),
                "max_iterations": args.max_iterations,
                "env_file": str(Path(args.env_file).expanduser().resolve()) if args.env_file else None,
                "profile": profile.public_dict(),
                "delivery": delivery,
            },
        )
    except (OSError, ProfileError, RunStateError, DeliveryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(payload["run_id"])
    return 0


def cmd_validate_run(args: argparse.Namespace) -> int:
    try:
        result = validate_run(Path(args.run_dir))
    except RunStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_resume_plan(args: argparse.Namespace) -> int:
    try:
        result = plan_resume(Path(args.run_dir), review_only=args.review_only)
    except (OSError, ApprovalError, RunStateError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.format == "nul":
        fields = (
            "repo",
            "worktree",
            "task_file",
            "base_commit",
            "max_iterations",
            "env_file",
            "resume_phase",
            "iteration",
        )
        for field in fields:
            sys.stdout.buffer.write(str(result.get(field) or "").encode("utf-8") + b"\0")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_resume_exec(args: argparse.Namespace) -> int:
    """Acquire a no-follow run lock and exec the shell engine while holding it."""
    run_candidate = Path(args.run_dir).expanduser()
    if run_candidate.is_symlink() or not run_candidate.is_dir():
        print("ERROR: run directory must be a regular directory", file=sys.stderr)
        return 1
    run_dir = run_candidate.resolve()
    try:
        fd = os.open(
            run_dir / ".resume.lock",
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("resume lock is not a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        print(f"ERROR: cannot lock run for resume: {exc}", file=sys.stderr)
        return 1
    os.set_inheritable(fd, True)
    command = [
        "bash",
        str(_repo_root_from_here() / "scripts" / "agents" / "run_task.sh"),
        "--resume-run-dir",
        str(run_dir),
    ]
    if args.review_only:
        command.append("--review-only")
    if args.env_file:
        command.extend(("--env-file", args.env_file))
    environment = dict(os.environ)
    environment["AGENT_LOOP_TOOL_ROOT"] = str(_repo_root_from_here())
    os.execvpe(command[0], command, environment)
    return 1  # pragma: no cover


def cmd_record_review_snapshot(args: argparse.Namespace) -> int:
    try:
        atomic_write_json(
            Path(args.run_dir) / f"review-{args.iteration}-snapshot.json",
            {
                "schema_version": 1,
                "iteration": args.iteration,
                "diff_hash": args.diff_hash,
            },
        )
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_validate_documentation(args: argparse.Namespace) -> int:
    try:
        profile = load_project_profile(Path(args.worktree))
        manifest = build_snapshot_manifest(Path(args.worktree), args.base_commit)
        changed = validate_documentation(
            profile,
            manifest,
            task_id=args.task_id,
            task_slug=args.task_slug,
        )
    except (OSError, ProfileError, SnapshotError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"documentation": changed}, sort_keys=True))
    return 0


def cmd_prepare_review_artifacts(args: argparse.Namespace) -> int:
    try:
        profile = load_project_profile(Path(args.worktree))
        summary, _messages = prepare_review_artifacts(
            run_dir=Path(args.run_dir),
            repo=Path(args.repo),
            worktree=Path(args.worktree),
            task_file=args.task_file,
            task_id=args.task_id,
            task_slug=args.task_slug,
            base_commit=args.base_commit,
            iteration=args.iteration,
            max_iterations=args.max_iterations,
            executor_report=Path(args.executor_report),
            reviewer_report=Path(args.reviewer_report),
            reviewed_hash=args.reviewed_hash,
            profile=profile,
        )
    except (OSError, ProfileError, SnapshotError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"file_count": summary["file_count"]}, sort_keys=True))
    return 0


def cmd_deliver_run(args: argparse.Namespace) -> int:
    try:
        result = deliver_run(Path(args.run_dir))
    except DeliveryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_review_status(args: argparse.Namespace) -> int:
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"ERROR: invalid reviewer report: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict) or set(data) != {"status", "summary", "findings", "tests_required"}:
        print("ERROR: reviewer report has missing or unknown fields", file=sys.stderr)
        return 1
    if data["status"] not in {"APPROVED", "CHANGES_REQUESTED", "BLOCKED"}:
        print("ERROR: invalid reviewer status", file=sys.stderr)
        return 1
    if not isinstance(data["summary"], str) or not isinstance(data["tests_required"], list) or not all(
        isinstance(item, str) for item in data["tests_required"]
    ):
        print("ERROR: invalid reviewer summary/tests_required", file=sys.stderr)
        return 1
    if not isinstance(data["findings"], list):
        print("ERROR: invalid reviewer findings", file=sys.stderr)
        return 1
    for finding in data["findings"]:
        if (
            not isinstance(finding, dict)
            or set(finding) != {"severity", "title", "details", "files"}
            or finding.get("severity") not in {"critical", "high", "medium", "low"}
            or not isinstance(finding.get("title"), str)
            or not isinstance(finding.get("details"), str)
            or not isinstance(finding.get("files"), list)
            or not all(isinstance(item, str) for item in finding["files"])
        ):
            print("ERROR: invalid reviewer finding", file=sys.stderr)
            return 1
    print(data["status"])
    return 0


def cmd_set_status(args: argparse.Namespace) -> int:
    from .approval import write_status

    write_status(Path(args.run_dir), args.status)
    return 0


def cmd_record_failure(args: argparse.Namespace) -> int:
    from .approval import utc_now_iso, write_status

    run_dir = Path(args.run_dir)
    write_status(run_dir, "BLOCKED")
    atomic_write_json(
        run_dir / "failure.json",
        {
            "schema_version": 1,
            "reason": args.reason,
            "phase": args.phase,
            "iteration": args.iteration,
            "report": args.report or None,
            "recorded_at": utc_now_iso(),
        },
    )
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    try:
        result = attach_evidence(Path(args.run_dir), Path(args.file), max_bytes=args.max_bytes)
    except (OSError, RunStateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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

    p = sub.add_parser("profile", help="Parse and print the sanitized project profile")
    p.add_argument("--repo", required=True)
    p.add_argument("--missing-policy", choices=("allow", "deny"), default="allow")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("instructions", help="Read validated tracked phase instructions")
    p.add_argument("--repo", required=True)
    p.add_argument("--phase", choices=("executor", "reviewer"), required=True)
    p.set_defaults(func=cmd_instructions)

    def add_runtime_arguments(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--repo", required=True)
        command_parser.add_argument("--worktree", required=True)
        command_parser.add_argument("--run-dir", required=True)
        command_parser.add_argument("--task-file", required=True)
        command_parser.add_argument("--base-commit", required=True)
        command_parser.add_argument("--env-file", default=None)

    p = sub.add_parser("run-bootstrap", help="Run the configured worktree bootstrap safely")
    add_runtime_arguments(p)
    p.set_defaults(func=cmd_run_bootstrap)

    p = sub.add_parser("run-validations", help="Run configured validation commands safely")
    add_runtime_arguments(p)
    p.set_defaults(func=cmd_run_validations)

    p = sub.add_parser("supervise", help="Run one agent phase with timeout and heartbeat")
    add_runtime_arguments(p)
    p.add_argument("--phase", choices=("executor", "reviewer"), required=True)
    p.add_argument("--iteration", type=int, required=True)
    p.add_argument("--report", default=None)
    p.add_argument("--artifact", action="append", default=[])
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_supervise)

    p = sub.add_parser("init-run", help="Write immutable resume metadata")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--base-commit", required=True)
    p.add_argument("--max-iterations", type=int, required=True)
    p.add_argument("--env-file", default=None)
    p.set_defaults(func=cmd_init_run)

    p = sub.add_parser("validate-run", help="Validate resume bindings")
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_validate_run)

    p = sub.add_parser("resume-plan", help="Validate and select the last trustworthy phase")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--review-only", action="store_true")
    p.add_argument("--format", choices=("json", "nul"), default="json")
    p.set_defaults(func=cmd_resume_plan)

    p = sub.add_parser("resume-exec", help=argparse.SUPPRESS)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--review-only", action="store_true")
    p.add_argument("--env-file", default=None)
    p.set_defaults(func=cmd_resume_exec)

    p = sub.add_parser("record-review-snapshot", help="Bind an in-flight review to a diff hash")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--iteration", type=int, required=True)
    p.add_argument("--diff-hash", required=True)
    p.set_defaults(func=cmd_record_review_snapshot)

    p = sub.add_parser(
        "validate-documentation",
        help="Require configured documentation paths in the current snapshot",
    )
    p.add_argument("--worktree", required=True)
    p.add_argument("--base-commit", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--task-slug", required=True)
    p.set_defaults(func=cmd_validate_documentation)

    p = sub.add_parser(
        "prepare-review-artifacts",
        help="Freeze the reviewed manifest and Telegram technical summary",
    )
    p.add_argument("--run-dir", required=True)
    p.add_argument("--repo", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--task-file", required=True)
    p.add_argument("--task-id", required=True)
    p.add_argument("--task-slug", required=True)
    p.add_argument("--base-commit", required=True)
    p.add_argument("--iteration", type=int, required=True)
    p.add_argument("--max-iterations", type=int, required=True)
    p.add_argument("--executor-report", required=True)
    p.add_argument("--reviewer-report", required=True)
    p.add_argument("--reviewed-hash", required=True)
    p.set_defaults(func=cmd_prepare_review_artifacts)

    p = sub.add_parser(
        "deliver-run",
        help="Publish the exact human-approved snapshot to its frozen branch",
    )
    p.add_argument("--run-dir", required=True)
    p.set_defaults(func=cmd_deliver_run)

    p = sub.add_parser("review-status", help="Validate a complete reviewer JSON report")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_review_status)

    p = sub.add_parser("set-status", help="Atomically write a run status")
    p.add_argument("--run-dir", required=True)
    p.add_argument("status")
    p.set_defaults(func=cmd_set_status)

    p = sub.add_parser("record-failure", help="Atomically block a run with structured reason")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--phase", required=True)
    p.add_argument("--iteration", type=int, default=0)
    p.add_argument("--report", default=None)
    p.set_defaults(func=cmd_record_failure)

    p = sub.add_parser("evidence", help="Attach bounded untrusted evidence to a run")
    p.add_argument("--run-dir", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--max-bytes", type=int, default=1024 * 1024)
    p.set_defaults(func=cmd_evidence)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
