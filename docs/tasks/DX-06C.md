# DX-06C — Operação local sem push automático

Status: implementada; aguardando revisão formal.

Marco: M0, fechamento simplificado.

Depende de: DX-05. Substitui as candidatas não aprovadas DX-06/DX-06B.

Próxima task: DX-07.

## Decisão

O produto estável não possui autoridade de commit ou push. Cursor continua
trabalhando no worktree local; Codex revisa; Telegram registra a decisão humana.
Depois de `HUMAN_APPROVED`, o worktree e o hash revisado permanecem preservados
para integração Git manual.

Os experimentos DX-06/DX-06B mostraram que push automático exige, ao mesmo
tempo, isolamento real de credenciais, configuração Git confiável, pin de
host/remote e recuperação transacional depois de falhas de rede. Essa fronteira
é desproporcional ao M0 e não deve ficar parcialmente disponível.

## Comportamento

- profiles aceitam somente ausência de `[delivery]` ou
  `[delivery] mode = "none"`;
- `push_branch` e antigas chaves de remote/branch/push falham no preflight;
- `run.json` registra somente `delivery.mode = "none"`;
- o callback Telegram termina em `HUMAN_APPROVED`;
- a bridge não importa código de delivery/Git e não cria job;
- `run_task.sh` e `resume` não criam commit, branch ou conexão remota;
- `agent-loop verify --run-dir ...` revalida decisão e snapshot antes da
  integração manual;
- estados legados `DELIVERING`, `DELIVERY_FAILED` e `PUSHED` permanecem
  reconhecíveis para leitura, mas nunca retomam operação de rede.

## Superfície removida

- comandos `deliver-run`, `ensure-delivery-job` e `delivery-worker`;
- módulos `dx.delivery` e `dx.delivery_job`;
- schema `delivery-job.schema.json`;
- criação/replay de `delivery-job.json`;
- configuração de remote, base branch, branch template, commit template e
  `push_after_human_approval`;
- testes que dependiam de remotes bare e push automático.

O histórico permanece recuperável pelo Git e pelas branches WIP locais. A
remoção não publica nem apaga branches remotas.

## Migração

Remover do profile:

```toml
[delivery]
mode = "push_branch"
remote = "origin"
base_branch = "main"
branch_template = "{task_slug}"
commit_message_template = "{task_id}: {task_title}"
push_after_human_approval = true
```

É válido omitir a tabela ou manter:

```toml
[delivery]
mode = "none"
```

Runs antigos já aprovados podem ser verificados, mas esta versão não continua
jobs de delivery. Integração e publicação exigem ação manual do operador.

## Evidência

- `python3 -m compileall -q scripts/agents/dx`;
- `bash -n agent-loop scripts/agents/*.sh`;
- `git diff --check`;
- `venv/bin/python -m pytest -q tests/unit`:
  **111 passed, 0 failed**.

As regressões locais provam:

- profile `push_branch` é recusado;
- os módulos/schema e a CLI de delivery estão ausentes;
- aprovação não cria `delivery-job.json` nem `delivery.json`;
- `plan_resume` termina aprovação em `complete`;
- Telegram usa linguagem local, sem promessa de publicação;
- documentação obrigatória, snapshots e mensagens multipart continuam
  cobertos.

## Critérios de aceite

1. Nenhum comando público ou interno inicia push automático.
2. Bridge e aprovação não importam módulos de delivery.
3. Nenhum profile válido configura remote ou credencial Git.
4. Aprovação válida termina em `HUMAN_APPROVED`.
5. Worktree pós-aprovação continua verificável pelo hash revisado.
6. Ajuda, README, profile e documentação operacional descrevem apenas
   integração manual.
7. A suíte completa passa sem depender de rede Git.

## Riscos residuais

- a integração manual usa a configuração, hooks e credenciais Git do ambiente
  do operador; ela está fora da fronteira do runner;
- o executor é instruído a não fazer push e usa o sandbox da CLI, mas esta task
  não prova bloqueio de rede em nível de sistema operacional;
- outro processo com o mesmo UID ainda pode adulterar worktree/state root;
  `verify` detecta drift, mas hashes não autenticam esse adversário;
- runs antigos de delivery não são migrados automaticamente para uma branch;
- push automático poderá voltar apenas como subsistema isolado, com credencial
  efêmera e máquina transacional própria, em marco posterior.
