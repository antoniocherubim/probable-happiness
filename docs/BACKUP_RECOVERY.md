# Runbook — backup e recovery de runs (DX-08)

## Filesystems suportados

- suportados: ext4, xfs, btrfs (POSIX local com `fsync` de diretório);
- não suportados como duráveis: NFS, FUSE e outros network FS — a API falha
  fechada com diagnóstico se o `fsync` do diretório for rejeitado.

## Inspeção (read-only)

```bash
agent-loop inspect --run-dir <run>
agent-loop verify-state --run-dir <run>
agent-loop migrate --run-dir <run> --dry-run
```

A saída lista schemas, hashes de audit, artefatos e problemas. Não inclui
tokens nem ambiente.

`migrate --dry-run` e a recusa de schema futuro **não** criam `.migration.lock`,
`.state.lock` nem qualquer outro arquivo; são fail-closed sem mutação.

## Migration

```bash
agent-loop migrate --run-dir <run>
```

- cria `.migration-backup/pre-migration-*.tar` (publicado via API segura) e
  manifesto **antes** de escrever;
- o manifesto registra `audit_head_before`, `artifact_hashes_before`,
  `artifact_names_before` e `backup_sha256`;
- sob mutação, segura `.migration.lock` e `.state.lock` (não bloqueante) para
  não intercalar com transitions que também usam `.state.lock`;
- é idempotente; não promove `status`;
- registry cobre run metadata, profile congelado, approval/outbox, ledger e
  audit;
- schema futuro desconhecido recusa **antes** de adquirir locks;
- delivery legado (`PUSHED` / `delivery.json`) permanece inspecionável e nunca
  retoma push.

Rollback (somente se o `head_hash` do audit for idêntico ao
`audit_head_before` do manifesto — um evento pré-existente não impede restore;
um evento **novo** após a migration impede):

```bash
agent-loop migrate --run-dir <run> --rollback
```

Restore:

1. autentica o tar contra `backup_sha256` do manifesto;
2. republica membros via `secure_write_bytes` + `fsync` de diretório;
3. remove arquivos criados pela migration que **não** estavam no snapshot
   pré-migration (ex.: `audit-trail.json` criado pelo registry);
4. verifica cada artefato restaurado contra `artifact_hashes_before`.

## Recovery de transação

Se existir `.txn.json` após crash:

```bash
agent-loop verify-state --run-dir <run> --recover
```

- journal é validado (schema fechado, `run_id`, hashes, origin) **antes** de
  mutar; schema futuro/malformado falha sem alterar o diretório;
- `preparing` → aborta o journal, preserva o último estado válido;
- `committing` → reaplica artefatos do journal e conclui o evento de forma
  idempotente; audit usa o `previous_state` e `created_at` **do journal**, não
  o estado já promovido;
- crash após status/artefato e antes do audit: recovery reporta `recovered`,
  grava o evento de audit faltante com a transição original e limpa `.txn.json`;
- nunca cria `human_approval_decision.json` nem remote OID ausentes do journal.

Transações críticas (approval, iteration budget, record-failure, transitions)
seguram `.state.lock` durante journal + artefatos + status + audit.

## Backup operacional

Copie o diretório do run (ou o tar em `.migration-backup/`) preservando modos
`0700`/`0600`. Restore deve ser seguido de `verify-state`.

## Matriz de crash points (evidência)

| Fronteira | Esperado | Teste |
|---|---|---|
| antes do journal | estado anterior intacto | `test_transaction_preparing_aborts_without_promotion` |
| journal `preparing` | abort na recovery | idem |
| após artefato, antes do status (`committing`) | recovery conclui BLOCKED+failure | `test_transaction_blocked_plus_failure_recovers_after_journal` |
| após status, antes do audit | recovery grava audit com `previous_state`/`created_at` do journal | `test_recovery_after_status_before_audit_clears_journal_and_writes_audit` |
| journal futuro/malformado | recusa sem mutação | `test_recovery_rejects_future_and_malformed_journals` |
| approval request + outbox parcial | recovery → AWAITING | `test_approval_request_and_outbox_commit_atomically` |
| decision exclusiva antes do status | recovery → HUMAN_APPROVED | `test_human_decision_crash_before_status_recovers` |
| containment + symlink (folha/intermediário) | falha fechada | `test_containment_root_rejects_leaf_and_intermediate_symlinks` |
| record-failure com `failure.json` symlink | não sucede / não “repara” | `test_transaction_and_record_failure_refuse_symlink_failure` |
| fsync de arquivo / replace / dir fsync | falha injetada aborta publish | `test_fault_injection_boundaries_for_secure_write` |
| audit timestamp / persistence futuro | falha fechada | `test_audit_chain_detects_timestamp_tamper_and_replay`, `test_audit_rejects_future_persistence_schema` |
| ledger `updated_at` adulterado | falha fechada | `test_iteration_budget_updated_at_tamper_rejected` |
| resume com persistence/runner futuro | recusa | `test_load_run_metadata_rejects_future_persistence_and_runner` |
| migration dry-run / futuro / rollback | sem mutação / remove criados / valida hashes | `test_migration_dry_run_apply_repeat_and_future_schema`, `test_migration_rollback_respects_pre_migration_audit_head` |
| approval×state-lock interleave | sem artefato parcial nem journal preso | `test_critical_txn_holds_state_lock_against_blocked_interleave` |
| migrate × transition (multiprocess) | locks canônicos | `test_multiprocess_migration_versus_transition` |

### Lacunas conhecidas da matriz

- não há fault injection de processo kill em **cada** fronteira de **cada**
  evento DX-07 (cobertura é por journal phase + API write path);
- fixtures DX-01…DX-07 são sintéticas, não archives históricos completos.
