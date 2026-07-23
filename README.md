# Codex Cursor Agent Loop

Runner externo para executar uma task com Cursor Agent, revisar o resultado com
Codex e exigir aprovação humana auditável pelo Telegram.

Projetos consumidores podem declarar bootstrap, ambiente allowlisted, timeouts,
heartbeat, validações, documentação obrigatória e entrega opt-in em branch em
`.agent-loop/project.toml`. Runs interrompidos
podem ser retomados sem descartar o worktree, e evidência complementar permanece
não confiável até nova revisão. Veja [Perfil e retomada segura](docs/PROJECT_PROFILE.md).

Por padrão o runner não faz commit nem push. Quando o projeto habilita
explicitamente `delivery.mode = "push_branch"`, ele cria um único commit do
snapshot aprovado e envia somente a branch congelada. Nunca faz merge, push em
`main`/`master`, force-push, tag, PR, deploy, limpeza destrutiva ou próxima task.

## Preparação

O runtime usa apenas Python 3 e ferramentas do sistema. `pytest` é necessário
somente para desenvolvimento:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Também são necessários `git`, `flock`, Cursor Agent e Codex CLI autenticados.

## Uso externo

O projeto-alvo não recebe scripts nem estado do runner:

```bash
./agent-loop run --repo /caminho/do/projeto docs/tasks/CP-00.md 3 main
./agent-loop review --repo /caminho/do/projeto docs/tasks/CP-00.md
./agent-loop resume --run-dir /caminho/externo/para/o/run
./agent-loop resume --run-dir /caminho/externo/para/o/run --additional-iterations 3
./agent-loop evidence --run-dir /caminho/externo/para/o/run --file /tmp/relatorio.txt
```

Por padrão, runs e worktrees ficam em:

```text
$XDG_STATE_HOME/codex-cursor-agent-loop/projects/<nome-hash>/
```

Sem `XDG_STATE_HOME`, usa `~/.local/state`. `--state-root` permite outro local.
O identificador inclui o caminho canônico do Git, isolando repositórios com o
mesmo nome e aliases por symlink.

## Gate humano

Configure token e IDs numéricos fora do Git conforme
[`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md), então execute:

```bash
./agent-loop serve
```

Uma única ponte descobre runs de múltiplos projetos no state root. O Telegram
envia o resumo técnico em partes numeradas; somente a última contém os botões
**Aprovar e publicar branch** e **Rejeitar**. Depois de `HUMAN_APPROVED`,
projetos sem entrega automática podem validar decisão e snapshot manualmente:

```bash
./agent-loop verify --run-dir /caminho/externo/para/o/run
```

O comando falha se não houver decisão humana válida ou se o worktree divergir
do hash revisado.

Com entrega habilitada, a aprovação transiciona por `DELIVERING` até `PUSHED`.
Uma falha preserva aprovação, worktree e commit local em `DELIVERY_FAILED`:

```bash
./agent-loop resume --run-dir /caminho/externo/para/o/run
```

A retomada repete somente a entrega; Cursor e Codex não executam novamente.

## Extensão explícita de iterações

Quando — e somente quando — o reviewer devolve `CHANGES_REQUESTED` na última
iteração e o run termina em `BLOCKED` com motivo estruturado
`max_review_iterations`, é possível autorizar novo orçamento:

```bash
./agent-loop resume \
  --run-dir /state/projects/<repo>/runs/<run> \
  --additional-iterations 3
```

Cada extensão aceita de 1 a 20 iterações; o limite efetivo total é 50. O
`max_iterations` original em `run.json` não muda. A cadeia auditável fica em
`iteration-budget.json`, vinculada ao último feedback e hash revisado. Repetir o
mesmo comando durante a extensão ativa é idempotente. Outras causas de
`BLOCKED`, drift, estados de aprovação/delivery e combinação com
`--review-only` são recusados sem novo orçamento.

## systemd --user

Gere a unidade com os caminhos reais da instalação:

```bash
./agent-loop systemd-unit \
  --output ~/.config/systemd/user/agent-telegram-bridge.service
systemd-analyze verify ~/.config/systemd/user/agent-telegram-bridge.service
```

O comando apenas gera o arquivo; não habilita nem inicia o serviço.

## Estrutura

- `agent-loop`: CLI externa (`run`, `review`, `resume`, `evidence`, `serve`, `verify`, `systemd-unit`);
- `scripts/agents/`: executor, revisor e ponte Telegram;
- `scripts/agents/dx/`: estado, hash, concorrência e cliente Bot API;
- `.agents/reviewer-output.schema.json`: contrato de saída do revisor;
- `tests/unit/`: suíte focada;
- `archive/`: evidências históricas, ignoradas pelo Git.
