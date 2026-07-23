# Orquestração local Codex → Cursor → Codex

## Objetivo

Automatizar o ciclo de uma task já aprovada pelo planner:

```text
task versionada
  -> Cursor executa em worktree isolado
  -> Codex revisa diff, aceite e testes
  -> feedback retorna ao Cursor
  -> APPROVED técnico, BLOCKED ou limite de três ciclos
  -> AWAITING_HUMAN_APPROVAL (Telegram)
  -> HUMAN_APPROVED (operador autorizado)
```

O fluxo não automatiza decisão de produto, criação da task, commit, push, merge, deploy nem ação destrutiva.

## Componentes

- `scripts/agents/run_task.sh`: loop executor/revisor para novas tasks;
- `scripts/agents/review_current.sh`: revisão única do checkout atual, útil para a implementação local da `LC-01`;
- `scripts/agents/telegram_bridge.py`: ponte local Telegram (long polling; DX apenas);
- `scripts/agents/dx/`: contrato de aprovação humana e cliente Bot API;
- `scripts/agents/telegram-bridge.service`: unidade `systemd --user` de exemplo (não instala sozinha);
- `.agents/reviewer-output.schema.json`: contrato do veredito do Codex;
- `.agents/runs/`: logs e vereditos locais ignorados pelo Git;
- `.agents/worktrees/`: worktrees locais isolados e ignorados pelo Git.

## Pré-requisitos

```bash
agent status
codex login status
```

Se `codex` não estiver no `PATH`, os scripts procuram automaticamente o binário
instalado pelas extensões OpenAI do VS Code e do Cursor. Para usar outro binário:

```bash
CODEX_BIN=/caminho/para/codex scripts/agents/review_current.sh docs/tasks/LC-01.md
```

Se o Cursor Agent ainda não estiver autenticado:

```bash
agent login
```

Credenciais não devem ser gravadas no projeto. O script usa a autenticação local das duas CLIs.

## Dry-run

Valide caminhos, baseline, executáveis e autenticação sem iniciar agentes:

```bash
scripts/agents/run_task.sh --dry-run docs/tasks/LC-01.md 3 HEAD
```

## Revisar a implementação atual da LC-01

A implementação da `LC-01` já existe como diff não commitado no checkout atual. Revise-a sem criar outro worktree:

```bash
scripts/agents/review_current.sh --ignore-orchestration docs/tasks/LC-01.md
```

O relatório fica em `.agents/runs/`. O revisor é impedido por verificação de integridade de alterar o diff silenciosamente.
Essa opção declara somente os arquivos do próprio orquestrador como mudança paralela já autorizada; demais alterações fora do escopo continuam sendo reportadas.

Quando os testes dependem de infraestrutura acessível ao executor, mas isolada do sandbox do revisor, passe o relatório salvo pelo executor:

```bash
scripts/agents/review_current.sh --ignore-orchestration \
  --evidence .agents/runs/<execucao>/cursor-1.json \
  docs/tasks/LC-01.md
```

A evidência é tratada como não confiável e confrontada com o diff; ela não substitui a revisão nem garante aprovação.

## Executar uma task nova

A task deve estar versionada no `base-ref`; alterações não commitadas do checkout principal não são copiadas para o worktree.

```bash
scripts/agents/run_task.sh docs/tasks/CP-00.md 3 main
```

O script:

1. confirma task, baseline, CLIs e autenticação;
2. impede dois loops simultâneos;
3. cria `.agents/worktrees/<task-id>` em detached HEAD;
4. executa o Cursor com sandbox e auto-review, sem `--force`;
5. executa o Codex com saída JSON estruturada;
6. detecta se o revisor alterou arquivos;
7. retorna `CHANGES_REQUESTED` ao executor ou encerra;
8. em `APPROVED` técnico, grava solicitação humana com o `diff_hash` revisado
   (antes=depois do Codex) e espera `HUMAN_APPROVED`.

Ao receber `HUMAN_APPROVED`, o worktree continua disponível para inspeção. O
planner **deve** verificar o snapshot antes de integrar:

```bash
python3 scripts/agents/dx/cli.py verify-reviewed-snapshot --run-dir .agents/runs/<run-id>
```

`HUMAN_APPROVED` aprova o `diff_hash` imutável revisado pelo Codex — **não**
congela o worktree. Se o hash atual divergir, a integração deve parar. Timeout
ou bridge indisponível preservam `AWAITING_HUMAN_APPROVAL` (nunca aprovam
sozinhos).

## Gate humano via Telegram (DX-01)

Melhoria local de developer experience. **Não** entra no runtime SaaS, API, banco ou containers do produto.

### Protocolo de estados

