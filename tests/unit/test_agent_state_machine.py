"""DX-07 typed run-state machine and writer-centralization regressions."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "scripts" / "agents"
sys.path.insert(0, str(AGENTS))

from dx.atomic import atomic_write_text  # noqa: E402
from dx.state_machine import (  # noqa: E402
    TRANSITIONS,
    RunEvent,
    RunState,
    StateTransitionError,
    read_state,
    transition_run,
)


ALL_STATES = (None, *RunState)
ALL_EVENT_STATE_PAIRS = tuple(
    (event, state) for event in RunEvent for state in ALL_STATES
)

# Independent of TRANSITIONS: every documented run_blocked source, including
# missing status (startup-failure / interrupt before run_started).
DOCUMENTED_RUN_BLOCKED_SOURCES = frozenset(
    {
        None,
        RunState.EXECUTING,
        RunState.REVIEWING,
        RunState.CHANGES_REQUESTED,
        RunState.APPROVED,
        RunState.AWAITING_HUMAN_APPROVAL,
    }
)

_STATUS_PATH_NAMES = frozenset({"status", "STATUS_FILENAME"})
_PYTHON_WRITE_FUNCS = frozenset(
    {
        "atomic_write_text",
        "atomic_write_bytes",
        "atomic_write_json",
        "write_text",
        "write_bytes",
        "write",
        "open",
    }
)
# Destination-oriented mutators: second positional / dst-like kwargs matter.
_PYTHON_DEST_FUNCS = frozenset({"replace", "rename", "move"})
_PYTHON_PATH_KEYWORDS = frozenset({"path", "file", "filename", "target", "dest", "dst"})
_PYTHON_DEST_KEYWORDS = frozenset(
    {"dst", "dest", "destination", "target", "path", "file", "filename"}
)
_PYTHON_OPEN_WRITE_MODES = frozenset({"w", "a", "x", "r+", "w+", "a+", "x+", "wb", "ab", "xb"})
_SHELL_STATUS_WRITE_HELPERS = ("write_run_status",)
_SHELL_STATUS_ASSIGN = re.compile(
    r"""^\s*([A-Za-z_][A-Za-z0-9_]*)="""
    r"""(["']?)(?:\$\{?RUN_DIR\}?|\.?)/status\2\s*$"""
)
_SHELL_REDIRECT_OP = re.compile(r"(?:^|[^>])>{1,2}(?!>)")
# Destination-arg writers only. printf/echo/cat write status solely via
# redirects (handled separately); bare `cat "$RUN_DIR/status"` is a read and
# `echo "$RUN_DIR/status"` is path logging — not classified as writes.
_SHELL_DEST_WRITE_CMDS = re.compile(
    r"""(?:^|[;\s|&])(?:tee|mv|cp|install)\b"""
)
_SHELL_DD_OF = re.compile(
    r"""(?:^|[;\s|&])dd\b(?:\s+[^;\n|&]+)*?\sof=("[^"]*"|'[^']*'|[^\s;|&]+)"""
)


def _shell_mentions_status_destination(text: str, aliases: frozenset[str]) -> bool:
    if re.search(r"/status\b", text):
        return True
    if re.search(r"""["']?\$\{?RUN_DIR\}?["']?/status\b""", text):
        return True
    for alias in aliases:
        if re.search(rf"""["']?\$\{{?{re.escape(alias)}\}}?["']?""", text):
            return True
    return False


def _collect_shell_status_aliases(source: str) -> frozenset[str]:
    aliases: set[str] = set()
    for line in source.splitlines():
        code = line.split("#", 1)[0]
        for stmt in code.split(";"):
            match = _SHELL_STATUS_ASSIGN.match(stmt)
            if match:
                aliases.add(match.group(1))
    return frozenset(aliases)


def find_shell_status_write_violations(source: str) -> list[str]:
    """Line-oriented scan for shell redirects/helpers writing run status."""
    hits: list[str] = []
    aliases = _collect_shell_status_aliases(source)
    for helper in _SHELL_STATUS_WRITE_HELPERS:
        if helper in source:
            hits.append(f"helper:{helper}")
    for index, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if not code.strip():
            continue
        mentions_status = bool(re.search(r"/status\b", code)) or any(
            re.search(rf"""["']?\$\{{?{re.escape(alias)}\}}?["']?""", code)
            for alias in aliases
        )
        if not mentions_status:
            continue
        # Output redirects only (> / >>); input redirects are reads.
        if _SHELL_REDIRECT_OP.search(code):
            for match in _SHELL_REDIRECT_OP.finditer(code):
                tail = code[match.end() :]
                if _shell_mentions_status_destination(tail, aliases):
                    hits.append(f"redirect:line={index}")
                    break
        dd_match = _SHELL_DD_OF.search(code)
        if dd_match and _shell_mentions_status_destination(dd_match.group(1), aliases):
            hits.append(f"dd-of:line={index}")
        dest_cmd = _SHELL_DEST_WRITE_CMDS.search(code)
        if dest_cmd and _shell_mentions_status_destination(
            code[dest_cmd.start() :], aliases
        ):
            hits.append(f"write-cmd:line={index}")
    return hits


def _expr_mentions_status_path(
    node: ast.AST, aliases: frozenset[str] | None = None
) -> bool:
    """True when an expression resolves a path component named status."""
    known = _STATUS_PATH_NAMES if aliases is None else aliases | _STATUS_PATH_NAMES
    if isinstance(node, ast.Constant) and node.value == "status":
        return True
    if isinstance(node, ast.Name) and node.id in known:
        return True
    if isinstance(node, ast.Attribute) and node.attr in known:
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _expr_mentions_status_path(
            node.left, aliases
        ) or _expr_mentions_status_path(node.right, aliases)
    if isinstance(node, ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name in {"Path", "joinpath"} or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "joinpath"
        ):
            return any(_expr_mentions_status_path(arg, aliases) for arg in node.args)
        if isinstance(node.func, ast.Attribute):
            return _expr_mentions_status_path(node.func.value, aliases) or any(
                _expr_mentions_status_path(arg, aliases) for arg in node.args
            )
    if isinstance(node, ast.Subscript):
        return _expr_mentions_status_path(
            node.value, aliases
        ) or _expr_mentions_status_path(node.slice, aliases)
    return False


def _collect_python_status_aliases(tree: ast.AST) -> frozenset[str]:
    """Bind names assigned from expressions that mention the status path."""
    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            if value is None or not _expr_mentions_status_path(value, frozenset(aliases)):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return frozenset(aliases)


def _call_path_targets(node: ast.Call) -> list[ast.AST]:
    """Positional and keyword path-like arguments for write helpers."""
    targets: list[ast.AST] = []
    if node.args:
        targets.append(node.args[0])
    for keyword in node.keywords:
        if keyword.arg in _PYTHON_PATH_KEYWORDS:
            targets.append(keyword.value)
    return targets


def _call_dest_targets(node: ast.Call) -> list[ast.AST]:
    """Destination operands for rename/replace/move-style calls."""
    targets: list[ast.AST] = []
    if len(node.args) >= 2:
        targets.append(node.args[1])
    elif len(node.args) == 1 and isinstance(node.func, ast.Attribute):
        # temp.rename(status_path) / Path(...).replace(status_path)
        targets.append(node.args[0])
    for keyword in node.keywords:
        if keyword.arg in _PYTHON_DEST_KEYWORDS:
            targets.append(keyword.value)
    return targets


def _open_is_write(node: ast.Call) -> bool:
    """True when open(...) uses a write-capable mode (default is read-only)."""
    mode_node: ast.AST | None = None
    is_bound = isinstance(node.func, ast.Attribute) and node.func.attr == "open"
    if is_bound:
        if node.args:
            mode_node = node.args[0]
    elif len(node.args) >= 2:
        mode_node = node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
    if mode_node is None:
        return False
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        mode = mode_node.value
        if mode in _PYTHON_OPEN_WRITE_MODES or any(flag in mode for flag in "wax+"):
            return True
    return False


def find_python_status_write_violations(source: str) -> list[str]:
    """AST scan: any write whose target path mentions the status filename."""
    tree = ast.parse(source)
    aliases = _collect_python_status_aliases(tree)
    hits: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _PYTHON_WRITE_FUNCS:
                if name == "open":
                    targets = list(_call_path_targets(node))
                    # Bound Path.open: receiver is the path, mode is args[0].
                    if isinstance(func, ast.Attribute):
                        targets.insert(0, func.value)
                    if _open_is_write(node) and any(
                        _expr_mentions_status_path(target, aliases)
                        for target in targets
                    ):
                        hits.append(f"call:{name}:line={node.lineno}")
                elif any(
                    _expr_mentions_status_path(target, aliases)
                    for target in _call_path_targets(node)
                ):
                    hits.append(f"call:{name}:line={node.lineno}")
                elif isinstance(func, ast.Attribute) and _expr_mentions_status_path(
                    func.value, aliases
                ):
                    hits.append(f"method:{name}:line={node.lineno}")
            elif name in _PYTHON_DEST_FUNCS:
                # os.replace/os.rename/shutil.move, or Path.rename — not str.replace.
                allowed = False
                if isinstance(func, ast.Name):
                    allowed = True
                elif isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name) and func.value.id in {
                        "os",
                        "shutil",
                    }:
                        allowed = True
                    elif name == "rename":
                        allowed = True
                if allowed:
                    targets = list(_call_dest_targets(node))
                    if isinstance(func, ast.Attribute) and _expr_mentions_status_path(
                        func.value, aliases
                    ):
                        targets.append(func.value)
                    if any(
                        _expr_mentions_status_path(target, aliases)
                        for target in targets
                    ):
                        hits.append(f"call:{name}:line={node.lineno}")
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


