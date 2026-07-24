# Codex Cursor Agent Loop

Runner externo para executar uma task com Cursor Agent, revisar o resultado com
Codex e exigir aprovação humana auditável pelo Telegram.

Projetos consumidores podem declarar bootstrap, ambiente allowlisted, timeouts,
heartbeat, validações, documentação obrigatória e entrega opt-in em branch em
`.agent-loop/project.toml`. Runs interrompidos
podem ser retomados sem descartar o worktree, e evidência complementar permanece
não confiável até nova revisão. Veja [Perfil e retomada segura](docs/PROJECT_PROFILE.md).

O projeto ainda está em estágio pré-alpha. O caminho até uma distribuição
confiável para terceiros, com gates objetivos de segurança, CI, empacotamento e
release, está no [roadmap](ROADMAP.md).

Por padrão o runner não faz commit nem push. Quando o projeto habilita
explicitamente `delivery.mode = "push_branch"`, ele cria um único commit do
snapshot aprovado e envia somente a branch congelada. Nunca faz merge, push em
`main`/`master`, force-push, tag, PR, deploy, limpeza destrutiva ou próxima task.

## Preparação

O runtime requer Python 3.11 ou posterior e ferramentas do sistema. `pytest` é
necessário somente para desenvolvimento:

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

Execute somente uma instância da ponte por state root; essa exclusividade ainda
não é imposta pelo processo. A ponte descobre runs de múltiplos projetos. O
Telegram envia o resumo técnico em partes numeradas; somente a última contém os
botões **Aprovar e publicar branch** e **Rejeitar**. O texto do primeiro botão é
fixo nesta versão: em projetos sem entrega automática ele registra apenas
`HUMAN_APPROVED`, sem criar commit ou branch. Esses projetos podem validar
decisão e snapshot manualmente:

```bash
./agent-loop verify --run-dir /caminho/externo/para/o/run
```

O comando falha se não houver decisão humana válida ou se o worktree divergir
do hash revisado.

Com entrega habilitada, a aprovação publica um `delivery-job.json` pendente e
responde imediatamente ao Telegram. Um worker separado conclui
`DELIVERING` → `PUSHED`. Sem worker ativo o run permanece aprovado e pendente,
nunca falsamente `PUSHED`. Uma falha preserva aprovação e worktree em
`DELIVERY_FAILED`; quando o commit já foi criado e registrado, ele também é
reutilizado pela retomada:

```bash
./agent-loop delivery-worker --run-dir /caminho/externo/para/o/run --once
./agent-loop resume --run-dir /caminho/externo/para/o/run
```

A retomada assegura o job e processa a entrega em one-shot; Cursor e Codex não
executam novamente.

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

> **Limitação atual:** a unidade endurecida libera escrita somente no state root,
> enquanto delivery precisa escrever no Git common dir. Após DX-05 a bridge só
> enfileira o job; execute `agent-loop delivery-worker` (ou `resume`) fora da
> unidade da bridge. Não amplie `ReadWritePaths` de forma genérica. O
> `EnvironmentFile` ainda coloca o token do Telegram no ambiente da ponte —
> hardening do worker e unidade dedicada sem o token estão na DX-06. Não use
> `push_branch` com hooks ou `core.hooksPath` não confiáveis até essa separação.

## Estrutura

- `agent-loop`: CLI externa (`run`, `review`, `resume`, `evidence`, `serve`, `verify`, `delivery-worker`, `systemd-unit`);
- `scripts/agents/`: executor, revisor e ponte Telegram;
- `scripts/agents/dx/`: estado, hash, concorrência, fila de delivery e cliente Bot API;
- `.agents/reviewer-output.schema.json`: contrato de saída do revisor;
- `tests/unit/`: suíte focada;
- `ROADMAP.md`: marcos e gates para uso confiável por terceiros;
- `archive/`: evidências históricas, ignoradas pelo Git.
