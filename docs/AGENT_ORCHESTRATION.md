# Orquestração externa — executor → validação → reviewer

## Fluxo

```text
task versionada
  → worktree externo isolado
  → executor (Cursor na implementação atual)
  → validações programáticas
  → reviewer separado (Codex na implementação atual)
  → CHANGES_REQUESTED retorna ao executor (orçamento inicial de 1 a 5 ciclos)
  → APPROVED técnico vinculado ao conteúdo revisado
  ├─ approval.mode=none → conclusão local
  └─ approval.mode=telegram → conclusão local + notificação terminal
  → verify opcional/read-only
  → integrate local somente quando explicitamente acionado pelo operador
```

Não há automação de decisão de produto, criação de task, push, tag, PR, deploy,
limpeza ou próxima task. `agent-loop integrate`, quando explicitamente acionado
pelo operador, automatiza **somente a integração Git local** (criação do commit
vinculado ao snapshot revisado + fast-forward). Publicação remota continua fora
do runtime.

A separação executor/reviewer é arquitetural; o runtime atual ainda está ligado
a Cursor/Codex como implementações concretas. Veja
[`ARCHITECTURE.md`](ARCHITECTURE.md) para guarantees/non-guarantees e
[`RESEARCH_OVERVIEW.md`](RESEARCH_OVERVIEW.md) para o enquadramento de pesquisa.

## Pré-requisitos

```bash
agent status
codex login status
venv/bin/pip install -r requirements.txt  # apenas testes locais
```

As credenciais das CLIs e do Telegram ficam fora do Git.
O runner recusa o Codex instalado via Snap, cujo confinamento não alcança os
worktrees externos. A resolução prioriza `CODEX_BIN`, depois
`~/.local/npm/bin/codex`, e ignora candidatos Snap encontrados no `PATH`.

## Comandos

Dry-run, sem criar worktree ou iniciar agentes:

```bash
./agent-loop run --repo /projetos/alvo --dry-run docs/tasks/TASK-01.md 3 main
```

Executar uma task versionada no `base-ref`:

```bash
./agent-loop run --repo /projetos/alvo docs/tasks/TASK-01.md 3 main
./agent-loop run --repo /projetos/alvo --allow-candidate-profile \
  docs/tasks/SELF-00A.md 3 main
```

Revisar mudanças já existentes no checkout atual:

```bash
./agent-loop review --repo /projetos/alvo docs/tasks/TASK-01.md
```

As opções `--ignore-orchestration` e `--evidence <arquivo>` estão disponíveis
para `review`. Evidência do executor é sempre tratada como não confiável e
confrontada com o diff.

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

Os hashes devem coincidir. O manifesto técnico em `APPROVED` vincula o valor de hash revisado nos modos
`none` e `telegram`, mas não torna o worktree imutável. A garantia é um binding
de aprovação ao conteúdo: drift posterior faz a verificação falhar.
Para uma verificação somente-leitura:

```bash
./agent-loop verify --run-dir /state/projects/<repo-id>/runs/<run-id>
```

A verificação retorna sucesso quando há aceite técnico terminal em `APPROVED`,
o manifesto corresponde ao perfil congelado e o hash atual ainda coincide.
Runs antigos em `HUMAN_APPROVED` continuam verificáveis somente para
compatibilidade de leitura.

Para criar o commit e avançar a branch local que ainda aponta exatamente para
o commit-base:

```bash
./agent-loop integrate --run-dir /state/projects/<repo-id>/runs/<run-id>
```

O integrador recompõe o commit pelo manifesto em um index temporário, desativa
hooks, verifica whitespace no diff completo e permite apenas fast-forward
local. Checkout sujo, drift, base
divergente, manifesto adulterado ou ausência de aprovação abortam sem alterar
a branch. Nenhum remoto é consultado ou modificado; push permanece manual.

## Telegram

Telegram é opcional. Selecione `[approval] mode = "telegram"` no perfil para
receber a mensagem terminal. A conclusão do run independe da ponte e da rede:
o estado fica `APPROVED` e o outbox pendente é reenviado quando o notifier
estiver disponível.

Crie um arquivo externo, por exemplo
`~/.config/codex-cursor-agent-loop/telegram.env`, com permissão `0600`:

```bash
AGENT_TELEGRAM_BOT_TOKEN=token-do-botfather
AGENT_TELEGRAM_ALLOWED_CHAT_ID=123456
```

Opcionalmente configure `AGENT_TELEGRAM_API_BASE`. Inicie em foreground:

```bash
AGENT_TELEGRAM_CREDENTIALS_FILE=~/.config/codex-cursor-agent-loop/telegram.env \
  ./agent-loop serve
```

