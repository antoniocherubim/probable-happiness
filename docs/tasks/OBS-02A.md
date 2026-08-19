---
id: OBS-02A
status: planned
depends_on:
  - OBS-01A
---

# OBS-02A — Reviewer finding taxonomy

## Contexto

O schema atual de findings é estrito e aceita apenas severity, title, details e
files. Isso preserva um contrato seguro, mas não permite analisar categorias ou
ligar um finding a um critério de aceite sem interpretar texto livre.

## Objetivo

Estender o contrato de finding com classificação opcional, pequena e versionada,
preservando relatórios antigos e rejeitando valores fora da taxonomia.

## Escopo permitido

- campo opcional `category`;
- campo opcional `acceptance_criterion` com referência opaca/limitada;
- categorias: `correctness`, `tests`, `scope`, `architecture`, `security`,
  `documentation`, `reproducibility`, `other`;
- atualização coordenada de JSON Schema, parser, resume e resumo/export;
- testes de compatibilidade, documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- tornar os novos campos obrigatórios;
- classificar automaticamente findings antigos;
- pedir ao reviewer métricas determinísticas que o engine pode calcular;
- alterar severity, status do review ou acceptance criteria da task;
- scoring, dashboard, embeddings, banco ou telemetria.

## Decisões fixadas

- `category` e `acceptance_criterion` são opcionais para compatibilidade.
- Quando presente, `category` aceita somente a enumeração fechada desta task.
- `acceptance_criterion` referencia o identificador/texto curto declarado na
  task; não afirma que o finding foi resolvido nem duplica o critério inteiro.
- O shape antigo continua válido. Campo adicional desconhecido, tipo incorreto
  ou categoria inválida continua falhando fechado.
- Export e resumo preservam campos presentes, mas não fabricam categoria para
  finding legado.

## Critérios bloqueantes

1. Relatório antigo válido continua aceito por schema, `review-status`, resume e
   preparação de artefatos.
2. Relatório novo com ambos os campos é aceito e exportado sem perda.
3. Categoria desconhecida, criterion não string/oversized e campos extras são
   recusados consistentemente em todos os validadores.
4. Prompts descrevem a taxonomia sem obrigar classificação artificial nem
   expandir o escopo de review.
5. Findings por categoria podem ser agregados sem parsing de title/details;
   ausência permanece explicitamente `uncategorized` na camada de estatística.
6. Fixtures existentes e consumidores externos não exigem migração imediata.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_dx02.py \
  tests/unit/test_agent_dx04.py \
  tests/unit/test_agent_local_only.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar schema/parser/prompts/export alinhados e fixtures old/new/invalid.
Atualizar esta task e roadmap com resultados reais.

## Riscos / observações

- Taxonomia melhora a possibilidade de análise; não garante consistência entre
  reviewers sem um experimento posterior.

