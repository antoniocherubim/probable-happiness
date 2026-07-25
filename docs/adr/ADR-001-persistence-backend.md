# ADR-001 — Backend de persistência do agent-loop (DX-08)

Status: aceita.
Data: 2026-07-25.

## Contexto

DX-07 centralizou transições tipadas, mas status e artefatos correlatos ainda
podiam divergir após crash. DX-08 exige persistência segura, durável,
transacional, versionada e inspecionável.

Duas famílias de backend foram comparadas:

1. JSON em arquivos + journal/transaction marker + recovery;
2. SQLite em WAL, com export JSON auditável.

## Critérios

| Critério | JSON + journal | SQLite WAL |
|---|---|---|
| Crash consistency | fsync arquivo + dir; journal completa ou aborta | WAL + commit atômico nativo |
| Locks multiprocess | `flock` por run já existente | locks do SQLite + atenção a NFS |
| Migrations | registry explícito por artefato | `ALTER`/versão de schema SQL |
| Inspeção manual | `jq`/editor em artefatos nomeados | exige `sqlite3` ou export |
| Portabilidade | só filesystem POSIX local | libsqlite + filesystem |
| Backup | tar dos arquivos do run | dump/`VACUUM INTO` + export |
| Complexidade | alinhada ao modelo atual | nova superfície + ORM/SQL |

## Decisão

**JSON + journal/transaction marker e recovery.**

Motivos:

- o produto já persiste contratos JSON nomeados (`run.json`, `failure.json`,
  request/decision, outbox, ledger); um segundo modelo aumentaria risco de
  divergência;
- inspeção manual e auditoria independente permanecem sem ferramenta extra;
- `flock` por run e compare-and-set de status já existem (DX-07);
- journal `.txn.json` cobre o par estado+artefato exigido pelos eventos
  críticos sem introduzir SQL;
- SQLite em NFS/FUSE também sofre limites de locking; não resolve sozinho o
  requisito de diagnosticar filesystems inadequados.

SQLite permanece uma opção futura se índices globais de outbox (M3) ou consultas
cross-run justificarem o custo; qualquer adoção exigiria export JSON contínuo.

## Consequências

- escritas passam por `scripts/agents/dx/persist.py` com `O_NOFOLLOW`, owner,
  modo privado, fsync de arquivo e diretório;
- eventos críticos usam `scripts/agents/dx/txn.py`;
- migrations em `scripts/agents/dx/migrate.py` com backup tar antes de mutar;
- filesystems suportados: POSIX locais (ext4/xfs/btrfs). NFS/FUSE/network FS
  podem falhar no `fsync` de diretório com diagnóstico explícito.
