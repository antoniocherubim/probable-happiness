---
id: SELF-01A
status: planned
depends_on:
  - PROV-01B
---

# SELF-01A — Enforce stable-controller invariant

## Contexto

No self-hosting, target repository e tool root canônicos podem ser o mesmo
repositório. O worktree candidato conterá uma proposta de Engine N+1, mas essa
proposta não pode controlar a avaliação que a está produzindo.

## Objetivo

Detectar runs self-hosted e garantir por software que todas as operações de
controle daquele run continuem presas ao Engine N registrado.

## Escopo permitido

- canonicalização e comparação entre target repo e tool root;
- persistência explícita de `self_hosted = true|false`;
- snapshot privado/imutável da superfície executável do Engine N ou mecanismo
  equivalente vinculado ao fingerprint de provenance;
- roteamento de helpers, schema, orchestration, verificação e integração para
  o controller estável;
- coordenação com locks e testes deliberadamente adversariais;
- documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- executar o candidate Engine N+1 para julgar o próprio run;
- auto-integração, autoatualização ou "self-improving AI";
- multi-candidate search, votação de reviewers ou operação remota;
- alterar sem necessidade o fluxo de um target externo.

## Decisões fixadas

- `self_hosted` resulta apenas de paths Git/tool root resolvidos e
  canonicalizados, nunca de nome de diretório ou flag fornecida pelo candidato.
- Para run self-hosted, a superfície do Engine N é copiada para storage privado
  do run antes do executor, vinculada ao `source_fingerprint` e usada até o fim.
- Python/interpreter e dependências operacionais podem permanecer externos, mas
  entrypoint, módulos, shell, schema e templates do controller não são lidos do
  worktree candidato.
- `resume`, review orchestration, `verify` e `integrate` devem validar a mesma
  provenance antes de usar o snapshot estável.
- Integração concorrente ou edição do checkout canônico não pode trocar o
  controller no meio de um run; deve serializar ou bloquear com erro estável.

## Critérios bloqueantes

1. Run com `target_repo == tool_root` após canonicalização registra
   `self_hosted = true`; aliases/symlinks não produzem falso negativo.
2. Teste modifica no candidato `agent-loop`, `run_task.sh`, módulos DX, schema
   e instruções e prova que o run corrente continua usando bytes de Engine N.
3. Um candidate helper que tenta produzir aprovação falsa nunca é executado
   pelo controller do mesmo run.
4. Snapshot ausente/adulterado, provenance divergente ou troca concorrente do
   engine bloqueia antes de estado canônico ou integração.
5. Runs de targets externos registram `self_hosted = false` e preservam CLI,
   profile, worktree e comportamento atuais.
6. Teste end-to-end demonstra `Engine N -> candidate Engine N+1 -> review ->
   verify`, sem invocar N+1 como controller.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_orchestration_scripts.py \
  tests/unit/test_agent_pc03.py \
  tests/unit/test_agent_integration.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Documentar o invariant como self-hosting e incluir uma regressão que adultere
deliberadamente a superfície candidata. Atualizar esta task e roadmap com
evidência real.

## Riscos / observações

- O invariant controla a origem dos bytes do harness; não transforma o modelo
  de confiança local em proteção contra root ou processo malicioso do mesmo UID.

