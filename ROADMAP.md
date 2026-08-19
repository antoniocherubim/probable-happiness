# Roadmap — self-hosting e observabilidade

Status desta fase: backlog planejado; nenhuma feature abaixo está implementada.

O objetivo é evoluir o harness por verification-driven agentic execution,
mantendo Engine N como controller estável do candidate Engine N+1. Aprovação e
integração continuam content-bound, locais e explicitamente acionadas pelo
operador. Não há push, deploy ou autoridade remota no runtime.

## P0 de bootstrap obrigatório

- [ ] `SELF-00P` — separar adapter de controle congelado do adapter candidato.

Essa predecessora é o menor ajuste técnico à ordem solicitada. O engine atual
recusa qualquer mudança em `.agent-loop/project.toml` e lê instruções do
worktree candidato; portanto `SELF-00A` não pode atualizar o gate nem criar
`reviewer.md` com segurança antes de `SELF-00P`. Ela é um must-have técnico
adicional e a primeira task a executar; não altera a prioridade relativa dos
seis itens solicitados abaixo.

## Must-have até sexta-feira

1. [ ] `SELF-00A` — bootstrap do adapter self-hosted.
2. [ ] `PROV-01A` — captura de provenance do engine.
3. [ ] `PROV-01B` — fail-closed em drift para resume/verify/integrate.
4. [ ] `SELF-01A` — stable-controller invariant para self-hosting.
5. [ ] `OBS-01A` — export estruturado e determinístico de run.
6. [ ] `DOC-01A` — documentação voltada a apresentação/pesquisa.

## High-value stretch

Prioridade de produto solicitada:

7. [ ] `TASK-01A` — metadata de task como contrato de primeira classe.
8. [ ] `TASK-01B` — dependency preflight genérico e project-controlled.
9. [ ] `OBS-01B` — estatísticas agregadas de exports.
10. [ ] `ARCH-01A` — contrato explícito do project adapter.
11. [ ] `ARCH-01B` — separação entre papéis e vendor drivers.
12. [ ] `OBS-02A` — taxonomia opcional de findings.

A ordem técnica move `OBS-02A` antes de `OBS-01B`, pois a métrica pedida de
findings por categoria não existe no schema atual. Isso não torna os campos
novos obrigatórios nem migra relatórios antigos.

## DAG

```text
SELF-00P → SELF-00A → PROV-01A → PROV-01B → SELF-01A
SELF-01A → OBS-01A → DOC-01A
SELF-00A → TASK-01A → TASK-01B
SELF-01A + TASK-01B → ARCH-01A → ARCH-01B
OBS-01A → OBS-02A → OBS-01B
```

Ordem linear recomendada quando não houver execuções paralelas:

```text
SELF-00P → SELF-00A → PROV-01A → PROV-01B → SELF-01A
→ OBS-01A → DOC-01A → TASK-01A → TASK-01B
→ ARCH-01A → ARCH-01B → OBS-02A → OBS-01B
```

## Disciplina de execução

- Todas as tasks permanecem `planned` neste commit de planejamento.
- Cada run deve atualizar sua própria task e este roadmap com comandos,
  contagens e riscos reais; evidência não é preenchida antecipadamente.
- Uma task nunca usa a feature que está criando como único gate de validação.
- Em self-hosting, o candidate runtime nunca substitui o controller do run que
  o produz.
- `artang-platform` permanece somente evidência de consumo externo e não é
  modificado por este roadmap.
- Ficam fora desta fase: multi-candidate search, tree-of-thought, planner swarm,
  memória vetorial, embeddings, LangChain, auto-training, voting de reviewers,
  deploy automático e agentes com autoridade remota.
