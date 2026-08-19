"""Immutable Engine-N control adapter captured from the run base commit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .atomic import atomic_write_bytes, atomic_write_json, exclusive_write_json, read_json
from .profile import (
    PROFILE_RELATIVE_PATH,
    ProfileError,
    ProjectProfile,
    load_project_profile,
    parse_project_profile_text,
)


CONTROL_ADAPTER_DIRNAME = "control-adapter"
CONTROL_ADAPTER_FILES = "files"
CONTROL_ADAPTER_MANIFEST = "manifest.json"
CANDIDATE_PROFILE_FILENAME = "candidate-profile.json"
CANDIDATE_PROFILE_FLAG = "--allow-candidate-profile"
AUTHORIZATION_SCHEMA_VERSION = 1
CONTROL_ADAPTER_SCHEMA_VERSION = 1
CANDIDATE_PROFILE_SCHEMA_VERSION = 1
PROFILE_RELATIVE = str(PROFILE_RELATIVE_PATH)
UNAUTHORIZED_PROFILE_MESSAGE = (
    "executor modified .agent-loop/project.toml; resume settings must remain immutable"
)
PROFILE_CHANGED_AFTER_RUN = "project profile changed after run creation"
CANDIDATE_PROFILE_INVALID = "candidate project profile is invalid"
AUTHORIZATION_TAMPERED = "candidate profile authorization is missing or tampered"
CONTROL_ADAPTER_TAMPERED = "frozen control adapter diverges from run metadata"
CANDIDATE_SNAPSHOT_DIVERGED = (
    "candidate project profile diverges from the reviewed snapshot"
)
_GIT_MODE = {"100644", "100755", "120000"}
_COMMIT = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_SHELL_INTERPRETERS = {"bash", "sh", "dash"}
_PYTHON_VALUE_OPTIONS = {"-W", "-X", "-Q", "--check-hash-based-pycs"}
_SHELL_FILE_OPTIONS = {"--init-file", "--rcfile"}
_SCRIPT_SUFFIXES = {".sh", ".bash", ".py"}
MISSING_BASE_ENTRYPOINT = "configured adapter entrypoint is missing from the base commit"
MISSING_FROZEN_ENTRYPOINT = "frozen adapter entrypoint is missing"
_AUTHORIZATION_FIELDS = {
    "schema_version",
    "allowed",
    "origin",
    "flag",
    "base_commit",
}


class UnauthorizedProfileChange(ValueError):
    """A candidate changed the project profile without an explicit CLI grant."""


class ControlAdapterError(ValueError):
    """Frozen control adapter capture, authorization, or candidate binding failed."""


def control_adapter_root(run_dir: Path) -> Path:
    return Path(run_dir) / CONTROL_ADAPTER_DIRNAME


def control_adapter_files(run_dir: Path) -> Path:
    return control_adapter_root(run_dir) / CONTROL_ADAPTER_FILES


def control_adapter_manifest_path(run_dir: Path) -> Path:
    return control_adapter_root(run_dir) / CONTROL_ADAPTER_MANIFEST


def _safe_relative(value: str) -> str:
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value.startswith(".git/")
        or "\x00" in value
    ):
        raise ControlAdapterError(f"unsafe adapter path: {value!r}")
    return path.as_posix()


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ControlAdapterError(f"cannot execute git {' '.join(args)}") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        message = detail[0][:300] if detail else f"git {' '.join(args)} failed"
        raise ControlAdapterError(message)
    return completed.stdout


def _validate_commit(commit: str) -> str:
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise ControlAdapterError("base commit must be a full Git object name")
    return commit


def read_git_blob(repo: Path, commit: str, relative: str) -> tuple[str, bytes] | None:
    """Return ``(git_mode, bytes)`` for a blob at ``commit:relative``, or None."""
    repo = Path(repo).resolve()
    commit = _validate_commit(commit)
    relative = _safe_relative(relative)
    probe = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-t", f"{commit}:{relative}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        return None
    kind = probe.stdout.decode("utf-8", errors="replace").strip()
    if kind != "blob":
        return None
    listing = _git(repo, "ls-tree", "-z", commit, "--", relative)
    if not listing:
        return None
    raw = listing.split(b"\0", 1)[0]
    meta, _sep, path = raw.partition(b"\t")
    if path.decode("utf-8", errors="surrogateescape") != relative:
        return None
    parts = meta.decode("ascii", errors="replace").split()
    if len(parts) < 2:
        raise ControlAdapterError(f"cannot inspect git blob: {relative}")
    mode = parts[0]
    if mode not in _GIT_MODE:
        raise ControlAdapterError(f"unsupported git mode for {relative}: {mode}")
    content = _git(repo, "cat-file", "blob", f"{commit}:{relative}")
    return mode, content


def _interpreter_kind(value: str) -> str | None:
    name = Path(value).name
    if name in _SHELL_INTERPRETERS:
        return "shell"
    if name == "python" or name.startswith("python3"):
        return "python"
    return None


def _python_option_is_code_or_module(arg: str) -> bool:
    if arg in {"-c", "-m"}:
        return True
    return bool(
        arg.startswith("-")
        and not arg.startswith("--")
        and any(letter in arg[1:] for letter in "cm")
    )


def _has_inline_code_or_module(command: Sequence[str], kind: str) -> bool:
    """True when argv uses -c/-m, so later paths are data rather than a gate file."""
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "--" or arg == "-" or not arg.startswith("-"):
            return False
        if kind == "python":
            if _python_option_is_code_or_module(arg):
                return True
            if arg in _PYTHON_VALUE_OPTIONS:
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("--"):
            if arg in _SHELL_FILE_OPTIONS:
                index += 2
                continue
            index += 1
            continue
        letters = arg[1:]
        consumed = 1
        for offset, letter in enumerate(letters):
            if letter == "c":
                return True
            if letter in {"o", "O"}:
                if not letters[offset + 1 :]:
                    consumed = 2
                break
        index += consumed
    return False


def _script_after_interpreter(
    command: Sequence[str], kind: str
) -> tuple[int, str] | None:
    """Skip interpreter flags; do not treat -c/-m payloads as file entrypoints."""
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "--":
            index += 1
            break
        if arg == "-" or not arg.startswith("-"):
            break
        if kind == "python":
            if _python_option_is_code_or_module(arg):
                return None
            if arg in _PYTHON_VALUE_OPTIONS:
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("--"):
            if arg in _SHELL_FILE_OPTIONS:
                index += 2
                continue
            index += 1
            continue
        letters = arg[1:]
        consumed = 1
        for offset, letter in enumerate(letters):
            if letter == "c":
                return None
            if letter in {"o", "O"}:
                if not letters[offset + 1 :]:
                    consumed = 2
                break
        index += consumed
    if index >= len(command):
        return None
    return index, command[index]


def _fallback_script_index(command: Sequence[str]) -> tuple[int, str] | None:
    """Locate a repo-relative script if flag skipping missed the operand."""
    for index, arg in enumerate(command[1:], start=1):
        if arg.startswith("-"):
            continue
        relative = _repo_relative_script(arg)
        if relative is None:
            continue
        suffix = Path(relative).suffix.lower()
        if suffix in _SCRIPT_SUFFIXES or relative.endswith(".sh"):
            return index, relative
    return None


def _repo_relative_script(value: str) -> str | None:
    path = Path(value)
    if path.is_absolute() or not value:
        return None
    parts = tuple(part for part in path.parts if part not in {".", ""})
    if not parts or ".." in parts:
        return None
    return Path(*parts).as_posix()


def command_entrypoint_index(command: Sequence[str]) -> tuple[int, str] | None:
    """Return ``(argv_index, repo-relative script)`` when the argv names a gate."""
    if not command:
        return None
    first = command[0]
    kind = _interpreter_kind(first)
    if kind is not None:
        if _has_inline_code_or_module(command, kind):
            return None
        found = _script_after_interpreter(command, kind)
        if found is None:
            found = _fallback_script_index(command)
        if found is None:
            return None
        index, candidate = found
    elif Path(first).is_absolute() or ("/" not in first and not first.startswith(".")):
        return None
    else:
        index = 0
        candidate = first
    relative = _repo_relative_script(candidate)
    if relative is None:
        return None
    return index, relative


def command_entrypoint(command: Sequence[str]) -> str | None:
    """Return the repository-relative gate/hook script, if the argv names one."""
    found = command_entrypoint_index(command)
    return None if found is None else found[1]


def _python_uses_module_flag(command: Sequence[str]) -> bool:
    """True when argv is ``python … -m MODULE``, including clustered ``-Bm``."""
    if not command or _interpreter_kind(command[0]) != "python":
        return False
    if not _has_inline_code_or_module(command, "python"):
        return False
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "--" or arg == "-" or not arg.startswith("-"):
            return False
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name in _PYTHON_VALUE_OPTIONS and "=" not in arg:
                index += 2
                continue
            index += 1
            continue
        if arg in _PYTHON_VALUE_OPTIONS:
            index += 2
            continue
        if arg == "-m" or (arg.startswith("-m") and not arg.startswith("--")):
            return True
        if _python_option_is_code_or_module(arg):
            letters = arg[1:]
            c_pos = letters.find("c")
            m_pos = letters.find("m")
            if c_pos == -1:
                return m_pos != -1
            if m_pos == -1:
                return False
            return m_pos < c_pos
        index += 1
    return False


def _python_unsafe_path_disabled(command: Sequence[str]) -> bool:
    """True when argv already has ``-P`` or ``-I`` before ``-m``/``-c``."""
    if not command or _interpreter_kind(command[0]) != "python":
        return False
    index = 1
    while index < len(command):
        arg = command[index]
        if arg == "--" or arg == "-" or not arg.startswith("-"):
            return False
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name in _PYTHON_VALUE_OPTIONS and "=" not in arg:
                index += 2
                continue
            index += 1
            continue
        if arg in _PYTHON_VALUE_OPTIONS:
            index += 2
            continue
        if arg in {"-P", "-I"}:
            return True
        if arg.startswith("-") and not arg.startswith("--"):
            letters = arg[1:]
            limit = len(letters)
            for pos, letter in enumerate(letters):
                if letter in "cm":
                    limit = pos
                    break
            prefix = letters[:limit]
            if "I" in prefix or "P" in prefix:
                return True
            if limit < len(letters):
                return False
        index += 1
    return False


def isolate_python_module_argv(command: Sequence[str]) -> list[str]:
    """Keep cwd as the test object without letting it supply ``python -m``."""
    rewritten = list(command)
    if not _python_uses_module_flag(rewritten):
        return rewritten
    if _python_unsafe_path_disabled(rewritten):
        return rewritten
    return [rewritten[0], "-P", *rewritten[1:]]


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _instruction_paths(profile: ProjectProfile, phase: str) -> tuple[str, ...]:
    configured = (
        profile.executor_instructions if phase == "executor" else profile.reviewer_instructions
    )
    paths = list(configured)
    conventional = f".agent-loop/{phase}.md"
    if conventional not in paths:
        paths.append(conventional)
    return tuple(paths)


def _profile_public_dict(profile: ProjectProfile) -> dict[str, Any]:
    payload = profile.public_dict()
    payload["profile_path"] = None
    return payload


def capture_control_adapter(
    repo: Path | str,
    base_commit: str,
    *,
    missing_policy: str = "allow",
) -> dict[str, Any]:
    """Snapshot profile, instruction files, and gate entrypoints from the base commit."""
    from .profile import load_project_profile_from_git

    repo_path = Path(repo).resolve()
    base_commit = _validate_commit(base_commit)
    profile = load_project_profile_from_git(
        repo_path, base_commit, missing_policy=missing_policy
    )
    profile_blob = read_git_blob(repo_path, base_commit, PROFILE_RELATIVE)
    files: dict[str, dict[str, Any]] = {}
    if profile_blob is not None:
        mode, content = profile_blob
        if mode == "120000":
            raise ControlAdapterError("project profile must be a regular non-symlink file")
        files[PROFILE_RELATIVE] = {
            "mode": mode,
            "sha256": _sha256_bytes(content),
            "content": content,
        }
    instructions: dict[str, list[dict[str, str]]] = {"executor": [], "reviewer": []}
    for phase in ("executor", "reviewer"):
        for relative in _instruction_paths(profile, phase):
            blob = read_git_blob(repo_path, base_commit, relative)
            if blob is None:
                if relative in (
                    profile.executor_instructions
                    if phase == "executor"
                    else profile.reviewer_instructions
                ):
                    raise ControlAdapterError(
                        f"configured {phase} instruction is missing from the base commit: {relative}"
                    )
                continue
            mode, content = blob
            if mode == "120000":
                raise ControlAdapterError(
                    f"{phase} instruction must be a regular non-symlink file: {relative}"
                )
            try:
                text = content.decode("utf-8")
            except UnicodeError as exc:
                raise ControlAdapterError(
                    f"instruction file is not UTF-8: {relative}"
                ) from exc
            if len(content) > 256 * 1024:
                raise ControlAdapterError(f"instruction file too large: {relative}")
            digest = _sha256_bytes(content)
            files.setdefault(
                relative,
                {"mode": mode, "sha256": digest, "content": content},
            )
            instructions[phase].append({"path": relative, "sha256": digest, "text": text})
    entrypoints: list[dict[str, str]] = []
    commands: list[tuple[str, ...]] = []
    if profile.bootstrap_command:
        commands.append(profile.bootstrap_command)
    commands.extend(profile.validation_commands)
    seen: set[str] = set()
    for command in commands:
        relative = command_entrypoint(command)
        if relative is None or relative in seen:
            continue
        blob = read_git_blob(repo_path, base_commit, relative)
        if blob is None:
            raise ControlAdapterError(f"{MISSING_BASE_ENTRYPOINT}: {relative}")
        mode, content = blob
        if mode == "120000":
            raise ControlAdapterError(
                f"adapter entrypoint must be a regular non-symlink file: {relative}"
            )
        digest = _sha256_bytes(content)
        files.setdefault(relative, {"mode": mode, "sha256": digest, "content": content})
        entrypoints.append({"path": relative, "sha256": digest, "mode": mode})
        seen.add(relative)
    profile_text = ""
    if PROFILE_RELATIVE in files:
        profile_text = files[PROFILE_RELATIVE]["content"].decode("utf-8")
    return {
        "schema_version": CONTROL_ADAPTER_SCHEMA_VERSION,
        "base_commit": base_commit,
        "profile": profile,
        "profile_text": profile_text,
        "profile_sha256": _sha256_text(profile_text) if profile_text else None,
        "instructions": instructions,
        "entrypoints": entrypoints,
        "files": files,
    }


def _file_mode(git_mode: str) -> int:
    return 0o755 if git_mode == "100755" else 0o644


def materialize_control_adapter(
    run_dir: Path,
    capture: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    """Write captured adapter bytes once under the run directory."""
    run_dir = Path(run_dir)
    root = control_adapter_root(run_dir)
    files_root = control_adapter_files(run_dir)
    files_root.mkdir(parents=True, exist_ok=True)
    stored_files: list[dict[str, str]] = []
    for relative, meta in sorted(capture["files"].items()):
        destination = files_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            destination,
            meta["content"],
            mode=_file_mode(str(meta["mode"])),
        )
        stored_files.append(
            {
                "path": relative,
                "sha256": str(meta["sha256"]),
                "mode": str(meta["mode"]),
            }
        )
    instructions = {
        phase: [
            {"path": item["path"], "sha256": item["sha256"]}
            for item in capture["instructions"][phase]
        ]
        for phase in ("executor", "reviewer")
    }
    manifest = {
        "schema_version": CONTROL_ADAPTER_SCHEMA_VERSION,
        "base_commit": capture["base_commit"],
        "profile_sha256": capture["profile_sha256"],
        "missing_profile": capture["profile"].missing_profile,
        "authorization": dict(authorization),
        "instructions": instructions,
        "entrypoints": list(capture["entrypoints"]),
        "files": stored_files,
    }
    published = exclusive_write_json(control_adapter_manifest_path(run_dir), manifest)
    if not published:
        existing = read_json(control_adapter_manifest_path(run_dir))
        if existing != manifest:
            raise ControlAdapterError("control adapter already captured differently")
    os.chmod(root, 0o700)
    return manifest


def build_authorization(
    *,
    allowed: bool,
    base_commit: str,
    origin: str = "cli",
) -> dict[str, Any]:
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "allowed": bool(allowed),
        "origin": origin,
        "flag": CANDIDATE_PROFILE_FLAG if allowed else None,
        "base_commit": _validate_commit(base_commit),
    }


def validate_authorization(
    value: Any,
    *,
    base_commit: str,
    required: bool = False,
) -> dict[str, Any] | None:
    if value is None:
        if required:
            raise ControlAdapterError(AUTHORIZATION_TAMPERED)
        return None
    if not isinstance(value, dict) or set(value) != _AUTHORIZATION_FIELDS:
        raise ControlAdapterError(AUTHORIZATION_TAMPERED)
    if (
        value.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION
        or type(value.get("allowed")) is not bool
        or value.get("origin") != "cli"
        or value.get("base_commit") != base_commit
    ):
        raise ControlAdapterError(AUTHORIZATION_TAMPERED)
    flag = value.get("flag")
    if value["allowed"]:
        if flag != CANDIDATE_PROFILE_FLAG:
            raise ControlAdapterError(AUTHORIZATION_TAMPERED)
    elif flag is not None:
        raise ControlAdapterError(AUTHORIZATION_TAMPERED)
    return value


def candidate_profile_allowed(metadata: Mapping[str, Any]) -> bool:
    authorization = validate_authorization(
        metadata.get("candidate_profile_authorization"),
        base_commit=str(metadata.get("base_commit", "")),
    )
    return bool(authorization and authorization["allowed"])


def load_control_profile(run_dir: Path, worktree: Path) -> ProjectProfile:
    """Return the frozen control profile, never the live candidate."""
    captured = control_adapter_files(run_dir) / PROFILE_RELATIVE_PATH
    try:
        info = captured.lstat()
    except OSError:
        info = None
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ControlAdapterError("frozen project profile must be a regular non-symlink file")
        return parse_project_profile_text(
            captured.read_text(encoding="utf-8"), path=captured
        )
    manifest_path = control_adapter_manifest_path(run_dir)
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        missing = manifest.get("missing_profile", "allow")
        if missing not in {"allow", "deny"}:
            raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
        return ProjectProfile(missing_profile=missing)
    return load_project_profile(worktree)


def load_frozen_instruction_text(run_dir: Path, phase: str) -> str:
    if phase not in {"executor", "reviewer"}:
        raise ControlAdapterError(f"unknown instruction phase: {phase}")
    manifest_path = control_adapter_manifest_path(run_dir)
    if not manifest_path.is_file():
        from .runstate import load_run_metadata

        metadata = load_run_metadata(run_dir)
        capture = capture_control_adapter(
            str(metadata["repo"]),
            str(metadata["base_commit"]),
            missing_policy="allow",
        )
        return "\n\n".join(
            f"Additional tracked project instructions from {item['path']}:\n{item['text']}"
            for item in capture["instructions"][phase]
        )
    manifest = read_json(manifest_path)
    files_root = control_adapter_files(run_dir).resolve()
    configured = manifest.get("instructions", {}).get(phase, [])
    if not isinstance(configured, list):
        raise ControlAdapterError("frozen instruction manifest is invalid")
    sections: list[str] = []
    for item in configured:
        if not isinstance(item, dict) or "path" not in item or "sha256" not in item:
            raise ControlAdapterError("frozen instruction entry is invalid")
        relative = _safe_relative(str(item["path"]))
        path = (files_root / relative).resolve()
        if not path.is_relative_to(files_root) or path.is_symlink() or not path.is_file():
            raise ControlAdapterError(
                f"frozen {phase} instruction is missing or unsafe: {relative}"
            )
        text = path.read_text(encoding="utf-8")
        if _sha256_text(text) != item["sha256"]:
            raise ControlAdapterError(
                f"frozen {phase} instruction was tampered: {relative}"
            )
        sections.append(f"Additional tracked project instructions from {relative}:\n{text}")
    return "\n\n".join(sections)


def _frozen_entrypoint_digest(run_dir: Path, relative: str) -> str | None:
    manifest_path = control_adapter_manifest_path(run_dir)
    if not manifest_path.is_file():
        return None
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED) from exc
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
    for item in listed:
        if not isinstance(item, dict):
            continue
        if item.get("path") == relative and isinstance(item.get("sha256"), str):
            return item["sha256"]
    return None


def _read_base_entrypoint_blob(
    run_dir: Path,
    relative: str,
    *,
    repo: Path | str | None = None,
    base_commit: str | None = None,
) -> tuple[str, bytes] | None:
    if repo is None or base_commit is None:
        from .runstate import RunStateError, load_run_metadata

        try:
            metadata = load_run_metadata(run_dir)
        except (OSError, ValueError, RunStateError):
            metadata = None
        if isinstance(metadata, dict):
            repo = repo or metadata.get("repo")
            base_commit = base_commit or metadata.get("base_commit")
    if not repo or not isinstance(base_commit, str):
        return None
    try:
        return read_git_blob(Path(repo), base_commit, relative)
    except ControlAdapterError:
        return None


def rewrite_frozen_entrypoint(
    command: Sequence[str],
    run_dir: Path,
    *,
    repo: Path | str | None = None,
    base_commit: str | None = None,
) -> list[str]:
    """Execute the captured gate/hook script, not a candidate replacement.

    Identified repo-relative entrypoints never fall back to the worktree path.
    Trusted bytes come from the materialized adapter or the Git base snapshot.
    Missing snapshots fail closed instead of running a candidate file.
    ``python -m`` is not a file entrypoint; ``-P`` is inserted so the named
    module cannot be loaded from the candidate worktree cwd.
    """
    rewritten = list(command)
    found = command_entrypoint_index(rewritten)
    if found is None:
        return isolate_python_module_argv(rewritten)
    index, relative = found
    files_root = control_adapter_files(run_dir).resolve()
    frozen = files_root / relative
    expected = _frozen_entrypoint_digest(run_dir, relative)
    content: bytes | None = None
    git_mode = "100644"
    if expected is not None:
        try:
            info = frozen.lstat()
        except OSError as exc:
            raise ControlAdapterError(f"{MISSING_FROZEN_ENTRYPOINT}: {relative}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ControlAdapterError(f"frozen entrypoint is unsafe: {relative}")
        resolved = frozen.resolve()
        if not resolved.is_relative_to(files_root):
            raise ControlAdapterError(f"frozen entrypoint is unsafe: {relative}")
        content = resolved.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected:
            raise ControlAdapterError(f"{MISSING_FROZEN_ENTRYPOINT}: {relative}")
        rewritten[index] = str(resolved)
        return rewritten
    blob = _read_base_entrypoint_blob(
        run_dir, relative, repo=repo, base_commit=base_commit
    )
    if blob is None:
        raise ControlAdapterError(f"{MISSING_FROZEN_ENTRYPOINT}: {relative}")
    git_mode, content = blob
    if git_mode == "120000":
        raise ControlAdapterError(f"frozen entrypoint is unsafe: {relative}")
    destination = files_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(destination, content, mode=_file_mode(git_mode))
    resolved = destination.resolve()
    if not resolved.is_relative_to(files_root) or resolved.is_symlink():
        raise ControlAdapterError(f"frozen entrypoint is unsafe: {relative}")
    rewritten[index] = str(resolved)
    return rewritten


def profile_path_changed(worktree: Path, base_commit: str) -> bool:
    worktree = Path(worktree)
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "diff",
            "--quiet",
            base_commit,
            "--",
            PROFILE_RELATIVE,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if diff.returncode != 0:
        return True
    untracked = subprocess.check_output(
        [
            "git",
            "-C",
            str(worktree),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            PROFILE_RELATIVE,
        ]
    )
    return bool(untracked)


def _public_profile(profile: ProjectProfile) -> dict[str, Any]:
    payload = profile.public_dict()
    payload["profile_path"] = None
    return payload


def _load_live_or_missing_profile(worktree: Path) -> ProjectProfile:
    return load_project_profile(worktree)


def _candidate_bytes(worktree: Path) -> bytes | None:
    path = Path(worktree) / PROFILE_RELATIVE_PATH
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ControlAdapterError("candidate project profile must be a regular non-symlink file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def parse_candidate_profile(worktree: Path) -> tuple[ProjectProfile | None, str | None]:
    raw = _candidate_bytes(worktree)
    if raw is None:
        return None, None
    try:
        text = raw.decode("utf-8")
        profile = parse_project_profile_text(text, path=Path(worktree) / PROFILE_RELATIVE_PATH)
    except (UnicodeError, ProfileError) as exc:
        raise ControlAdapterError(f"{CANDIDATE_PROFILE_INVALID}: {exc}") from exc
    return profile, _sha256_bytes(raw)


def record_candidate_profile(
    run_dir: Path,
    *,
    profile: ProjectProfile | None,
    sha256: str | None,
    differs: bool,
) -> dict[str, Any]:
    payload = {
        "schema_version": CANDIDATE_PROFILE_SCHEMA_VERSION,
        "differs_from_frozen": differs,
        "sha256": sha256,
        "profile": None if profile is None else _public_profile(profile),
    }
    atomic_write_json(Path(run_dir) / CANDIDATE_PROFILE_FILENAME, payload)
    return payload


def verify_control_adapter_binding(run_dir: Path, metadata: Mapping[str, Any]) -> None:
    recorded = metadata.get("control_adapter")
    manifest_path = control_adapter_manifest_path(run_dir)
    if recorded is None and not manifest_path.is_file():
        return
    if not isinstance(recorded, dict) or not manifest_path.is_file():
        raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError) as exc:
        raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED) from exc
    expected = {
        "schema_version": CONTROL_ADAPTER_SCHEMA_VERSION,
        "base_commit": metadata.get("base_commit"),
        "profile_sha256": recorded.get("profile_sha256"),
        "manifest_sha256": recorded.get("manifest_sha256"),
    }
    actual_manifest_hash = _sha256_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    if (
        recorded.get("schema_version") != CONTROL_ADAPTER_SCHEMA_VERSION
        or recorded.get("base_commit") != metadata.get("base_commit")
        or manifest.get("base_commit") != metadata.get("base_commit")
        or manifest.get("profile_sha256") != recorded.get("profile_sha256")
        or actual_manifest_hash != recorded.get("manifest_sha256")
        or expected["base_commit"] != manifest.get("base_commit")
        or manifest.get("authorization")
        != metadata.get("candidate_profile_authorization")
    ):
        raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
    files_root = control_adapter_files(run_dir).resolve()
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
    for item in listed:
        if not isinstance(item, dict):
            raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
        relative = _safe_relative(str(item.get("path", "")))
        path = (files_root / relative).resolve()
        if not path.is_relative_to(files_root) or path.is_symlink() or not path.is_file():
            raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            raise ControlAdapterError(CONTROL_ADAPTER_TAMPERED)


def control_adapter_metadata(capture: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return {
        "schema_version": CONTROL_ADAPTER_SCHEMA_VERSION,
        "base_commit": capture["base_commit"],
        "profile_sha256": capture["profile_sha256"],
        "manifest_sha256": _sha256_text(encoded),
    }


def accept_candidate_profile(run_dir: Path, worktree: Path, base_commit: str) -> dict[str, Any]:
    """Validate an authorized candidate profile without activating it."""
    from .runstate import load_run_metadata

    metadata = load_run_metadata(run_dir)
    verify_control_adapter_binding(run_dir, metadata)
    authorization = validate_authorization(
        metadata.get("candidate_profile_authorization"),
        base_commit=str(metadata.get("base_commit", "")),
        required=False,
    )
    changed = profile_path_changed(worktree, base_commit)
    frozen = metadata.get("profile")
    if not isinstance(frozen, dict):
        raise ControlAdapterError("frozen project profile missing from run metadata")
    frozen_cmp = dict(frozen)
    frozen_cmp["profile_path"] = None
    if not changed:
        live = _load_live_or_missing_profile(worktree)
        live_cmp = _public_profile(live)
        if live_cmp != frozen_cmp:
            raise ControlAdapterError(PROFILE_CHANGED_AFTER_RUN)
        return record_candidate_profile(
            run_dir, profile=live, sha256=None, differs=False
        )
    if not (authorization and authorization["allowed"]):
        raise UnauthorizedProfileChange(UNAUTHORIZED_PROFILE_MESSAGE)
    candidate, digest = parse_candidate_profile(worktree)
    if candidate is None:
        # Authorized deletion: remaining control still uses the frozen profile.
        return record_candidate_profile(
            run_dir, profile=None, sha256=None, differs=True
        )
    return record_candidate_profile(
        run_dir, profile=candidate, sha256=digest, differs=True
    )


def evaluate_live_profile(run_dir: Path, worktree: Path, metadata: Mapping[str, Any]) -> None:
    """Resume/verify binding: frozen control stays in metadata; live may be candidate."""
    verify_control_adapter_binding(run_dir, metadata)
    allowed = candidate_profile_allowed(metadata)
    frozen = dict(metadata["profile"])
    frozen["profile_path"] = None
    changed = profile_path_changed(worktree, str(metadata["base_commit"]))
    if not changed:
        try:
            live = _public_profile(load_project_profile(worktree))
        except ProfileError as exc:
            raise ControlAdapterError(f"current project profile is invalid: {exc}") from exc
        if live != frozen:
            raise ControlAdapterError(PROFILE_CHANGED_AFTER_RUN)
        return
    if not allowed:
        raise ControlAdapterError(PROFILE_CHANGED_AFTER_RUN)
    parse_candidate_profile(worktree)


def snapshot_profile_changed(manifest: Mapping[str, Any]) -> bool:
    for entry in manifest.get("entries", []):
        if isinstance(entry, dict) and entry.get("path") == PROFILE_RELATIVE:
            return True
    return False


def assert_candidate_transport(
    run_dir: Path,
    metadata: Mapping[str, Any],
    manifest: Mapping[str, Any],
    worktree: Path,
) -> None:
    """Integration may carry a candidate profile only with matching authorization."""
    verify_control_adapter_binding(run_dir, metadata)
    allowed = candidate_profile_allowed(metadata)
    changed = snapshot_profile_changed(manifest)
    if not changed:
        return
    if not allowed:
        raise ControlAdapterError(AUTHORIZATION_TAMPERED)
    recorded_path = Path(run_dir) / CANDIDATE_PROFILE_FILENAME
    if not recorded_path.is_file():
        raise ControlAdapterError(CANDIDATE_SNAPSHOT_DIVERGED)
    candidate, digest = parse_candidate_profile(worktree)
    recorded = read_json(recorded_path)
    if (
        recorded.get("schema_version") != CANDIDATE_PROFILE_SCHEMA_VERSION
        or recorded.get("differs_from_frozen") is not True
        or recorded.get("sha256") != digest
    ):
        raise ControlAdapterError(CANDIDATE_SNAPSHOT_DIVERGED)
    recorded_profile = recorded.get("profile")
    live_profile = None if candidate is None else _public_profile(candidate)
    if recorded_profile != live_profile:
        raise ControlAdapterError(CANDIDATE_SNAPSHOT_DIVERGED)
