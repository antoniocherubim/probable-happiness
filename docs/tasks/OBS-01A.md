---
id: OBS-01A
status: planned
depends_on:
  - SELF-01A
---

# OBS-01A — Structured run export

## Contexto

O run já persiste estado, resultados das fases, validações, reviews, snapshots,
resumo técnico, falhas e integração. Esses artefatos ainda não possuem uma
representação normalizada para análise de runs reais.

## Objetivo

Adicionar `agent-loop export-run --run-dir <path>` para emitir JSON versionado,
determinístico e derivado exclusivamente de evidência persistida.

## Escopo permitido

- schema de export e comando somente-leitura;
- identidade do run/task/base/target e provenance do engine;
- fases, iterações, validações, resultado/falha, reviews e findings;
- arquivos/diff quando recuperáveis de artefato confiável;
- hash revisado e integration status/commit quando registrados;
- tratamento explícito de run aprovado, bloqueado, incompleto e legado;
- testes, documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- banco, dashboard, servidor, telemetria ou upload;
- reexecutar validação, consultar Git remoto ou modificar o run;
- recomputar informação a partir de worktree mutável quando não houver
  artefato confiável;
- inventar identidade/versionamento de executor ou reviewer ausente;
- agregar múltiplos runs.

## Decisões fixadas

- O comando escreve JSON canônico em stdout; qualquer `--output` opcional usa
  escrita atômica e não altera artefatos do run.
- O export tem `schema_version`; listas e chaves têm ordenação determinística.
- Não se gera timestamp novo. Timestamps já persistidos ficam em uma seção
  operacional documentada e não participam da identidade do dataset.
- Campo indisponível é `null` ou recebe availability/reason estruturado; nunca
  é inferido de texto livre ou do estado atual da rede.
- Findings vêm do `review-N.json` autoritativo, não do resumo truncado.
- Contagens de teste seguem a mesma regra da última validação terminal
  autoritativa; outputs intermediários não são somados.

## Critérios bloqueantes

1. O export cobre run id, task id, base, target, provenance, iterações,
   resultado final, validations, changes-requested, findings, tempos persistidos,
   arquivos/diff confiáveis, reviewed hash, integração e blocker.
2. Exportar duas vezes um run imutável produz bytes idênticos.
3. Run incompleto, bloqueado ou legado gera export válido com ausências
   explícitas; JSON corrompido, symlink ou contrato conflitante falha fechado.
4. Additions/deletions e arquivos não são recalculados do worktree após a
   revisão; usam manifesto/resumo vinculado ou ficam indisponíveis.
5. Identidades de drivers não persistidas ficam `null`; o exporter não lê
   logs para adivinhar vendor/model/version.
6. O comando não muda mtimes/conteúdo do run, Git, estado ou integração e não
   abre rede.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_state_machine.py \
  tests/unit/test_agent_dx02.py \
  tests/unit/test_agent_dx04.py \
  tests/unit/test_agent_local_only.py \
  tests/unit/test_agent_integration.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar schema/documentação do export e fixtures de aprovado, bloqueado,
integrado, incompleto e legado. Atualizar esta task e roadmap somente com dados
observados.

## Riscos / observações

- O export normaliza evidência existente; ele não prova causalidade nem melhora
  confiabilidade por si só.

