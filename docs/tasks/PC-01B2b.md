# PC-01B2b — Incorporar orçamento no estado

Status: concluída.

Objetivo único: substituir `iteration-budget.json` pelo campo
`iteration_budget` de `state.json`.

Não alterar decisão humana ou reports nesta etapa.

## Evidência

- orçamento e status são publicados juntos em `state.json`;
- `.resume.lock` e `.state.lock` preservam exclusão e ordem de locks;
- idempotência, hashes, drift e concorrência continuam cobertos;
- código de produção não contém caminho para `iteration-budget.json`;
- suíte completa: 204 testes aprovados.
