# Orquestração externa Codex → Cursor → Codex

## Fluxo

```text
task versionada
  → worktree externo isolado
  → Cursor executa
  → Codex revisa diff, aceite e testes
  → CHANGES_REQUESTED retorna ao Cursor (orçamento inicial de 1 a 5 ciclos)
  → APPROVED técnico
  → AWAITING_HUMAN_APPROVAL
  → HUMAN_APPROVED para o hash revisado
  → sem delivery: verificação manual antes de integrar
  → com delivery: DELIVERING → PUSHED em branch da task
```

Não há automação de decisão de produto, criação de task, merge, push na base,
force-push, tag, PR, deploy, limpeza ou próxima task. Commit e push de branch
ocorrem somente no modo opt-in descrito no perfil.

## Pré-requisitos

```bash
agent status
codex login status
venv/bin/pip install -r requirements.txt  # apenas testes locais
```

As credenciais das CLIs e do Telegram ficam fora do Git.

## Comandos

Dry-run, sem criar worktree ou iniciar agentes:

```bash
./agent-loop run --repo /projetos/alvo --dry-run docs/tasks/CP-00.md 3 main
```

Executar uma task versionada no `base-ref`:

```bash
./agent-loop run --repo /projetos/alvo docs/tasks/CP-00.md 3 main
```

Revisar mudanças já existentes no checkout atual:

```bash
./agent-loop review --repo /projetos/alvo docs/tasks/CP-00.md
```

As opções históricas `--ignore-orchestration` e `--evidence <arquivo>` continuam
disponíveis para `review`. Evidência do executor é sempre tratada como não
confiável e confrontada com o diff.

## Tool, target e state roots

- `TOOL_ROOT`: diretório desta instalação; contém scripts e schema.
- `TARGET_REPO`: raiz Git informada em `--repo`.
- `STATE_ROOT`: `<base>/projects/<repo-id>`; contém `runs`, `worktrees` e lock.

O base padrão é `$XDG_STATE_HOME/codex-cursor-agent-loop`, com fallback para
`~/.local/state/codex-cursor-agent-loop`. Use `--state-root` para sobrescrever.
O `repo-id` usa o caminho Git real, portanto nomes iguais e symlinks não
compartilham estado incorretamente.

Os scripts antigos ainda podem ser chamados dentro do projeto; nesse modo de
compatibilidade usam `<repo>/.agents`.

## Snapshot content-addressed

Antes e depois da revisão, o runner calcula SHA-256 sobre:

- diff Git binário contra o commit-base;
- untracked ordenados por caminho;
- tipo Git, bit executável e conteúdo de arquivos regulares;
- bytes do destino de symlinks, sem seguir o link.

Os hashes devem coincidir. `HUMAN_APPROVED` aprova esse hash imutável, mas não
congela o worktree. Antes de integrar:

```bash
./agent-loop verify --run-dir /state/projects/<repo-id>/runs/<run-id>
```

A verificação retorna sucesso somente quando há uma decisão humana válida, o
status está em `HUMAN_APPROVED`, `DELIVERING`, `DELIVERY_FAILED` ou `PUSHED` e o
hash atual ainda coincide.

## Telegram

Crie um arquivo externo, por exemplo
`~/.config/codex-cursor-agent-loop/telegram.env`, com permissão `0600`:

```bash
AGENT_TELEGRAM_BOT_TOKEN=token-do-botfather
AGENT_TELEGRAM_ALLOWED_USER_ID=123456
AGENT_TELEGRAM_ALLOWED_CHAT_ID=123456
```

Opcionalmente configure `AGENT_TELEGRAM_POLL_TIMEOUT_SEC` e
`AGENT_HUMAN_APPROVAL_TIMEOUT_SEC`. Inicie em foreground:

```bash
AGENT_TELEGRAM_CREDENTIALS_FILE=~/.config/codex-cursor-agent-loop/telegram.env \
  ./agent-loop serve
```

A ponte usa long polling, não abre porta pública e não aceita comandos de shell.
Somente o `user_id` e `chat_id` numéricos allowlisted podem aprovar. Falha de
rede nunca promove estado. Execute somente uma ponte por state root: ela varre
os runs de todos os projetos, mas o processo ainda não possui trava global de
instância.

Ao abrir o gate, o Telegram recebe ID/título, repositório, base, iteração, hash,
arquivos, estatísticas, executor, testes/validações, reviewer, findings, riscos
e documentação — nunca o diff completo. Texto não usa `parse_mode`, URLs e
atribuições sensíveis são redigidas e campos grandes são truncados
explicitamente. Mensagens longas são numeradas; apenas a última tem botões.
Cada `message_id` é persistido depois da resposta bem-sucedida do Telegram,
reduzindo reenvios. A semântica permanece *at-least-once*: uma queda entre envio
e persistência, reinício com offset apenas em memória ou duas pontes concorrentes
pode duplicar updates ou mensagens.

