# Roadmap — self-hosting e observabilidade

Status desta fase: `SELF-00P` implementada neste worktree, com evidência de
teste abaixo; as demais features do DAG continuam no backlog planejado.

O objetivo é evoluir o harness por verification-driven agentic execution,
mantendo Engine N como controller estável do candidate Engine N+1. Aprovação e
integração continuam content-bound, locais e explicitamente acionadas pelo
operador. Não há push, deploy ou autoridade remota no runtime.

## P0 de bootstrap obrigatório

- [x] `SELF-00P` — separar adapter de controle congelado do adapter candidato.

Evidência (2026-08-19, Python canônico `venv/bin/python -m pytest -q`):
240 passed, 0 failed, 0 skipped, 0 errors em 67.26s;
`python3 -m compileall -q scripts/agents`, `bash -n agent-loop scripts/agents/*.sh`
e `git diff --check` saíram 0. Sem SHA/URL de branch: a integração ainda não
ocorreu.

Essa predecessora é o menor ajuste técnico à ordem solicitada. O engine passa a
recusar mudança de `.agent-loop/project.toml` salvo `--allow-candidate-profile`,
e lê instruções/gates do adapter congelado no commit-base. `SELF-00A` pode agora
atualizar o gate e criar `reviewer.md` sem controlar a própria avaliação. Ela é
um must-have técnico adicional já executado; não altera a prioridade relativa
dos seis itens solicitados abaixo.

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

- Todas as tasks permanecem `planned` neste commit de planejamento, exceto
  `SELF-00P`, atualizada para `completed` com evidência real deste run.
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
