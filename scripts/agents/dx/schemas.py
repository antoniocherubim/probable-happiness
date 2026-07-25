"""Schema and runner versions for durable run persistence (DX-08)."""

from __future__ import annotations

# SemVer of the runner that writes new artifacts. Persisted on new runs so
# migrations can refuse unknown futures without mutating state.
RUNNER_VERSION = "1.1.0"

# Persistence envelope shared by run metadata and the audit trail.
PERSISTENCE_SCHEMA_VERSION = 2

# Per-artifact schema versions after DX-08 migrations.
RUN_METADATA_SCHEMA_VERSION = 2
ITERATION_BUDGET_SCHEMA_VERSION = 2
AUDIT_TRAIL_SCHEMA_VERSION = 1
TXN_JOURNAL_SCHEMA_VERSION = 1

# Compatibility window: this release can migrate or inspect these prior
# persistence schema numbers without requiring manual JSON edits.
SUPPORTED_PRIOR_PERSISTENCE_SCHEMAS = frozenset({1, 2})

# Artifact schema versions this release understands for migration/inspect.
SUPPORTED_RUN_METADATA_SCHEMAS = frozenset({1, 2})
SUPPORTED_ITERATION_BUDGET_SCHEMAS = frozenset({1, 2})

# Explicit refusal for unknown futures (never mutate).
FUTURE_SCHEMA_REFUSAL = (
    "persistence schema is newer than this runner; refusing mutation"
)