```text
(1/1)
CP-00 — Proibir falso sucesso do adapter Noop

Resultado técnico: APPROVED
Iteração: 2/3
Arquivos: 9
Diff: +288 / -15
Testes: 47 passed, 1 skipped, 0 failed, 0 errors
Hash revisado: 752aef57…

Resumo do reviewer:
Runtime falha antes do claim quando não existe adapter real.

Findings:
- nenhum

Documentação:
- docs/env-variables.md
- ROADMAP.md

[Aprovar e publicar branch] [Rejeitar]
```

## systemd --user

```bash
mkdir -p ~/.config/systemd/user
./agent-loop systemd-unit \
  --credentials-file ~/.config/codex-cursor-agent-loop/telegram.env \
  --output ~/.config/systemd/user/agent-telegram-bridge.service
systemd-analyze verify ~/.config/systemd/user/agent-telegram-bridge.service
systemctl --user daemon-reload
# habilitação é sempre uma ação manual:
# systemctl --user enable --now agent-telegram-bridge.service
```

O template aplica `NoNewPrivileges`, `ProtectSystem=strict`, home read-only e
liberação de escrita somente para o state root.

### Limitação da entrega na unidade atual

Após DX-05 a ponte apenas registra a decisão e enfileira `delivery-job.json`;
ela não executa Git. Conclua a entrega com:

```bash
./agent-loop delivery-worker --run-dir /state/projects/<repo-id>/runs/<run> --once
# ou
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run>
```

A unidade da bridge continua sem escrita no repositório. O `EnvironmentFile`
ainda coloca o token do bot no ambiente da ponte; a DX-06 introduz a unidade do
worker sem esse token, com ambiente Git mínimo e hooks desabilitados. Até lá,
habilite `push_branch` somente em repositórios e hooks confiáveis.

## Estados e falhas

- `EXECUTING`: Cursor trabalhando;
- `REVIEWING`: Codex avaliando;
- `CHANGES_REQUESTED`: feedback retornará ao Cursor;
- `APPROVED`: aceite técnico, nunca humano;
- `AWAITING_HUMAN_APPROVAL`: botão pendente;
- `HUMAN_APPROVED`: decisão autenticada para o hash revisado; com
  `push_branch`, publica `delivery-job.json` pendente;
- `DELIVERING`: worker validou job/manifesto e entrega em andamento;
- `DELIVERY_FAILED`: aprovação preservada; `delivery-worker`/`resume` repetem
  somente a entrega;
- `PUSHED`: commit e OID remoto confirmados; terminal com delivery;
- `BLOCKED`: falha, interrupção, dependência externa ou limite atingido.

Quando a causa for exclusivamente `max_review_iterations`, a notificação
informa que worktree e último feedback foram preservados. A continuação exige
CLI explícita:

```bash
./agent-loop resume --run-dir /state/projects/<repo>/runs/<run> \
  --additional-iterations 3
```

Não há botão Telegram nesta versão. Isso evita autorização parcial sem o mesmo
protocolo de `.resume.lock`, ledger e recuperação idempotente da CLI.

Interrupções `INT`, `TERM` e `HUP` marcam runs ativos como `BLOCKED`, enviam
notificação best-effort e preservam o worktree. O outbox usa identificador por
mensagem para não consumir uma notificação substituída durante envio.

```text
APPROVED → AWAITING_HUMAN_APPROVAL
                    ├─ Rejeitar → BLOCKED
                    └─ Aprovar  → HUMAN_APPROVED
                                      ├─ delivery=none → terminal
                                      └─ push_branch → delivery-job.json (pending)
                                                          └─ delivery-worker
                                                               → DELIVERING
                                                                   ├─ sucesso → PUSHED
                                                                   └─ falha → DELIVERY_FAILED
                                                                                └─ resume/worker → DELIVERING

CHANGES_REQUESTED em N = limite
  → BLOCKED/max_review_iterations
  → autorização atômica em iteration-budget.json
  → CHANGES_REQUESTED
  → executor em N+1 com o review-N.json
  → validações → reviewer → gate humano normal ou novo limite
```

O commit nasce de uma index temporária baseada no commit-base e no manifesto
exato, nunca de `git add -A`. FIFO/socket/device são recusados; artefatos
operacionais precisam estar ignorados. O push usa refspec explícito e confirma
o OID remoto. Para remotes GitHub reconhecidos, a mensagem final inclui links
sanitizados de branch e comparação; em outros providers mostra apenas remote e
branch. Falha dessa notificação não desfaz um push confirmado.

## Perfil, ambiente e retomada

O contrato DX-02 está em [`PROJECT_PROFILE.md`](PROJECT_PROFILE.md), com seu
registro histórico em [`tasks/DX-02.md`](tasks/DX-02.md), incluindo schema TOML,
bootstrap, ambiente externo `0600`, timeout por grupo de processos, heartbeat,
`agent-loop resume` e `agent-loop evidence`. O delivery opt-in está registrado
em [`tasks/DX-03.md`](tasks/DX-03.md).

## Limpeza

Não há limpeza automática. Depois de integrar ou descartar conscientemente:

```bash
git -C /projetos/alvo worktree list
git -C /projetos/alvo worktree remove /state/projects/<repo-id>/worktrees/<task-id>
```

Não force a remoção sem inspecionar mudanças pendentes.
