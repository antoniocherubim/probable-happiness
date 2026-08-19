---
id: DOC-01A
status: planned
depends_on:
  - SELF-01A
  - OBS-01A
---

# DOC-01A — Research-facing documentation

## Contexto

Uma baseline de documentação voltada à apresentação já existe sobre o estado
pós-`SELF-00P`. Depois de provenance, stable controller e export estruturado,
esta task fará a sincronização final para que esses mecanismos possam ser
descritos como implementados, sem transformar hipóteses em resultados.

## Objetivo

Revisar e sincronizar a documentação técnica/de pesquisa com o estado final do
P0, preservando a separação entre mecanismo, observação, hipótese e experimento
ainda não realizado.

## Escopo permitido

- atualizar `README.md`, `docs/AGENT_ORCHESTRATION.md` e
  `docs/PROJECT_PROFILE.md`;
- criar `docs/ARCHITECTURE.md`, `docs/RESEARCH_OVERVIEW.md` e
  `docs/EVALUATION_PLAN.md`;
- placeholder curto e explicitamente incompleto de `RELATED_WORK.md`, se útil;
- testes/checagens de links, termos e afirmações proibidas;
- esta task e `ROADMAP.md`.

## Fora de escopo

- implementar mecanismo novo;
- revisão bibliográfica substantiva ou citação não verificada;
- afirmar ganho empírico, superioridade ou causalidade sem experimento;
- chamar o sistema de formal verification ou Speculative Reasoning de
  inference-time;
- apresentar `artang-platform` como prova de superioridade;
- dashboard, telemetria, push, deploy ou publicação externa.

## Decisões fixadas

- Termos preferidos: verification-driven agentic execution, candidate ou
  speculative execution no nível de ação/estado, content-bound approval,
  programmatic/non-LLM validation, separate review stage e controlled canonical
  state transition.
- Cada documento distingue explicitamente: mecanismo implementado, case study
  observado, hipótese de pesquisa e experimento ainda não realizado.
- `artang-platform` é evidência de uso externo/portabilidade longitudinal,
  não prova de confiabilidade melhor.
- O plano de avaliação usa somente métricas recuperáveis por exports e declara
  ameaças à validade, baseline e critérios antes de qualquer resultado.

## Critérios bloqueantes

1. Arquitetura documenta Engine N, candidate N+1, adapter de projeto, fases,
   trust boundaries e integração local explícita conforme o código real.
2. Research overview rotula cada afirmação relevante como implementada,
   observada, hipótese ou ainda não avaliada.
3. Evaluation plan define unidades de análise, exports, métricas descritivas,
   comparações futuras e limitações sem inserir resultados fictícios.
4. README continua operacional para consumidores atuais e não promete API,
   compatibilidade ou garantias inexistentes.
5. Nenhum documento inventa evidência, commit, CI, paper, link remoto ou melhora
   empírica.
6. Teste documental recusa os exageros/termos proibidos no contexto de claims e
   a suíte completa permanece verde.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_local_only.py \
  tests/unit/test_agent_orchestration_scripts.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar os seis documentos listados com links coerentes e linguagem calibrada.
Atualizar esta task/roadmap com checks realmente executados; deixar related work
para uma task bibliográfica separada.

## Riscos / observações

- O vocabulário deve acompanhar o mecanismo implementado no momento da task,
  não o desenho planejado neste backlog.