NEGATIVE_PYTHON_STATUS_WRITES = (
    'atomic_write_text(run_dir / "status", "EXECUTING")\n',
    'atomic_write_text(run_path / STATUS_FILENAME, "BLOCKED")\n',
    'atomic_write_text(path=run_dir / "status", content="EXECUTING")\n',
    '(run_dir / "status").write_text("EXECUTING\\n", encoding="utf-8")\n',
    'Path(run_dir, "status").write_bytes(b"BLOCKED\\n")\n',
    'open(run_dir / "status", "w", encoding="utf-8").write("EXECUTING\\n")\n',
    'open(file=run_dir / "status", mode="w")\n',
    'status_path = run_dir / "status"\nstatus_path.write_text("EXECUTING\\n", encoding="utf-8")\n',
    'status_path = run_dir / "status"\natomic_write_text(path=status_path, content="BLOCKED")\n',
    'status_path = run_dir / "status"\nstatus_path.open("w")\n',
    'status_path = run_dir / "status"\nstatus_path.open(mode="w")\n',
    'status_path = run_dir / "status"\nos.replace(temp, status_path)\n',
    'status_path = run_dir / "status"\nos.rename(temp, status_path)\n',
    'status_path = run_dir / "status"\nshutil.move(temp, status_path)\n',
)

