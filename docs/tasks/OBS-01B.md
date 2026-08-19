---
id: OBS-01B
status: planned
depends_on:
  - OBS-01A
  - OBS-02A
---

# OBS-01B — Aggregate run statistics

## Contexto

Exports normalizados permitem calcular estatísticas descritivas sem acessar
worktrees, logs livres ou rede. A contagem por categoria depende da taxonomia
opcional introduzida em `OBS-02A`.

## Objetivo

Adicionar um comando local aproximadamente equivalente a `agent-loop stats`
que agregue exports compatíveis de múltiplos runs e emita JSON determinístico.

## Escopo permitido

- leitura de arquivos produzidos por `export-run`;
- total e outcomes approved/blocked/failed;
- first-pass approval, runs com revisão e média/mediana de iterações;
- findings por severity/category, validation failures e tempos disponíveis;
- tratamento explícito de dados ausentes e schemas incompatíveis;
- testes, documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- ler runs brutos diretamente, reexportar ou corrigir evidência;
- dashboard, banco, servidor web, telemetria ou upload;
- inferência causal, benchmark de modelos ou afirmação de melhora;
- agrupar por dado não persistido ou interpretar texto livre;
- mutar exports, runs, Git ou estado.

## Decisões fixadas

- Inputs são um ou mais exports schema-compatible; qualquer repetição da
  identidade composta `(target_repository, run_id)` é recusada com erro
  estável, mesmo quando os arquivos forem idênticos. Nada é deduplicado ou
  contado silenciosamente duas vezes.
- Média/mediana usam somente valores disponíveis e sempre informam denominador
  e quantidade ausente.
- Finding sem category permanece `uncategorized`; categoria nunca é inferida.
- Ordenação de inputs não altera os bytes do resultado.
- Diretório vazio, export parcial e versão desconhecida têm comportamento e
  exit code estáveis.

## Critérios bloqueantes

1. Fixtures cobrem zero, um e múltiplos runs com approved/blocked/failed,
   primeira passagem e revisão repetida.
2. Total, denominadores, média e mediana são exatos e independentes da ordem.
3. Findings são agregados por severity e category, incluindo
   `uncategorized`, sem parsing de texto.
4. Validation failures e tempos usam apenas campos normalizados e distinguem
   zero de indisponível.
5. Schema desconhecido, identidade de run repetida e JSON inválido falham
   fechados sem produzir estatística parcial enganosa.
6. O comando é stdlib-only, somente-leitura, sem rede, banco ou side effects.

## Gate focado

```bash
venv/bin/python -m pytest -q tests/unit/test_agent_run_statistics.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar contrato de input/output, fixtures representativas e exemplos
descritivos claramente rotulados como fixtures. Atualizar task/roadmap com
evidência real.

## Riscos / observações

- Estatísticas descritivas de runs observados estão sujeitas a seleção de tasks,
  mudança de engine e dados ausentes; isso deve acompanhar qualquer leitura.
