# Codex Cursor Agent Loop

Runner externo para executar uma task com Cursor Agent, revisar o resultado com
Codex e preservar um snapshot verificável. A decisão humana pelo Telegram é
opcional.

A implementação atual é o [Personal Core v2](docs/PERSONAL_CORE_V2.md): um
núcleo pequeno, exclusivamente local e voltado ao uso pessoal. A linha anterior
permanece disponível no histórico Git, sem seus mecanismos de transactions,
migrations e audit trail no runtime atual.

Projetos consumidores podem declarar bootstrap, ambiente allowlisted, timeouts,
heartbeat, validações e documentação obrigatória em
`.agent-loop/project.toml`. Runs interrompidos
podem ser retomados sem descartar o worktree, e evidência complementar permanece
não confiável até nova revisão. Veja [Perfil e retomada segura](docs/PROJECT_PROFILE.md).

As mudanças de estado passam por uma tabela tipada e compare-and-set sob
`.state.lock`; eventos inválidos falham sem substituir o estado anterior.

O projeto é mantido para uso pessoal, sem compromisso de distribuição ou
compatibilidade para terceiros.

O runner não faz commit, push, merge, tag, PR ou deploy. Após o aceite técnico
ou humano, ele preserva o worktree e o hash revisado para integração Git manual.
Não existe configuração de publicação; uma tabela `[delivery]` é recusada como
desconhecida.

Executor, validações e reviewer recebem `GIT_ALLOW_PROTOCOL=file`; Git recusa
transportes remotos antes de abrir conexão, inclusive quando chamado por caminho
absoluto. Isso não é um namespace de rede e não bloqueia clientes HTTP genéricos;
repositórios deliberadamente hostis permanecem fora do modelo suportado.

## Preparação

O runtime requer Python 3.11 ou posterior e ferramentas do sistema. `pytest` é
necessário somente para desenvolvimento:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Também são necessários `git`, `flock`, `systemd-run`, `systemctl`, `prlimit`,
Cursor Agent e Codex CLI autenticados.

## Uso externo

O projeto-alvo não recebe scripts nem estado do runner:

```bash
./agent-loop run --repo /caminho/do/projeto docs/tasks/TASK-01.md 3 main
./agent-loop review --repo /caminho/do/projeto docs/tasks/TASK-01.md
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

## Aprovação opcional

O perfil escolhe explicitamente:

```toml
[approval]
mode = "none"      # termina em APPROVED após review e verificação local
# mode = "telegram"  # abre o gate humano existente
```

O default de compatibilidade é `telegram`. Em `none`, nenhum request ou outbox
Telegram é criado; `verify` aceita o manifesto técnico congelado. Em `telegram`,
configure token e IDs numéricos fora do Git conforme
[`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md), então execute:

```bash
./agent-loop serve
```

Execute somente uma instância da ponte por state root; essa exclusividade ainda
não é imposta pelo processo. A ponte descobre runs de múltiplos projetos. O
Telegram envia o resumo técnico em partes numeradas; somente a última contém os
botões **Aprovar alterações** e **Rejeitar**. A aprovação registra apenas
`HUMAN_APPROVED`; não cria job, commit ou branch e não acessa a rede Git.
Antes de integrar manualmente, valide novamente decisão e snapshot:

```bash
./agent-loop verify --run-dir /caminho/externo/para/o/run
```

O comando falha se o modo configurado não tiver seu aceite válido ou se o
worktree divergir do hash revisado.

Depois da verificação, inspecione o worktree preservado e execute
`git add`/`commit`/`push` conscientemente no seu fluxo normal. Esses comandos
não são executados pelo runner.

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
`max_iterations` original na metadata de `state.json` não muda. A cadeia
auditável fica no campo `iteration_budget` do mesmo arquivo, vinculada ao último
feedback e hash revisado. Repetir o mesmo comando durante a extensão ativa é
idempotente. Outras causas de `BLOCKED`, drift, estados de aprovação e
combinação com `--review-only` são recusados sem novo orçamento.

## systemd --user

Gere a unidade com os caminhos reais da instalação:

```bash
./agent-loop systemd-unit \
  --output ~/.config/systemd/user/agent-telegram-bridge.service
systemd-analyze verify ~/.config/systemd/user/agent-telegram-bridge.service
```

O comando apenas gera o arquivo; não habilita nem inicia o serviço.

A unidade endurecida libera escrita somente no state root. A bridge não
executa Git e não precisa escrever no repositório.

## Estrutura

- `agent-loop`: CLI externa (`run`, `review`, `resume`, `evidence`, `serve`, `verify`, `systemd-unit`);
- `scripts/agents/`: executor, revisor e ponte Telegram;
- `scripts/agents/dx/`: estado, hash, concorrência, aprovação e cliente Bot API;
- `.agents/reviewer-output.schema.json`: contrato de saída do revisor;
- `tests/unit/`: suíte focada;
- `docs/`: contrato atual, operação, perfil e exemplos mínimos.