NEGATIVE_SHELL_STATUS_WRITES = (
    'printf "EXECUTING\\n" > "${RUN_DIR}/status"\n',
    'echo BLOCKED > "$RUN_DIR/status"\n',
    'cat > "$RUN_DIR/status" <<EOF\nBLOCKED\nEOF\n',
    'write_run_status "$RUN_DIR" EXECUTING\n',
    'mv /tmp/status.tmp "$RUN_DIR/status"\n',
    'status_path="$RUN_DIR/status"; printf "EXECUTING\\n" > "$status_path"\n',
    'status_path="$RUN_DIR/status"\necho BLOCKED > "$status_path"\n',
    'status_path="$RUN_DIR/status"\nmv /tmp/status.tmp "$status_path"\n',
    'dd if=/tmp/status.tmp of="$RUN_DIR/status"\n',
    'status_path="$RUN_DIR/status"\ndd if=/tmp/status.tmp of="$status_path"\n',
)

# Reads / path logging must not be classified as status writers.
POSITIVE_PYTHON_STATUS_READS = (
    'status_path = run_dir / "status"\nstatus_path.read_text(encoding="utf-8")\n',
    'status_path = run_dir / "status"\nopen(status_path, encoding="utf-8")\n',
    'status_path = run_dir / "status"\nstatus_path.open("r")\n',
)

POSITIVE_SHELL_STATUS_READS = (
    'cat "$RUN_DIR/status"\n',
    'echo "$RUN_DIR/status"\n',
    'printf "%s\\n" "$RUN_DIR/status"\n',
    'status_path="$RUN_DIR/status"\ncat "$status_path"\n',
)