| Estado | Significado |
|--------|-------------|
| `APPROVED` | Aceite **técnico** do Codex (registrado no request; não é aprovação humana) |
| `AWAITING_HUMAN_APPROVAL` | Solicitação gravada; aguarda botão no Telegram |
| `HUMAN_APPROVED` | Operador autorizado aprovou o **mesmo** `run_id` + `diff_hash` revisado |
| `BLOCKED` | Falha/limite; só notificação, **sem** botão de aprovação |

### Snapshot content-addressed (vínculo run/diff)

1. Imediatamente **antes** e **depois** do Codex, o loop calcula o mesmo
   `compute-diff-hash` canônico (diff binário + untracked ordenados).
2. Os dois hashes devem ser iguais; caso contrário o run fica `BLOCKED`
   (revisor alterou o tree).
3. Esse hash revisado entra em `human_approval_request.json` e na decisão
   humana. O callback **não** revalida o worktree ao vivo (um flock de run não
   congela escritas arbitrárias no tree).
4. `HUMAN_APPROVED` significa: o operador aprovou aquele hash imutável.
5. Antes de integrar, o planner executa `verify-reviewed-snapshot` e só segue
   se `matches=true`.

Artefatos no run directory (escrita atômica; decisão via temp fsync + hard-link exclusivo):

- `human_approval_request.json` — task, run id, base commit, worktree, relatório, `diff_hash` revisado, token opaco, `token_consumed`;
- `human_approval_decision.json` — decisão auditável ligada a run/diff/token + user/chat Telegram numéricos;
- `telegram_notify.json` — outbox da ponte (falha de envio não altera aprovação).

Decisões mínimas/forjadas (sem token, schema, `token_consumed`, ou campos
Telegram autenticados) **não** promovem `HUMAN_APPROVED`.
### Configuração (sem segredos no Git)

1. Crie o bot com BotFather e anote o token **fora** do repositório.
2. Descubra seu `user_id` / `chat_id` numéricos (username **não** autentica).
3. Copie o exemplo e preencha valores reais somente no arquivo local:

```bash
mkdir -p ~/.config/chatbot-artang
cp scripts/agents/telegram-bridge.env.example ~/.config/chatbot-artang/agent-telegram.env
chmod 600 ~/.config/chatbot-artang/agent-telegram.env
```

Variáveis: `AGENT_TELEGRAM_BOT_TOKEN`, `AGENT_TELEGRAM_ALLOWED_USER_ID`,
`AGENT_TELEGRAM_ALLOWED_CHAT_ID`. Opcional: `AGENT_TELEGRAM_CREDENTIALS_FILE`,
`AGENT_HUMAN_APPROVAL_TIMEOUT_SEC` (padrão 3600).

### Serviço `systemd --user` (usuário `cherubim`, nunca root)

A unidade de exemplo **não** é instalada nem habilitada automaticamente:

```bash
# Revisar caminhos em scripts/agents/telegram-bridge.service, depois:
mkdir -p ~/.config/systemd/user
cp scripts/agents/telegram-bridge.service \
  ~/.config/systemd/user/agent-telegram-bridge.service
systemctl --user daemon-reload
# Somente após revisar:
# systemctl --user enable --now agent-telegram-bridge.service
```

A ponte usa long polling (`getUpdates`). Não há webhook, porta HTTP pública nem comandos
`/run` `/stop` `/logs`. Remetentes não allowlisted recebem resposta neutra.

## Estados

- `EXECUTING`: Cursor trabalhando;
- `REVIEWING`: Codex avaliando;
- `CHANGES_REQUESTED`: feedback retornará ao Cursor;
- `APPROVED`: aceite técnico automatizado (não encerra o gate humano);
- `AWAITING_HUMAN_APPROVAL`: aguardando botão Telegram do operador allowlisted;
- `HUMAN_APPROVED`: aprovação humana auditável; worktree preservado para o planner;
- `BLOCKED`: falha, dependência externa, evidência insuficiente ou limite atingido.

O estado atual de cada execução fica no arquivo `status` do respectivo diretório em `.agents/runs/`.

## Limites de segurança

- máximo configurável entre um e cinco ciclos; padrão recomendado: três;
- nenhum `--force`/`--yolo` no Cursor;
- nenhum bypass de sandbox no Codex;
- nenhuma credencial em prompt, task, relatório ou Git;
- nenhuma automação de commit, push, merge ou deploy;
- somente uma task automatizada por vez no repositório;
- o planner continua responsável por atualizar o `ROADMAP.md` após a revisão;
- o bot Telegram não executa shell, não inicia a próxima task e não integra Git.

## Limpeza

Não há limpeza automática para evitar perda acidental. Depois de integrar ou descartar conscientemente uma execução, inspecione e remova o worktree com Git:

```bash
git worktree list
git worktree remove .agents/worktrees/<task-id>
```

Não use remoção forçada sem revisar mudanças pendentes.
