---
id: TASK-01A
status: planned
depends_on:
  - SELF-00A
---

# TASK-01A — First-class task metadata

## Contexto

O engine deriva o ID apenas do nome do arquivo. Consumidores já usam front
matter com `id`, `status` e `depends_on`, mas essa informação não possui parser
genérico e acaba duplicada em shell/roadmap.

## Objetivo

Adicionar um parser stdlib-only, pequeno e fail-closed para o subset de front
matter necessário ao contrato genérico de task.

## Escopo permitido

- tipo `TaskMetadata` com id, status e dependencies;
- parser do front matter no início do arquivo;
- suporte a `depends_on: []` e lista simples com um ID por item;
- comando local/sanitizado para inspecionar metadata, se útil;
- compatibilidade explícita com tasks legadas sem front matter;
- testes, documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- parser YAML geral ou dependência PyYAML;
- resolver dependências ou impor semântica de conclusão;
- conhecer IDs, milestones ou estados específicos do `artang-platform`;
- editar task/roadmap automaticamente;
- executar próxima task, Git remoto, deploy ou plugin framework.

## Decisões fixadas

- O subset aceita exatamente `id`, `status` e `depends_on`; chave duplicada,
  desconhecida, tipo inesperado ou delimitador incompleto falha fechado.
- `id` e dependências são tokens seguros e genéricos, sem impor a regex de IDs
  do consumidor; o `id` declarado deve coincidir com o stem do arquivo.
- `status` é um token não vazio preservado pelo core. Quais estados satisfazem
  dependência pertence à política do projeto.
- Dependências preservam ordem declarada, não aceitam duplicata nem
  self-dependency.
- Task sem front matter continua válida no modo legado: ID pelo filename,
  status indisponível e dependências vazias, com marcação explícita `legacy`.

## Critérios bloqueantes

1. Todos os arquivos deste backlog e os formatos reais `[]`/lista do consumidor
   são parseados deterministicamente.
2. Unknown/duplicate keys, escalares/listas trocados, IDs inseguros, ID divergente,
   duplicata, self-dependency e front matter truncado são recusados.
3. Fixtures existentes sem front matter continuam executáveis sem alteração de
   CLI, prompt ou task ID.
4. O parser possui limites de tamanho/quantidade, não executa tags/templates e
   não lê fora do arquivo regular rastreado.
5. Nenhuma dependência externa é adicionada e o core não interpreta estados de
   domínio do projeto.
6. Testes integram o parser ao preflight de task sem antecipar `TASK-01B`.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_dx02.py \
  tests/unit/test_agent_orchestration_scripts.py \
  tests/unit/test_agent_pc03.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Documentar a gramática suportada e o comportamento legado. Atualizar esta task
e roadmap com testes reais, sem converter automaticamente tasks consumidoras.

## Riscos / observações

- "YAML-like subset" deve ser descrito como contrato próprio, não como
  compatibilidade YAML completa.

