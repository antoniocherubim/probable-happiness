# YOLO-01 — Approval mode YOLO

## Status

COMPLETE

## Objective

Add an explicit personal-project mode that runs an ordered list of already
versioned tasks until each receives technical `APPROVED`, integrates every
approved snapshot as one local commit, and starts the next task from the new
local `HEAD`.

## Contract

- The project profile opts in with `[approval] mode = "yolo"`.
- `agent-loop yolo` receives an ordered, non-empty list of safe repository-
  relative task files.
- Every task must be tracked before the sequence starts.
- `CHANGES_REQUESTED` returns to the executor without a numeric iteration cap.
- Executor or reviewer token/context exhaustion is a terminal blocker for the
  complete sequence.
- Every approved task is verified and integrated by the existing local,
  content-bound integrator before the next task starts.
- Integration creates one local commit and advances only the currently checked
  out branch. It never pushes and never creates a task branch.
- Any task, validation, review, integration, repository-drift, or agent failure
  stops the sequence before another task is launched.
- After the last task is integrated, a reviewer-only cumulative technical
  review writes `final-review.md` in the external sequence state directory.
- Findings in the final review are reported only. The executor is not invoked
  again and the already integrated commits are not rewritten.

## Compatibility

Modes `none`, `telegram`, `github_branch`, and legacy `github_pr` retain their
current iteration budgets and terminal behavior. Existing `run`, `resume`,
`verify`, and `integrate` contracts remain valid.

## Exclusions

- push, pull request, merge, deploy, tag, or remote access;
- automatic task creation or product planning;
- parallel task execution;
- rollback or history rewriting after the final review;
- using the YOLO implementation to control the run that introduces it.

## Evidence

Implemented directly under the previous stable controller on 2026-08-27. The
canonical test command `venv/bin/python -m pytest -q -p no:cacheprovider`
completed with `250 passed` in 71.65 seconds. The focused compatibility suite
covering YOLO, local integration, orchestration scripts, `none`/`telegram`, and
`github_branch` completed with `47 passed`.

The YOLO end-to-end regression exercised two tracked tasks, one
`CHANGES_REQUESTED` retry, exactly two linear local commits, no additional Git
branch, and one reviewer-only final Markdown report. Shell syntax, Python
compileall, and `git diff --check` also completed successfully. No push, remote
operation, deployment, or real project run was performed by this evidence.
