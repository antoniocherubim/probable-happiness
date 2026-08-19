---
id: ARCH-01B
status: planned
depends_on:
  - ARCH-01A
---

# ARCH-01B — Separate agent roles from vendor drivers

## Contexto

O protocolo precisa de papéis Executor e Reviewer. Hoje comandos, prompts e
mensagens acoplam conceitualmente Executor a Cursor e Reviewer a Codex, embora
essas sejam apenas as implementações concretas atuais.

## Objetivo

Introduzir uma fronteira interna simples `role -> driver` preservando Cursor e
Codex como defaults e todo o comportamento externo atual.

## Escopo permitido

- interfaces/configuração internas de Executor role e Reviewer role;
- drivers Cursor e Codex extraídos do orchestration atual;
- resolução, autenticação, argv, report e identidade diagnóstica dos drivers;
- compatibilidade com `CURSOR_AGENT_BIN` e `CODEX_BIN`;
- testes com fakes, documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- implementar outro vendor/model;
- LangChain ou framework de agentes;
- escolher modelo automaticamente, ensemble, voting ou planner swarm;
- mudar schema de review, trust boundaries, prompts de escopo ou rede Git;
- executar mesmo-model experiment nesta task.

## Decisões fixadas

- `Executor` e `Reviewer` são papéis do protocolo; Cursor/Codex são drivers
  concretos selecionados pelos defaults atuais.
- Driver prepara comando e interpreta seu report, mas não controla estado,
  validação, snapshot, approval ou integração.
- O reviewer permanece uma fase separada, sem permissão de editar o worktree.
- Overrides e resolução existentes mantêm precedência e mensagens compatíveis.
- Identidade/versionamento indisponível é registrado como tal; não se inventa
  modelo a partir do nome do binário.

## Critérios bloqueantes

1. O fluxo default executa o mesmo argv Cursor/Codex, reports, schema, prompts,
   autenticação e transições observáveis de antes.
2. Orchestrator depende de papéis/interfaces, não de condicionais espalhadas por
   nomes de vendor.
3. Fake Executor e fake Reviewer provam contratos independentes, inclusive
   failure, timeout, report vazio e reviewer sem mutação.
4. Codex Snap continua recusado e os overrides atuais continuam cobertos.
5. Nenhum driver ganha autoridade sobre Git remoto, integração ou estado.
6. Documentação habilita experimento futuro same-model vs different-model sem
   afirmar qualquer resultado.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_orchestration_scripts.py \
  tests/unit/test_agent_pc03.py \
  tests/unit/test_agent_dx02.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar somente a abstração e os dois drivers existentes, com testes de
equivalência. Atualizar task/roadmap com evidência real.

## Riscos / observações

- Abstração prematura deve ser evitada: a interface deve conter apenas o que os
  dois papéis atuais realmente precisam.