A ponte é exclusivamente de saída: usa `sendMessage`, não abre porta pública,
não consulta `getUpdates`, não recebe callbacks e não aceita comandos de shell.
Falha de rede não altera o estado terminal. Execute somente uma ponte por bot:
ela varre os runs de todos os projetos e mantém uma trava local por token. Uma
segunda instância, mesmo configurada com outro state root, encerra antes de
duplicar entregas.

Ao concluir o run, o Telegram recebe ID/título, repositório, base, iteração, hash,
arquivos, estatísticas, executor, testes/validações, reviewer, findings, riscos
e documentação — nunca o diff completo. Texto não usa `parse_mode`, URLs e
atribuições sensíveis são redigidas e campos grandes são truncados
explicitamente. Mensagens longas são numeradas e nenhuma contém botões.
A contagem de testes vem somente do último `validation-N.log` concluído que
contenha um resultado terminal. Marcadores `passed=N failed=N skipped=N` têm
precedência; relatórios do executor, diffs, documentação, logs do reviewer,
validações anteriores e diagnósticos de self-tests negativos não são somados.
Cada `message_id` é persistido depois da resposta bem-sucedida do Telegram,
reduzindo reenvios. A semântica permanece *at-least-once*: uma queda entre envio
e persistência pode duplicar uma mensagem.

```text
(1/1)
TASK-01 — Descrição curta da mudança

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
- README.md
- docs/tasks/TASK-01.md

Run finalizada. A integração local exige comando explícito do operador.
```

Depois de todos os chunks do resumo, o notifier envia uma mensagem separada,
determinística e editável. Ela não executa Git nem altera a integração:

```text
Mensagem de commit sugerida:

TASK-01: implementa Descrição curta da mudança
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

### Limite intencional da unidade

A ponte registra somente a entrega da notificação. Ela não executa Git e não escreve no
repositório. O `EnvironmentFile` coloca o token do
bot somente nesse processo; não existe worker de push no produto estável.

## Estados e falhas

Todas as mudanças abaixo passam pela máquina central de eventos sob
`.state.lock`. Metadata e status ficam no único `state.json`; replays
idempotentes não reescrevem o arquivo e uma aresta incompatível falha sem
mutação. A ordem quando há composição é
`.resume.lock` → `.approval.lock` → `.state.lock`.

- `EXECUTING`: Cursor trabalhando;
- `REVIEWING`: Codex avaliando;
- `CHANGES_REQUESTED`: feedback retornará ao Cursor;
- `APPROVED`: aceite técnico terminal, aguardando eventual `integrate` explícito;
- `AWAITING_HUMAN_APPROVAL` e `HUMAN_APPROVED`: estados legados, aceitos somente
  para migração/verificação de runs antigos;
- `BLOCKED`: falha, interrupção, dependência externa ou limite atingido.

Quando a causa for exclusivamente `max_review_iterations`, a notificação
informa que worktree e último feedback foram preservados. A continuação exige
CLI explícita:

```bash
./agent-loop resume --run-dir /state/projects/<repo>/runs/<run> \
  --additional-iterations 3
```

Não há botão nem decisão humana pelo Telegram. A integração continua sendo uma
ação manual separada.

Interrupções `INT`, `TERM` e `HUP` marcam runs ativos como `BLOCKED` e preservam
o worktree. Quando Telegram está habilitado, também enfileiram uma notificação
best-effort. O outbox usa identificador por mensagem para não consumir uma
notificação substituída durante envio.

```text
APPROVED
  ├─ mode=none     → verify opcional → integrate explícito/local
  └─ mode=telegram → enfileira notificação sem ações
                     → verify opcional → integrate explícito/local

CHANGES_REQUESTED em N = limite
  → BLOCKED/max_review_iterations
  → autorização atômica em state.json.iteration_budget
  → CHANGES_REQUESTED
  → executor em N+1 com o review-N.json
  → validações → reviewer → APPROVED ou novo limite
```

FIFO/socket/device são recusados no snapshot; artefatos operacionais precisam
estar ignorados. Depois do aceite, cabe ao operador decidir conscientemente entre descartar,
inspecionar/`verify` ou executar `agent-loop integrate`. A publicação remota, se
desejada, continua sendo feita fora do runtime.

## Perfil, ambiente e retomada

O contrato atual está em [`PROJECT_PROFILE.md`](PROJECT_PROFILE.md), incluindo
schema TOML, adapter congelado versus profile candidato, bootstrap, ambiente
externo `0600`, timeout por scope `systemd --user`, heartbeat, `agent-loop resume`
e `agent-loop evidence`. Sem `--allow-candidate-profile`, a interface pública de
`run`/`resume`/`verify`/`integrate` permanece a mesma para consumidores
externos.

## Limpeza

Não há limpeza automática. Depois de integrar ou descartar conscientemente:

```bash
git -C /projetos/alvo worktree list
git -C /projetos/alvo worktree remove /state/projects/<repo-id>/worktrees/<task-id>
```

Não force a remoção sem inspecionar mudanças pendentes.
