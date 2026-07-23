"""Human approval contract: technical APPROVED vs HUMAN_APPROVED."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic import (
    atomic_write_json,
    atomic_write_text,
    exclusive_write_json,
    read_json,
    run_scoped_lock,
)

STATUS_APPROVED = "APPROVED"
STATUS_AWAITING = "AWAITING_HUMAN_APPROVAL"
STATUS_HUMAN_APPROVED = "HUMAN_APPROVED"
STATUS_BLOCKED = "BLOCKED"

REQUEST_FILENAME = "human_approval_request.json"
DECISION_FILENAME = "human_approval_decision.json"
NOTIFY_FILENAME = "telegram_notify.json"
STATUS_FILENAME = "status"
LOCK_FILENAME = ".approval.lock"

SCHEMA_VERSION = 1
TOKEN_BYTES = 16  # 32 hex chars; fits Telegram callback_data (64 byte limit)
MESSAGE_SOFT_LIMIT = 3500

# Security binding: recreate is idempotent only when these match exactly.
REQUEST_BINDING_KEYS = (
    "task",
    "task_id",
    "base_commit",
    "worktree",
    "review_report",
    "diff_hash",
    "run_id",
)


class ApprovalError(ValueError):
    """Invalid approval request/decision transition."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_id_from_dir(run_dir: Path) -> str:
    return Path(run_dir).name


def write_status(run_dir: Path, status: str) -> None:
    atomic_write_text(Path(run_dir) / STATUS_FILENAME, status)


def read_status(run_dir: Path) -> str:
    path = Path(run_dir) / STATUS_FILENAME
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _security_binding(payload: dict[str, Any]) -> dict[str, str]:
    return {key: str(payload.get(key, "")) for key in REQUEST_BINDING_KEYS}


def _security_bindings_equal(existing: dict[str, Any], proposed: dict[str, Any]) -> bool:
    return _security_binding(existing) == _security_binding(proposed)


def _untracked_entry_fingerprint(worktree: Path, relative: str) -> bytes:
    """
    Content-address one untracked Git entry without following links.

    Includes relative path, Git file type, and Git-relevant mode. Regular files
    are opened with ``O_NOFOLLOW`` so a symlink swap cannot redirect the read.
    Symlinks contribute the link-target bytes themselves (never the referent).
    Unsupported/special types are rejected without opening or blocking.
    """
    # Refuse path escape; git ls-files should already be relative and clean.
    if relative.startswith("/") or relative == ".." or "/../" in f"/{relative}/":
        raise ApprovalError(f"unsafe untracked path: {relative!r}")

    path = worktree / relative
    try:
        st = path.lstat()
    except OSError as exc:
        raise ApprovalError(f"untracked entry missing: {relative}: {exc}") from exc

    mode = st.st_mode
    entry = hashlib.sha256()
    entry.update(relative.encode("utf-8", errors="surrogateescape"))
    entry.update(b"\0")

    if stat.S_ISLNK(mode):
        # Git mode for symlinks is always 120000; payload is the target string.
        target = os.readlink(path)
        if isinstance(target, bytes):
            target_bytes = target
        else:
            target_bytes = target.encode("utf-8", errors="surrogateescape")
        entry.update(b"lnk\0")
        entry.update(b"120000\0")
        entry.update(hashlib.sha256(target_bytes).hexdigest().encode("ascii"))
        return entry.digest()

    if stat.S_ISREG(mode):
        # Match Git's executable bit: any +x → 100755, else 100644.
        git_mode = b"100755" if (mode & 0o111) else b"100644"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise ApprovalError(f"cannot open untracked file: {relative}: {exc}") from exc
        try:
            # Re-check type after open to close a symlink-swap race when
            # O_NOFOLLOW is unavailable (non-POSIX) or open raced with replace.
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ApprovalError(
                    f"untracked entry type changed during hash: {relative}"
                )
            hasher = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
            content_hex = hasher.hexdigest().encode("ascii")
        finally:
            os.close(fd)
        entry.update(b"reg\0")
        entry.update(git_mode)
        entry.update(b"\0")
        entry.update(content_hex)
        return entry.digest()

    raise ApprovalError(
        f"unsupported untracked type for {relative!r} "
        f"(mode={stat.filemode(mode)})"
    )


