# PC-01B — Consolidar estado atômico

Status: dividida em PC-01B1 e PC-01B2.

Objetivo único: substituir `status`, `run.json`, failure, decisão e budget
autoritativos por um `state.json` atômico sob um lock por run.

Reports grandes permanecem arquivos separados referenciados por hash. Não
implementar journal, migration, rollback nem audit chain.