def set_state_fixture(run_dir: Path, state: RunState | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status"
    if state is None:
        status_path.unlink(missing_ok=True)
    else:
        atomic_write_text(status_path, state.value)


@pytest.mark.parametrize(("event", "initial"), ALL_EVENT_STATE_PAIRS)
def test_complete_transition_matrix(
    tmp_path: Path,
    event: RunEvent,
    initial: RunState | None,
) -> None:
    run_dir = tmp_path / f"{event.value}-{initial.value if initial else 'empty'}"
    set_state_fixture(run_dir, initial)
    before = (run_dir / "status").read_bytes() if initial is not None else None
    spec = TRANSITIONS[event]
    allowed = initial in spec.sources or (
        initial == spec.target and spec.idempotent
    )

    if allowed:
        result = transition_run(run_dir, event)
        assert result.current == spec.target
        assert read_state(run_dir) == spec.target
        assert result.result == (
            "already_applied" if initial == spec.target else "applied"
        )
    else:
        with pytest.raises(StateTransitionError, match=f"event {event.value}"):
            transition_run(run_dir, event)
        assert read_state(run_dir) == initial
        if before is not None:
            assert (run_dir / "status").read_bytes() == before


def test_idempotent_replay_does_not_rewrite_status(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    first = transition_run(run_dir, RunEvent.RUN_STARTED)
    status_path = run_dir / "status"
    before = status_path.stat()
    content = status_path.read_bytes()

    replay = transition_run(run_dir, RunEvent.RUN_STARTED)
    after = status_path.stat()

    assert first.result == "applied"
    assert replay.result == "already_applied"
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert status_path.read_bytes() == content


def test_expected_state_conflict_is_compare_and_set_failure(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    transition_run(run_dir, RunEvent.RUN_STARTED)
    before = (run_dir / "status").read_bytes()

    with pytest.raises(StateTransitionError, match="expected"):
        transition_run(
            run_dir,
            RunEvent.REVIEW_STARTED,
            expected_states={RunState.CHANGES_REQUESTED},
        )

    assert read_state(run_dir) == RunState.EXECUTING
    assert (run_dir / "status").read_bytes() == before


def test_concurrent_conflicting_events_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    set_state_fixture(run_dir, RunState.REVIEWING)
    barrier = Barrier(2)

    def attempt(event: RunEvent) -> str:
        barrier.wait()
        try:
            return transition_run(run_dir, event).result
        except StateTransitionError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                attempt,
                (
                    RunEvent.REVIEW_APPROVED,
                    RunEvent.REVIEW_CHANGES_REQUESTED,
                ),
            )
        )

    assert sorted(outcomes) == ["applied", "conflict"]
    assert read_state(run_dir) in {
        RunState.APPROVED,
        RunState.CHANGES_REQUESTED,
    }


@pytest.mark.parametrize(
    "legacy",
    (RunState.DELIVERING, RunState.DELIVERY_FAILED, RunState.PUSHED),
)
@pytest.mark.parametrize("event", tuple(RunEvent))
def test_legacy_states_are_terminal(
    tmp_path: Path,
    legacy: RunState,
    event: RunEvent,
) -> None:
    run_dir = tmp_path / f"{legacy.value}-{event.value}"
    set_state_fixture(run_dir, legacy)

    with pytest.raises(StateTransitionError):
        transition_run(run_dir, event)

    assert read_state(run_dir) == legacy


def test_status_reader_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("EXECUTING\n", encoding="utf-8")
    symlink_run = tmp_path / "symlink-run"
    symlink_run.mkdir()
    (symlink_run / "status").symlink_to(target)
    with pytest.raises(StateTransitionError, match="non-symlink"):
        read_state(symlink_run)

    oversized_run = tmp_path / "oversized-run"
    oversized_run.mkdir()
    (oversized_run / "status").write_text("X" * 129, encoding="utf-8")
    with pytest.raises(StateTransitionError, match="oversized"):
        read_state(oversized_run)


@pytest.mark.parametrize("content", ("", "UNKNOWN\n", b"\xff"))
def test_status_reader_rejects_empty_unknown_and_non_utf8(
    tmp_path: Path,
    content: str | bytes,
) -> None:
    run_dir = tmp_path / "invalid-run"
    run_dir.mkdir()
    status_path = run_dir / "status"
    if isinstance(content, bytes):
        status_path.write_bytes(content)
    else:
        status_path.write_text(content, encoding="utf-8")

    with pytest.raises(StateTransitionError):
        read_state(run_dir)


def test_run_blocked_sources_match_independent_documentation() -> None:
    """TRANSITIONS must not silently diverge from the documented sources."""
    assert (
        TRANSITIONS[RunEvent.RUN_BLOCKED].sources == DOCUMENTED_RUN_BLOCKED_SOURCES
    )


@pytest.mark.parametrize(
    "source",
    sorted(
        DOCUMENTED_RUN_BLOCKED_SOURCES,
        key=lambda state: state.value if state is not None else "",
    ),
)
def test_run_blocked_from_each_documented_source(
    tmp_path: Path,
    source: RunState | None,
) -> None:
    run_dir = tmp_path / f"block-from-{source.value if source else 'empty'}"
    set_state_fixture(run_dir, source)

    result = transition_run(run_dir, RunEvent.RUN_BLOCKED)

    assert result.result == "applied"
    assert result.previous == source
    assert result.current == RunState.BLOCKED
    assert read_state(run_dir) == RunState.BLOCKED


def test_run_blocked_from_missing_status_is_startup_failure(tmp_path: Path) -> None:
    """Fresh run dirs may be closed by record-failure before run_started."""
    run_dir = tmp_path / "fresh"
    run_dir.mkdir()
    assert read_state(run_dir) is None

    result = transition_run(run_dir, RunEvent.RUN_BLOCKED)

    assert result.previous is None
    assert result.current == RunState.BLOCKED
    assert result.result == "applied"


def test_production_has_only_one_status_writer() -> None:
    state_machine = AGENTS / "dx" / "state_machine.py"
    # Every production Python/shell file under scripts/agents is scanned;
    # only state_machine.py may write status.
    other_python = sorted(
        path for path in AGENTS.rglob("*.py") if path != state_machine
    )
    other_shell = sorted(AGENTS.rglob("*.sh"))
    assert other_shell, "expected production shell entrypoints under scripts/agents"

    # state_machine itself is the sole allowed writer; still assert it writes once.
    machine_hits = find_python_status_write_violations(
        state_machine.read_text(encoding="utf-8")
    )
    assert len(machine_hits) == 1, machine_hits

    for path in other_python:
        hits = find_python_status_write_violations(path.read_text(encoding="utf-8"))
        assert hits == [], f"{path}: {hits}"

    for path in other_shell:
        hits = find_shell_status_write_violations(path.read_text(encoding="utf-8"))
        assert hits == [], f"{path}: {hits}"

    cli = (AGENTS / "dx" / "cli.py").read_text(encoding="utf-8")
    assert 'add_parser("set-status"' not in cli
    assert 'add_parser("transition-state"' in cli


@pytest.mark.parametrize("fixture", NEGATIVE_PYTHON_STATUS_WRITES)
def test_python_status_write_scanner_rejects_direct_writers(fixture: str) -> None:
    assert find_python_status_write_violations(fixture), fixture


@pytest.mark.parametrize("fixture", NEGATIVE_SHELL_STATUS_WRITES)
def test_shell_status_write_scanner_rejects_direct_writers(fixture: str) -> None:
    assert find_shell_status_write_violations(fixture), fixture


@pytest.mark.parametrize("fixture", POSITIVE_PYTHON_STATUS_READS)
def test_python_status_write_scanner_allows_reads(fixture: str) -> None:
    assert find_python_status_write_violations(fixture) == [], fixture


@pytest.mark.parametrize("fixture", POSITIVE_SHELL_STATUS_READS)
def test_shell_status_write_scanner_allows_reads_and_path_logging(
    fixture: str,
) -> None:
    assert find_shell_status_write_violations(fixture) == [], fixture


def test_internal_cli_exposes_only_runner_events(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    script = AGENTS / "telegram_bridge.py"

    started = subprocess.run(
        [
            sys.executable,
            str(script),
            "transition-state",
            "--run-dir",
            str(run_dir),
            "--event",
            RunEvent.RUN_STARTED.value,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    forbidden = subprocess.run(
        [
            sys.executable,
            str(script),
            "transition-state",
            "--run-dir",
            str(run_dir),
            "--event",
            RunEvent.HUMAN_APPROVED.value,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert started.returncode == 0
    assert json.loads(started.stdout)["current"] == RunState.EXECUTING.value
    assert forbidden.returncode == 2
    assert read_state(run_dir) == RunState.EXECUTING


def test_record_failure_blocks_once_and_preserves_first_reason(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    script = AGENTS / "telegram_bridge.py"
    transition_run(run_dir, RunEvent.RUN_STARTED)

    def record(reason: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "record-failure",
                "--run-dir",
                str(run_dir),
                "--reason",
                reason,
                "--phase",
                "executor",
                "--iteration",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert record("first_failure").returncode == 0
    first = json.loads((run_dir / "failure.json").read_text(encoding="utf-8"))
    assert record("replayed_failure").returncode == 0

    assert read_state(run_dir) == RunState.BLOCKED
    assert json.loads(
        (run_dir / "failure.json").read_text(encoding="utf-8")
    ) == first
