# Orquestração externa Codex → Cursor → Codex

## Fluxo

```text
task versionada
  → worktree externo isolado
  → Cursor executa
  → Codex revisa diff, aceite e testes
  → CHANGES_REQUESTED retorna ao Cursor (máximo de 1 a 5 ciclos)
  → APPROVED técnico
  → AWAITING_HUMAN_APPROVAL
  → HUMAN_APPROVED para o hash revisado
  → verificação obrigatória do snapshot antes de integrar
```

Não há automação de decisão de produto, criação de task, commit, merge, push,
deploy, limpeza ou próxima task.

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

A verificação retorna sucesso somente quando há uma decisão humana válida,
status `HUMAN_APPROVED` e o hash atual ainda coincide.

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
rede nunca promove estado. Uma única ponte varre os runs de todos os projetos.

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

## Estados e falhas

- `EXECUTING`: Cursor trabalhando;
- `REVIEWING`: Codex avaliando;
- `CHANGES_REQUESTED`: feedback retornará ao Cursor;
- `APPROVED`: aceite técnico, nunca humano;
- `AWAITING_HUMAN_APPROVAL`: botão pendente;
- `HUMAN_APPROVED`: decisão autenticada para o hash revisado;
- `BLOCKED`: falha, interrupção, dependência externa ou limite atingido.

Interrupções `INT`, `TERM` e `HUP` marcam runs ativos como `BLOCKED`, enviam
notificação best-effort e preservam o worktree. O outbox usa identificador por
mensagem para não consumir uma notificação substituída durante envio.

## Limpeza

Não há limpeza automática. Depois de integrar ou descartar conscientemente:

```bash
git -C /projetos/alvo worktree list
git -C /projetos/alvo worktree remove /state/projects/<repo-id>/worktrees/<task-id>
```

Não force a remoção sem inspecionar mudanças pendentes.
