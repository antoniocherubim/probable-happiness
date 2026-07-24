"""Async delivery job queue: bridge enqueues; worker executes Git delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from .approval import (
    DECISION_FILENAME,
    LOCK_FILENAME as APPROVAL_LOCK,
    STATUS_DELIVERING,
    STATUS_DELIVERY_FAILED,
    STATUS_HUMAN_APPROVED,
    STATUS_PUSHED,
    read_status,
    utc_now_iso,
    validate_decision_matches_request,
)
from .atomic import exclusive_write_json, read_json, run_scoped_lock

logger = logging.getLogger("agent_dx.delivery_job")

JOB_FILENAME = "delivery-job.json"
JOB_SCHEMA_VERSION = 1
JOB_STATUS_PENDING = "pending"

# Decision fields hashed into decision_content_hash (never callback_token).
_DECISION_HASH_KEYS = (
    "schema_version",
    "decision",
    "run_id",
    "diff_hash",
    "telegram_user_id",
    "telegram_chat_id",
    "request_created_at",
    "decided_at",
)


class DeliveryJobError(RuntimeError):
    """Delivery job is missing, divergent, or unsafe to process."""


def decision_content_hash(decision: dict[str, Any]) -> str:
    payload = {key: decision.get(key) for key in _DECISION_HASH_KEYS}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_job_id(
    *,
    run_id: str,
    decision_hash: str,
    reviewed_diff_hash: str,
    remote: str,
    branch: str,
) -> str:
    binding = {
        "branch": branch,
        "decision_content_hash": decision_hash,
        "remote": remote,
        "reviewed_diff_hash": reviewed_diff_hash,
        "run_id": run_id,
    }
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _frozen_push_delivery(metadata: dict[str, Any]) -> dict[str, Any] | None:
    frozen = metadata.get("delivery")
    if not isinstance(frozen, dict):
        return None
    if frozen.get("mode") != "push_branch":
        return None
    remote = frozen.get("remote")
    branch = frozen.get("branch")
    if not isinstance(remote, str) or not remote.strip():
        raise DeliveryJobError("frozen delivery remote missing")
    if not isinstance(branch, str) or not branch.strip():
        raise DeliveryJobError("frozen delivery branch missing")
    return frozen


def _read_run_metadata(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.is_file():
        raise DeliveryJobError("run.json missing")
    metadata = read_json(path)
    if not isinstance(metadata, dict):
        raise DeliveryJobError("run.json is not an object")
    return metadata


def build_job_document(
    *,
    run_id: str,
    decision: dict[str, Any],
    remote: str,
    branch: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    reviewed = str(decision.get("diff_hash") or "")
    if len(reviewed) < 32:
        raise DeliveryJobError("decision diff_hash missing")
    decision_hash = decision_content_hash(decision)
    job_id = compute_job_id(
        run_id=run_id,
        decision_hash=decision_hash,
        reviewed_diff_hash=reviewed,
        remote=remote,
        branch=branch,
    )
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "run_id": run_id,
        "reviewed_diff_hash": reviewed,
        "decision_content_hash": decision_hash,
        "remote": remote,
        "branch": branch,
        "created_at": created_at or utc_now_iso(),
        "status": JOB_STATUS_PENDING,
    }


def load_delivery_job(run_dir: Path) -> dict[str, Any] | None:
    path = Path(run_dir) / JOB_FILENAME
    if not path.is_file():
        return None
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise DeliveryJobError("delivery-job.json is not an object")
    return payload


def expected_job_binding(run_dir: Path) -> dict[str, Any]:
    """Build the binding that a valid job for this run must match."""
    metadata = _read_run_metadata(run_dir)
    frozen = _frozen_push_delivery(metadata)
    if frozen is None:
        raise DeliveryJobError("delivery is not push_branch")
    decision = validate_decision_matches_request(run_dir)
    run_id = str(decision.get("run_id") or metadata.get("run_id") or run_dir.name)
    return build_job_document(
        run_id=run_id,
        decision=decision,
        remote=str(frozen["remote"]),
        branch=str(frozen["branch"]),
        created_at="1970-01-01T00:00:00+00:00",  # ignored for binding compare
    )


def validate_job_document(job: dict[str, Any], expected: dict[str, Any]) -> None:
    if job.get("schema_version") != JOB_SCHEMA_VERSION:
        raise DeliveryJobError("delivery-job schema_version mismatch")
    if job.get("status") != JOB_STATUS_PENDING:
        raise DeliveryJobError("delivery-job status must remain pending")
    for key in (
        "job_id",
        "run_id",
        "reviewed_diff_hash",
        "decision_content_hash",
        "remote",
        "branch",
    ):
        if job.get(key) != expected.get(key):
            raise DeliveryJobError(f"delivery-job {key} diverges from approved binding")
    if not isinstance(job.get("created_at"), str) or not job["created_at"]:
        raise DeliveryJobError("delivery-job created_at missing")
    # Recompute job_id from stored fields to catch partial tampering.
    recomputed = compute_job_id(
        run_id=str(job["run_id"]),
        decision_hash=str(job["decision_content_hash"]),
        reviewed_diff_hash=str(job["reviewed_diff_hash"]),
        remote=str(job["remote"]),
        branch=str(job["branch"]),
    )
    if job.get("job_id") != recomputed:
        raise DeliveryJobError("delivery-job job_id is inconsistent")


def _ensure_delivery_job_locked(run_dir: Path) -> dict[str, Any]:
    """
    Create or validate delivery-job.json while holding ``.approval.lock``.

    Returns a result dict with keys:
      result: disabled | not_eligible | already_pushed | created | existing | blocked_divergent
      job: optional job document
    """
    run_dir = Path(run_dir)
    try:
        metadata = _read_run_metadata(run_dir)
    except DeliveryJobError as exc:
        return {"result": "not_eligible", "reason": str(exc), "job": None}

    frozen = _frozen_push_delivery(metadata)
    if frozen is None:
        return {"result": "disabled", "job": None}

    status = read_status(run_dir)
    if status == STATUS_PUSHED:
        existing = load_delivery_job(run_dir)
        return {"result": "already_pushed", "job": existing}

    if status not in {
        STATUS_HUMAN_APPROVED,
        STATUS_DELIVERING,
        STATUS_DELIVERY_FAILED,
    }:
        # Decision may exist before status repair; allow if decision validates.
        if not (run_dir / DECISION_FILENAME).is_file():
            return {"result": "not_eligible", "reason": f"status={status}", "job": None}

    try:
        decision = validate_decision_matches_request(run_dir)
    except Exception as exc:  # ApprovalError and JSON errors
        return {"result": "not_eligible", "reason": str(exc), "job": None}

    run_id = str(decision.get("run_id") or run_dir.name)
    expected = build_job_document(
        run_id=run_id,
        decision=decision,
        remote=str(frozen["remote"]),
        branch=str(frozen["branch"]),
    )
    # Drop placeholder created_at for exclusive publish of a fresh job.
    job_path = run_dir / JOB_FILENAME
    existing = load_delivery_job(run_dir)
    if existing is not None:
        try:
            validate_job_document(existing, expected)
        except DeliveryJobError as exc:
            logger.warning(
                "delivery job divergent run_id=%s branch=%s reason=%s",
                run_id,
                frozen["branch"],
                exc,
            )
            return {
                "result": "blocked_divergent",
                "reason": str(exc),
                "job": existing,
            }
        logger.info(
            "delivery job already exists run_id=%s job_id=%s branch=%s",
            run_id,
            existing.get("job_id", "")[:12],
            frozen["branch"],
        )
        return {"result": "existing", "job": existing}

    fresh = build_job_document(
        run_id=run_id,
        decision=decision,
        remote=str(frozen["remote"]),
        branch=str(frozen["branch"]),
    )
    if exclusive_write_json(job_path, fresh):
        logger.info(
            "delivery job created run_id=%s job_id=%s branch=%s",
            run_id,
            fresh["job_id"][:12],
            frozen["branch"],
        )
        return {"result": "created", "job": fresh}

    # Lost the race: reload and validate.
    existing = load_delivery_job(run_dir)
    if existing is None:
        raise DeliveryJobError("delivery-job publish raced and disappeared")
    try:
        validate_job_document(existing, expected)
    except DeliveryJobError as exc:
        return {
            "result": "blocked_divergent",
            "reason": str(exc),
            "job": existing,
        }
    logger.info(
        "delivery job already exists run_id=%s job_id=%s branch=%s",
        run_id,
        existing.get("job_id", "")[:12],
        frozen["branch"],
    )
    return {"result": "existing", "job": existing}


def ensure_delivery_job(
    run_dir: Path,
    *,
    approval_lock_held: bool = False,
) -> dict[str, Any]:
    """
    Idempotently publish ``delivery-job.json`` for an approved push_branch run.

    Must run under ``.approval.lock`` (acquired here unless already held).
    """
    run_dir = Path(run_dir)
    if approval_lock_held:
        return _ensure_delivery_job_locked(run_dir)
    with run_scoped_lock(run_dir, lock_name=APPROVAL_LOCK):
        return _ensure_delivery_job_locked(run_dir)


def callback_delivery_message(run_dir: Path) -> str:
    """Immediate Telegram answer text after a successful approval claim/replay."""
    run_dir = Path(run_dir)
    try:
        metadata = _read_run_metadata(run_dir)
    except DeliveryJobError:
        return "Aprovado."
    frozen = metadata.get("delivery")
    if not isinstance(frozen, dict) or frozen.get("mode") != "push_branch":
        return "Aprovado."

    status = read_status(run_dir)
    branch = str(frozen.get("branch") or "")
    delivery_path = run_dir / "delivery.json"
    delivery: dict[str, Any] | None = None
    if delivery_path.is_file():
        try:
            payload = read_json(delivery_path)
            if isinstance(payload, dict):
                delivery = payload
        except (OSError, ValueError, json.JSONDecodeError):
            delivery = None

    if status == STATUS_PUSHED or (delivery and delivery.get("status") == STATUS_PUSHED):
        published = branch or str((delivery or {}).get("branch") or "")
        if published:
            return f"Aprovado; branch {published} já publicada."
        return "Aprovado; branch já publicada."

    if status == STATUS_DELIVERY_FAILED or (
        delivery and delivery.get("status") == STATUS_DELIVERY_FAILED
    ):
        return "Aprovado; a entrega falhou anteriormente e a aprovação foi preservada."

    return "Aprovado; entrega enfileirada."


def project_state_from_run_dir(run_dir: Path) -> Path:
    run_dir = Path(run_dir).resolve()
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent
    return run_dir.parent


def discover_delivery_job_runs(project_state: Path) -> list[Path]:
    """Return run directories that contain a recognized delivery-job.json."""
    root = Path(project_state)
    if not root.is_dir():
        return []
    patterns = ("runs/*/delivery-job.json", "*/delivery-job.json", "delivery-job.json")
    found: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.name != JOB_FILENAME or not path.is_file():
                continue
            found.add(path.parent.resolve())
    return sorted(found)


def validate_job_for_worker(run_dir: Path) -> dict[str, Any]:
    """Revalidate decision + immutable job before Git delivery."""
    run_dir = Path(run_dir)
    ensure_result = ensure_delivery_job(run_dir)
    if ensure_result["result"] == "disabled":
        return {"eligible": False, "reason": "disabled", "job": None}
    if ensure_result["result"] == "already_pushed":
        return {"eligible": False, "reason": "already_pushed", "job": ensure_result.get("job")}
    if ensure_result["result"] == "blocked_divergent":
        raise DeliveryJobError(
            ensure_result.get("reason") or "delivery-job diverges from approved binding"
        )
    if ensure_result["result"] == "not_eligible":
        raise DeliveryJobError(ensure_result.get("reason") or "run is not eligible for delivery")
    job = ensure_result.get("job")
    if not isinstance(job, dict):
        raise DeliveryJobError("delivery-job missing after ensure")
    expected = expected_job_binding(run_dir)
    validate_job_document(job, expected)
    status = read_status(run_dir)
    if status not in {
        STATUS_HUMAN_APPROVED,
        STATUS_DELIVERING,
        STATUS_DELIVERY_FAILED,
        STATUS_PUSHED,
    }:
        raise DeliveryJobError(f"run status is not delivery-eligible: {status}")
    return {"eligible": True, "reason": "ok", "job": job}


def process_delivery_run(run_dir: Path) -> dict[str, Any]:
    """Validate job contract and execute ``deliver_run`` under delivery lock semantics."""
    from .delivery import DeliveryError, deliver_run

    run_dir = Path(run_dir).resolve()
    check = validate_job_for_worker(run_dir)
    if not check["eligible"]:
        logger.info(
            "delivery worker skip run=%s reason=%s",
            run_dir.name,
            check["reason"],
        )
        if check["reason"] == "already_pushed":
            delivery_path = run_dir / "delivery.json"
            if delivery_path.is_file():
                existing = read_json(delivery_path)
                if isinstance(existing, dict):
                    return existing
        return {
            "status": check["reason"],
            "run_id": run_dir.name,
            "job": check.get("job"),
        }

    job = check["job"]
    assert isinstance(job, dict)
    logger.info(
        "delivery worker started run=%s job_id=%s branch=%s",
        run_dir.name,
        str(job.get("job_id", ""))[:12],
        job.get("branch"),
    )
    try:
        result = deliver_run(run_dir)
    except DeliveryError as exc:
        logger.warning(
            "delivery worker failed run=%s job_id=%s reason=%s",
            run_dir.name,
            str(job.get("job_id", ""))[:12],
            exc,
        )
        raise
    logger.info(
        "delivery worker finished run=%s job_id=%s status=%s",
        run_dir.name,
        str(job.get("job_id", ""))[:12],
        result.get("status"),
    )
    return result


def run_delivery_worker(
    project_state: Path,
    *,
    once: bool = False,
    run_dir: Path | None = None,
    max_cycles: int | None = None,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """
    Process pending delivery jobs for one project state.

    ``once`` processes discovered jobs (or a single ``run_dir``) and returns.
    Continuous mode polls until ``max_cycles`` if set.
    """
    project_state = Path(project_state).resolve()
    cycles = 0
    processed: list[dict[str, Any]] = []

    while True:
        targets = [Path(run_dir).resolve()] if run_dir is not None else discover_delivery_job_runs(
            project_state
        )
        for target in targets:
            try:
                outcome = process_delivery_run(target)
                processed.append({"run_dir": str(target), "ok": True, "result": outcome})
            except Exception as exc:  # DeliveryError / DeliveryJobError / OSError
                processed.append(
                    {
                        "run_dir": str(target),
                        "ok": False,
                        "error": str(exc),
                    }
                )
        cycles += 1
        if once or run_dir is not None:
            break
        if max_cycles is not None and cycles >= max_cycles:
            break
        time.sleep(poll_interval)

    return {
        "project_state": str(project_state),
        "cycles": cycles,
        "processed": processed,
    }
