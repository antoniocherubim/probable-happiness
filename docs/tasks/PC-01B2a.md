# PC-01B2a — Incorporar failure no estado

Status: concluída.

Objetivo único: substituir `failure.json` pelo campo `failure` de `state.json`,
atualizado atomicamente junto do status `BLOCKED` sob `.state.lock`.

Não alterar orçamento, decisão humana ou reports nesta etapa.

## Evidência

- `BLOCKED` e `failure` são publicados em uma única escrita de `state.json`;
- replay preserva o primeiro blocker estruturado;
- código de produção não contém caminho para `failure.json`;
- suíte completa: 204 testes aprovados.
