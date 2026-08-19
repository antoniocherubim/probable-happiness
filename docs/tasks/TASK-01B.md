---
id: TASK-01B
status: planned
depends_on:
  - TASK-01A
---

# TASK-01B — Generic dependency preflight

## Contexto

Consumidores hoje duplicam dependências em front matter, roadmap e grandes
blocos `if TASK_ID == ...`. Alguns também usam checkpoints agregados que não
correspondem diretamente a um arquivo de task, portanto o engine não pode impor
uma convenção de produto.

## Objetivo

Oferecer um preflight genérico e opt-in que exponha o contrato normalizado da
task a uma política declarada pelo adapter do projeto, antes de criar run ou
worktree.

## Escopo permitido

- configuração opcional e estrita de dependency preflight no project profile;
- hook argv rastreado, sem shell/eval, com contexto normalizado e allowlisted;
- preflight antes de qualquer agente, run dir final ou worktree;
- helpers para validar grafo de tasks diretamente resolvíveis;
- diagnósticos estáveis e testes positivos/negativos;
- documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- hardcode de IDs/status/checkpoints do `artang-platform`;
- exigir que todo dependency ID tenha `docs/tasks/<id>.md`;
- mover regras de milestone, CI remoto ou produto para o engine;
- auto-selecionar/autoexecutar próxima task;
- discovery de plugins, shell implícito, rede ou mutação de roadmap.

## Decisões fixadas

- Profiles atuais sem preflight mantêm o comportamento existente.
- Quando configurado, o engine parseia `TaskMetadata` e executa um comando argv
  do adapter com cwd no repositório/base e ambiente mínimo; o comando decide se
  estados/checkpoints do projeto satisfazem as dependências.
- A interface fornece task file/id/status/dependencies em forma não ambígua e
  versionada; nenhum item é interpolado em shell.
- Falha, timeout, saída inválida ou dependência não satisfeita bloqueia antes de
  alocar o run e tem mensagem estável.
- Para dependências que resolvem a arquivos estruturados, helper stdlib pode
  detectar ausência, self-edge, duplicata e ciclo; agregados permanecem no hook.

## Critérios bloqueantes

1. Preflight configurado aceita dependência satisfeita e recusa ausente,
   incompleta ou desconhecida conforme a política fixture do projeto.
2. Recusa ocorre antes de worktree, run metadata, executor e reviewer; teste
   negativo comprova ausência de efeitos persistentes.
3. Duplicata, self-dependency e ciclo diretamente resolvível falham com
   diagnóstico determinístico.
4. Hook recebe somente contexto allowlisted, roda sem `eval`, sem protocolo Git
   remoto e com timeout/limites documentados.
5. Fixture externa demonstra regras diferentes, inclusive checkpoint agregado,
   sem alteração no core ou bloco condicional por task no launcher.
6. Profile e wrappers existentes continuam válidos quando a opção não é usada.

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

Entregar contrato do hook, preflight sem efeitos colaterais e fixture de pelo
menos duas políticas de projeto. Atualizar task/roadmap com resultados reais.

## Riscos / observações

- O hook continua sendo código confiável do adapter no base commit; o candidato
  não pode trocar a política do próprio run.

