---
id: SELF-00A
status: planned
depends_on:
  - SELF-00P
---

# SELF-00A — Bootstrap self-hosted project integration

## Contexto

Este repositório já contém um profile mínimo, mas seu gate programático cobre
somente `compileall` e `git diff --check`. O uso real em `artang-platform`
mostra uma separação útil entre engine genérico e adapter versionado no projeto,
sem copiar o runtime.

## Objetivo

Preparar o próprio repositório como consumidor disciplinado do engine canônico,
com instruções e gate completo adequados, sem duplicar o agent-loop.

## Escopo permitido

- criar `.agent-loop/executor.md` e `.agent-loop/reviewer.md`;
- criar `scripts/agent-loop/test.sh` e um `start_task.sh` mínimo;
- criar `scripts/agent-loop/bootstrap.sh` apenas para disponibilizar, sem rede,
  o ambiente pytest ignorado no worktree isolado;
- atualizar `.agent-loop/project.toml` usando a autorização de `SELF-00P`;
- testes do caminho self-hosted, esta task e `ROADMAP.md`.

## Fora de escopo

- copiar o engine para `.agent-loop/` ou `scripts/agent-loop/`;
- mapa manual de dependências por ID semelhante ao consumidor externo;
- interpretar front matter ou implementar dependency preflight;
- alterar contratos públicos para consumidores externos;
- provenance, export de runs, drivers alternativos ou operações remotas.

## Decisões fixadas

- `scripts/agent-loop/test.sh` executa a suíte pytest completa, inclusive os
  testes reais de `systemd --user`, e falha em qualquer skip/falha inesperada.
- O bootstrap não instala dependências nem acessa rede e nunca copia,
  compartilha ou cria symlink gravável para o `venv/` canônico no candidato.
  O gate pode invocar um interpreter canônico previamente validado somente com
  sua árvore de dependências isolada como read-only; sem essa garantia, o
  bootstrap bloqueia e pede um ambiente provisionado pelo operador.
- O profile usa o gate completo e `git diff --check`, além de tornar task e
  roadmap documentação obrigatória para os runs seguintes.
- `start_task.sh` faz apenas preflight genérico: task existente e rastreada,
  checkout limpo, base SHA explícito, profile válido e binário canônico.
- O run de `SELF-00A` continua sendo controlado pelo adapter congelado anterior;
  os arquivos candidatos passam a valer somente depois da integração.

## Critérios bloqueantes

1. O profile final referencia instruções rastreadas e um gate que executa toda
   a suíte pytest, não apenas `compileall`.
2. Um teste self-hosted cria um worktree candidato deste repositório e comprova
   que engine, schema e helpers continuam resolvidos pelo `TOOL_ROOT` canônico.
3. Bootstrap e gate funcionam em worktree isolado sem criar artefato não
   ignorado, instalar pacote ou acessar rede.
4. `start_task.sh` não contém branches por task/dependência e preserva base,
   quoting de paths com espaços e `--require-profile`.
5. Instruções de executor/reviewer preservam escopo, validação independente,
   integração manual e proibição de commit/push/deploy pelo agente.
6. A CLI externa atual e uma fixture de projeto consumidor continuam passando.

## Gate focado

```bash
bash scripts/agent-loop/test.sh
bash -n agent-loop scripts/agents/*.sh scripts/agent-loop/*.sh
git diff --check
```

O run deve também provar que o gate estável de Engine N julgou o candidato; o
novo `test.sh` é evidência adicional neste run e torna-se gate configurado apenas
depois da integração.

## Entrega obrigatória

Entregar somente o adapter local mínimo, seus testes e a configuração completa.
Atualizar esta task e `ROADMAP.md` com comandos, contagens e riscos observados.

## Riscos / observações

- O `venv/` local não é versionado; ausência, incompatibilidade ou falta de
  isolamento read-only deve bloquear com diagnóstico, nunca disparar instalação
  implícita nem expor estado mutável ao candidato.
- Esta task deve ser iniciada com a autorização explícita de mudança de profile
  criada por `SELF-00P`.
