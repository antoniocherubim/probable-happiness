---
id: PROV-01A
status: planned
depends_on:
  - SELF-00A
---

# PROV-01A — Capture engine provenance

## Contexto

O metadata atual identifica target, task, base, worktree e profile, mas não
responde qual implementação exata do harness criou e controlou o run. Path,
commit ou um simples indicador `dirty` isoladamente não identificam os bytes
efetivamente executados.

## Objetivo

Capturar uma identidade determinística e diagnóstica do Engine N no momento da
criação de cada novo run e persistí-la junto ao metadata autoritativo.

## Escopo permitido

- contrato versionado de provenance do engine;
- tool root canônico, versão/schema, Git HEAD quando disponível e estado
  clean/dirty;
- fingerprint content-addressed da superfície efetiva do controller;
- persistência/leitura no metadata e apresentação sanitizada por CLI local;
- compatibilidade de leitura com runs antigos;
- testes, esta task e `ROADMAP.md`.

## Fora de escopo

- aplicar política de drift em `resume`, `verify` ou `integrate`;
- assinar artefatos, usar serviço externo ou criar banco/registry;
- usar timestamp como identidade;
- identificar semanticamente modelos/vendors ou medir qualidade do run;
- alterar o candidato com dados de provenance.

## Decisões fixadas

- A identidade contém `provenance_schema_version`, `engine_schema_version`,
  `tool_root`, `git_commit` e `git_dirty` anuláveis quando Git estiver
  indisponível, e `source_fingerprint`.
- `source_fingerprint` cobre, com paths/modos/bytes ordenados e leitura
  no-follow, o entrypoint, `scripts/agents/`, schema do reviewer e templates
  usados pelo controller. Ele é obrigatório mesmo em checkout clean ou sem Git.
- Campos temporais podem registrar observação operacional, mas ficam fora da
  identidade e do match futuro.
- Overrides de teste como `AGENT_DX_CLI` não podem produzir provenance ambígua
  no caminho normal; devem ser recusados ou identificados explicitamente.
- Runs antigos sem o campo continuam legíveis e são classificados como
  `legacy_provenance_unavailable`; nenhuma provenance é inferida retroativamente.

## Critérios bloqueantes

1. Todo run novo persiste provenance completa atomicamente com o metadata, sem
   expor segredo, hostname desnecessário ou conteúdo integral dos arquivos.
2. Dois roots com mesmo commit e bytes iguais têm fingerprints iguais, enquanto
   mudança de conteúdo, modo ou arquivo da superfície altera o fingerprint.
3. Checkout dirty e instalação sem metadata Git continuam recebendo identidade
   exata pelos bytes; `git_commit` ausente nunca vira valor inventado.
4. Escrita parcial, symlink, arquivo especial, troca durante leitura e superfície
   incompleta falham antes de iniciar agentes.
5. Runs antigos permanecem carregáveis com estado legado explícito; runs novos
   com provenance malformada falham fechados.
6. Testes end-to-end demonstram captura clean, dirty, sem Git e determinismo.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_state_machine.py \
  tests/unit/test_agent_pc03.py \
  tests/unit/test_agent_orchestration_scripts.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Documentar o contrato e os limites da provenance, incluindo quais arquivos
formam a superfície do controller. Atualizar esta task e o roadmap apenas com
evidência produzida pelo run real.

## Riscos / observações

- O fingerprint identifica bytes, não atesta autoria ou integridade contra um
  processo malicioso do mesmo UID.
- Enforcement fica deliberadamente em `PROV-01B`.