def compute_diff_hash(worktree: Path, base_commit: str) -> str:
    """
    Hash the reviewed tree state: binary diff vs base + sorted untracked digests.

    Untracked entries are fingerprinted with path + Git type/mode metadata and
    type-safe payloads (see ``_untracked_entry_fingerprint``). Ordering remains
    lexicographic by relative path so the reviewed snapshot stays deterministic.
    """
    worktree = Path(worktree)
    if not worktree.is_dir():
        raise ApprovalError(f"worktree not found: {worktree}")

    diff = subprocess.check_output(
        ["git", "-C", str(worktree), "diff", "--binary", base_commit, "--"],
        stderr=subprocess.STDOUT,
    )
    digest = hashlib.sha256()
    digest.update(b"DIFF\0")
    digest.update(diff)

    listed = subprocess.check_output(
        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z"],
        stderr=subprocess.STDOUT,
    )
    files = [item.decode("utf-8", errors="surrogateescape") for item in listed.split(b"\0") if item]
    files.sort()
    digest.update(b"UNTRACKED\0")
    for relative in files:
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(_untracked_entry_fingerprint(worktree, relative).hex().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def create_approval_request(
    *,
    run_dir: Path,
    task: str,
    base_commit: str,
    worktree: Path | str,
    review_report: str,
    diff_hash: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """
    Persist an atomic human-approval request after technical APPROVED.

    ``diff_hash`` should be the content-addressed snapshot that was hashed
    immediately before and after Codex review (equal). The human gate approves
    that immutable hash — it does not claim the live worktree is frozen.

    State machine (under the run-scoped lock):
    - A **new** request may open only when public status is already technical
      ``APPROVED`` (``run_task`` records that before calling create). Transition
      is ``APPROVED`` → request on disk → ``AWAITING_HUMAN_APPROVAL``. Opening
      from ``BLOCKED``, ``HUMAN_APPROVED``, empty, or unrelated states raises.
    - Re-entry with an identical security binding never rotates
      ``callback_token``. Valid ``HUMAN_APPROVED`` decisions are retained.
      Crash recovery promotes identical pending request from ``APPROVED`` →
      ``AWAITING_HUMAN_APPROVAL``. Identical retry while already ``AWAITING``
      is idempotent. Identical retry in ``BLOCKED`` is a no-op (does not reopen).
    - Binding mismatch raises ``ApprovalError`` and leaves artifacts untouched.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = Path(worktree)
    resolved_hash = diff_hash or compute_diff_hash(worktree_path, base_commit)
    if len(resolved_hash) < 32:
        raise ApprovalError("diff_hash looks too short")

    task_name = Path(task).name
    derived_task_id = task_id or Path(task_name).stem
    proposed = {
        "task": task,
        "task_id": derived_task_id,
        "run_id": run_id_from_dir(run_dir),
        "base_commit": base_commit,
        "worktree": str(worktree_path),
        "review_report": review_report,
        "diff_hash": resolved_hash,
    }
    request_path = run_dir / REQUEST_FILENAME
    # Serialize with callback claims: publish request + AWAITING under one lock.
    with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
        existing: dict[str, Any] | None = None
        if request_path.is_file():
            try:
                existing = read_json(request_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ApprovalError(f"corrupt approval request: {exc}") from exc

        if existing is not None:
            if not _security_bindings_equal(existing, proposed):
                raise ApprovalError(
                    "approval request binding mismatch; refusing to recreate"
                )
            # Identical binding: never rotate token; recover valid decision if any.
            if _decision_ready_locked(run_dir):
                return existing
            status = read_status(run_dir)
            # Interrupted after atomic request publish but before AWAITING:
            # promote technical APPROVED → AWAITING so the callback can claim.
            if status == STATUS_APPROVED:
                write_status(run_dir, STATUS_AWAITING)
            # AWAITING: idempotent. BLOCKED / HUMAN_APPROVED / other: no-op
            # (never reopen or downgrade).
            return existing

        # No request yet — new gate only from technical APPROVED.
        status = read_status(run_dir)
        if status != STATUS_APPROVED:
            raise ApprovalError(
                "approval gate may only open from technical APPROVED; "
                f"got {status!r}"
            )

        # Refuse orphan decisions that would poison a new gate.
        if (run_dir / DECISION_FILENAME).is_file():
            raise ApprovalError(
                "orphan decision artifact present without approval request"
            )

        token = secrets.token_hex(TOKEN_BYTES)
        request = {
            "schema_version": SCHEMA_VERSION,
            "technical_status": STATUS_APPROVED,
            **proposed,
            "callback_token": token,
            "token_consumed": False,
            "created_at": utc_now_iso(),
        }
        # Caller already recorded technical APPROVED; publish request then
        # AWAITING before the lock is released so claims cannot observe a
        # half-published gate.
        atomic_write_json(request_path, request)
        write_status(run_dir, STATUS_AWAITING)
        return request


def _awaiting_button_publish_allowed_locked(run_dir: Path) -> dict[str, Any] | None:
    """
    Return the pending request when an awaiting button may be published.

    Must be called while holding the run-scoped lock. Requires public status
    ``AWAITING_HUMAN_APPROVAL``, a pending request on disk, and no validated
    human decision — so a callback between request creation and notify cannot
    yield a stale approval button.
    """
    if read_status(run_dir) != STATUS_AWAITING:
        return None
    request_path = run_dir / REQUEST_FILENAME
    if not request_path.is_file():
        return None
    if (run_dir / DECISION_FILENAME).is_file():
        try:
            validate_decision_matches_request(run_dir)
        except ApprovalError:
            pass
        else:
            if read_status(run_dir) != STATUS_HUMAN_APPROVED:
                write_status(run_dir, STATUS_HUMAN_APPROVED)
            return None
    try:
        return read_json(request_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def enqueue_notification(
    *,
    run_dir: Path,
    kind: str,
    summary: str,
    report_hint: str = "",
) -> dict[str, Any] | None:
    """
    Write a best-effort outbox entry for the Telegram bridge.

    Failure to deliver must not change approval state; this only stages a message.

    For ``awaiting_human_approval``, publication is lock-coordinated and runs only
    while the gate is still ``AWAITING_HUMAN_APPROVAL`` with a matching pending
    request and no valid decision. Returns ``None`` when the button must not be
    offered (e.g. callback won the race, or status is already ``HUMAN_APPROVED`` /
    ``BLOCKED``).
    """
    run_dir = Path(run_dir)
    if kind not in {"awaiting_human_approval", "blocked", "failure"}:
        raise ApprovalError(f"unsupported notify kind: {kind}")

    if kind == "awaiting_human_approval":
        with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
            request = _awaiting_button_publish_allowed_locked(run_dir)
            if request is None:
                return None
            payload: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "kind": kind,
                "run_id": run_id_from_dir(run_dir),
                "status": STATUS_AWAITING,
                "summary": truncate_message(summary),
                "report_hint": report_hint,
                "created_at": utc_now_iso(),
                "notification_id": secrets.token_hex(8),
                "sent_at": None,
                "offer_approval_button": True,
                "callback_token": request["callback_token"],
                "diff_hash": request["diff_hash"],
                "task_id": request.get("task_id", ""),
            }
            atomic_write_json(run_dir / NOTIFY_FILENAME, payload)
            return payload

    with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "kind": kind,
            "run_id": run_id_from_dir(run_dir),
            "status": read_status(run_dir),
            "summary": truncate_message(summary),
            "report_hint": report_hint,
            "created_at": utc_now_iso(),
            "notification_id": secrets.token_hex(8),
            "sent_at": None,
            "offer_approval_button": False,
        }
        atomic_write_json(run_dir / NOTIFY_FILENAME, payload)
        return payload


def truncate_message(text: str, limit: int = MESSAGE_SOFT_LIMIT) -> str:
    text = text.replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)].rstrip() + "\n…[truncated]"


def load_request(run_dir: Path) -> dict[str, Any]:
    return read_json(Path(run_dir) / REQUEST_FILENAME)


def load_decision(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / DECISION_FILENAME
    if not path.is_file():
        return None
    return read_json(path)


def find_run_dir_by_token(runs_root: Path, token: str) -> Path | None:
    if not token or len(token) > 64:
        return None
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return None
    # ``runs_root`` may be one legacy runs directory, one project state root,
    # or the external AG-01 root. Deliberately avoid recursive worktree scans.
    patterns = (
        f"*/{REQUEST_FILENAME}",
        f"runs/*/{REQUEST_FILENAME}",
        f"projects/*/runs/*/{REQUEST_FILENAME}",
    )
    request_paths = {path for pattern in patterns for path in runs_root.glob(pattern)}
    for request_path in sorted(request_paths):
        child = request_path.parent
        try:
            request = read_json(request_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if request.get("callback_token") == token:
            return child
    return None


def _decision_matches_request(
    existing: dict[str, Any],
    request: dict[str, Any],
    callback_token: str,
) -> bool:
    try:
        _validate_decision_binding(request, existing, callback_token=callback_token)
    except ApprovalError:
        return False
    return True


def _require_positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value <= 0:
        raise ApprovalError(f"decision {key} missing or not a positive int")
    return value


def _validate_decision_binding(
    request: dict[str, Any],
    decision: dict[str, Any],
    *,
    callback_token: str | None = None,
) -> None:
    """Reject incomplete, forged, or unbound decision artifacts."""
    if request.get("schema_version") != SCHEMA_VERSION:
        raise ApprovalError("request schema_version mismatch")
    if decision.get("schema_version") != SCHEMA_VERSION:
        raise ApprovalError("decision schema_version mismatch")
    if decision.get("decision") != "approve":
        raise ApprovalError("decision is not approve")
    if decision.get("run_id") != request.get("run_id"):
        raise ApprovalError("decision run_id mismatch")
    if not decision.get("diff_hash") or decision.get("diff_hash") != request.get("diff_hash"):
        raise ApprovalError("decision diff_hash mismatch")

    expected_token = request.get("callback_token")
    if not expected_token or decision.get("callback_token") != expected_token:
        raise ApprovalError("decision callback_token mismatch")
    if callback_token is not None and decision.get("callback_token") != callback_token:
        raise ApprovalError("decision callback_token mismatch")

    if request.get("token_consumed") is not True:
        raise ApprovalError("request token not consumed")

    _require_positive_int(decision, "telegram_user_id")
    _require_positive_int(decision, "telegram_chat_id")


def _idempotent_replay_locked(
    run_dir: Path,
    existing: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Return idempotent_replay under the held run-scoped lock.

    If a fully valid decision was published but status never reached
    HUMAN_APPROVED (crash between exclusive decision publish and status
    write), repair status while still holding the lock so bridge/waiters
    observe the repaired persisted state.
    """
    if read_status(run_dir) != STATUS_HUMAN_APPROVED:
        write_status(run_dir, STATUS_HUMAN_APPROVED)
    return "idempotent_replay", existing


def _claim_human_approval_locked(
    *,
    run_dir: Path,
    request_path: Path,
    callback_token: str,
    telegram_user_id: int,
    telegram_chat_id: int,
) -> tuple[str, dict[str, Any]]:
    """
    Claim approval under an already-held run-scoped lock.

    Approves the immutable reviewed ``diff_hash`` bound in the request. The
    live worktree is not re-hashed here: a run lock cannot freeze arbitrary
    worktree writes. Planner integration must call
    ``verify_reviewed_snapshot`` before trusting the tree contents.
    """
    if not request_path.is_file():
        return "rejected_wrong_state", {}

    request = read_json(request_path)
    if request.get("callback_token") != callback_token:
        return "rejected_token", {}

    existing = load_decision(run_dir)
    if existing is not None:
        if _decision_matches_request(existing, request, callback_token):
            return _idempotent_replay_locked(run_dir, existing)
        return "rejected_wrong_state", existing

    status = read_status(run_dir)
    # Claimable only after create_approval_request establishes AWAITING under lock.
    if status != STATUS_AWAITING:
        return "rejected_wrong_state", {}

    if request.get("token_consumed"):
        existing = load_decision(run_dir)
        if existing is not None and _decision_matches_request(existing, request, callback_token):
            return _idempotent_replay_locked(run_dir, existing)
        # Interrupted claim: token marked consumed before decision publish — finish it.
        if existing is not None:
            return "rejected_token", {}
    else:
        # Mark consumed before exclusive publish so waiters never accept an unbound decision.
        request["token_consumed"] = True
        atomic_write_json(request_path, request)

    expected_hash = request["diff_hash"]
    decision = {
        "schema_version": SCHEMA_VERSION,
        "decision": "approve",
        "run_id": request["run_id"],
        "diff_hash": expected_hash,
        "callback_token": callback_token,
        "telegram_user_id": telegram_user_id,
        "telegram_chat_id": telegram_chat_id,
        "request_created_at": request.get("created_at"),
        "decided_at": utc_now_iso(),
    }
    decision_path = run_dir / DECISION_FILENAME
    # Exclusive publish is the one-use claim; losers see a stable idempotent replay.
    if not exclusive_write_json(decision_path, decision):
        request = read_json(request_path)
        existing = load_decision(run_dir)
        if existing is not None and _decision_matches_request(existing, request, callback_token):
            return _idempotent_replay_locked(run_dir, existing)
        return "rejected_wrong_state", existing or {}

    write_status(run_dir, STATUS_HUMAN_APPROVED)
    return "accepted", decision


def apply_human_approval(
    *,
    run_dir: Path,
    callback_token: str,
    telegram_user_id: int,
    telegram_chat_id: int,
    allowed_user_id: int,
    allowed_chat_id: int,
) -> tuple[str, dict[str, Any]]:
    """
    Record an authenticated human approval bound to run_id + reviewed diff_hash.

    Callback claiming is serialized with a run-scoped flock and an exclusive
    decision-file publish so concurrent replays yield exactly one ``accepted``.
    The decision approves the content-addressed reviewed snapshot hash stored in
    the request — not a claim that the mutable worktree cannot change.

    Returns (result, decision_or_existing) where result is one of:
      accepted | idempotent_replay | rejected_unauthorized | rejected_token |
      rejected_wrong_state
    """
    run_dir = Path(run_dir)

    if telegram_user_id != allowed_user_id or telegram_chat_id != allowed_chat_id:
        return "rejected_unauthorized", {}

    request_path = run_dir / REQUEST_FILENAME
    if not request_path.is_file():
        return "rejected_wrong_state", {}

    request = read_json(request_path)
    if request.get("callback_token") != callback_token:
        return "rejected_token", {}

    with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
        return _claim_human_approval_locked(
            run_dir=run_dir,
            request_path=request_path,
            callback_token=callback_token,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        )


def validate_decision_matches_request(run_dir: Path) -> dict[str, Any]:
    request = load_request(run_dir)
    decision = load_decision(run_dir)
    if decision is None:
        raise ApprovalError("decision missing")
    _validate_decision_binding(request, decision)
    return decision


def verify_reviewed_snapshot(run_dir: Path) -> dict[str, Any]:
    """
    Mandatory pre-integration check for the planner.

    Recomputes the live worktree hash and compares it to the reviewed
    ``diff_hash`` recorded in the request (and decision, when present).
    ``HUMAN_APPROVED`` means the operator approved that immutable hash; this
    command detects post-approval worktree drift before integration.
    """
    run_dir = Path(run_dir)
    request = load_request(run_dir)
    if read_status(run_dir) != STATUS_HUMAN_APPROVED:
        raise ApprovalError("run is not HUMAN_APPROVED")
    decision = load_decision(run_dir)
    if decision is None:
        raise ApprovalError("human approval decision missing")
    _validate_decision_binding(request, decision)
    expected = request.get("diff_hash")
    if not expected or len(str(expected)) < 32:
        raise ApprovalError("reviewed diff_hash missing from request")

    worktree = Path(request["worktree"])
    base_commit = str(request["base_commit"])
    current = compute_diff_hash(worktree, base_commit)
    if decision.get("diff_hash") != expected:
        raise ApprovalError("decision diff_hash diverges from request")

    matches = current == expected
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": request.get("run_id"),
        "reviewed_diff_hash": expected,
        "current_diff_hash": current,
        "matches": matches,
        "status": read_status(run_dir),
        "worktree": str(worktree),
        "base_commit": base_commit,
    }


def _decision_ready_locked(run_dir: Path) -> bool:
    """
    Return True when a validated approve decision is present.

    Must be called while holding the run-scoped lock. Success is derived only
    from the decision artifact (never from a bare status string).
    """
    if not (run_dir / DECISION_FILENAME).is_file():
        return False
    try:
        validate_decision_matches_request(run_dir)
    except ApprovalError:
        return False
    if read_status(run_dir) != STATUS_HUMAN_APPROVED:
        write_status(run_dir, STATUS_HUMAN_APPROVED)
    return True


def wait_for_decision(run_dir: Path, timeout_sec: int, poll_interval: float = 1.0) -> bool:
    """
    Block until a valid decision appears or timeout.

    Poll and timeout cleanup both take the run-scoped lock so a concurrent
    callback claim cannot race status observation. Under the lock, success is
    True only for a fully validated decision (``_decision_ready_locked`` may
    promote status to ``HUMAN_APPROVED``). On timeout without a valid decision,
    return False and leave the exact status untouched — never write
    ``AWAITING_HUMAN_APPROVAL`` over ``BLOCKED``, ``HUMAN_APPROVED``, or other
    states (and never invent approval).
    """
    run_dir = Path(run_dir)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
            if _decision_ready_locked(run_dir):
                return True
        time.sleep(poll_interval)

    # Timeout: observe under the same lock as claim publication; do not rewrite.
    with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
        return _decision_ready_locked(run_dir)


def list_pending_notifications(runs_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    runs_root = Path(runs_root)
    pending: list[tuple[Path, dict[str, Any]]] = []
    if not runs_root.is_dir():
        return pending
    # Discover only recognized run layouts. The state root also contains
    # worktrees, whose repository contents must never be interpreted as outbox.
    patterns = (
        f"*/{NOTIFY_FILENAME}",
        f"runs/*/{NOTIFY_FILENAME}",
        f"projects/*/runs/*/{NOTIFY_FILENAME}",
    )
    notify_paths = {path for pattern in patterns for path in runs_root.glob(pattern)}
    for notify_path in sorted(notify_paths):
        child = notify_path.parent
        try:
            payload = read_json(notify_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("sent_at"):
            continue
        pending.append((child, payload))
    return pending


def mark_notification_sent(
    run_dir: Path,
    expected_notification_id: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Mark only the exact outbox item that was sent; never consume a replacement."""
    run_dir = Path(run_dir)
    path = run_dir / NOTIFY_FILENAME
    with run_scoped_lock(run_dir, lock_name=LOCK_FILENAME):
        payload = read_json(path)
        if not expected_notification_id or payload.get("notification_id") != expected_notification_id:
            return False
        payload["sent_at"] = utc_now_iso()
        if extra:
            payload.update(extra)
        atomic_write_json(path, payload)
        return True
