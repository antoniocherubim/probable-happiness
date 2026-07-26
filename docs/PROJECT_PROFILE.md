# Perfil de projeto e retomada segura

O motor continua externo. Um repositório consumidor pode rastrear somente sua
integração em `.agent-loop/project.toml`, instruções Markdown e scripts de
bootstrap/teste. Estado, worktrees, evidências e credenciais permanecem no XDG.
Um exemplo completo está em [`docs/examples/project.toml`](examples/project.toml).

## Schema `project.toml` (versão 1)

O parser é estrito: tabelas/chaves desconhecidas, tipos incorretos, comandos
vazios, variáveis inválidas e caminhos absolutos/com `..` são recusados. Cada
comando é um array `argv`; nenhum valor passa por `eval` ou shell implícito.

| Campo | Tipo | Default/restrição |
|---|---|---|
| `schema_version` | inteiro | obrigatório, `1` |
| `bootstrap.command` | array de strings | opcional |
| `bootstrap.timeout_seconds` | inteiro | `300`, 1–86400 |
| `executor.timeout_seconds` | inteiro | `1800`, 1–86400 |
| `executor.heartbeat_seconds` | inteiro | `30`, 1–3600 |
| `reviewer.timeout_seconds` | inteiro | `1800`, 1–86400 |
| `reviewer.heartbeat_seconds` | inteiro | `30`, 1–3600 |
| `environment.required` | nomes de variável | vazio, sem duplicatas |
| `validation.commands` | arrays `argv` | vazio, máximo 32 |
| `instructions.executor/reviewer` | caminhos relativos | vazio, 256 KiB/arquivo |
| `documentation.required` | booleano | `false` |
| `documentation.required_paths` | templates relativos | vazio; `{task_id}`, `{task_slug}` |
| `policy.missing_profile` | `allow` ou `deny` | `allow` |
| `policy.terminate_grace_seconds` | inteiro | `5`, 1–300 |

Projetos sem perfil usam defaults seguros e o formato antigo continua válido.
Use `--require-profile` para bloquear a criação de um run sem o arquivo. O valor
`policy.missing_profile = "deny"` documenta a política quando o perfil existe; a
flag é a proteção aplicável quando ele está ausente.

Templates são analisados sem `eval`. Documentação aceita somente `{task_id}` e
`{task_slug}`. Placeholder desconhecido e caminho absoluto/com `..` bloqueiam o
preflight.

## Documentação obrigatória

Quando `documentation.required = true`, cada caminho renderizado deve ter sido
criado ou alterado no snapshot final. O executor recebe instrução para registrar
comportamento, testes e riscos; o reviewer valida a precisão. Ausência bloqueia
o gate humano. O loop não edita documentação por heurística e não exige SHA ou
URL de uma branch que ainda não existe.

## Aprovação local

Após a decisão humana, o loop termina em `HUMAN_APPROVED` e preserva o
worktree. Não cria index, commit, branch nem conexão Git remota.

Use `agent-loop verify --run-dir ...` imediatamente antes da integração manual.
Profiles com uma tabela `[delivery]` são recusados; remova a tabela inteira.

## Bootstrap e ambiente

O bootstrap roda no worktree depois de `git worktree add` e antes do Cursor. Ele
recebe somente o ambiente operacional mínimo, variáveis allowlisted e:

- `AGENT_LOOP_TARGET_REPO`;
- `AGENT_LOOP_WORKTREE`;
- `AGENT_LOOP_RUN_DIR`;
- `AGENT_LOOP_TASK_FILE`;
- `AGENT_LOOP_BASE_COMMIT`.

Ao terminar, qualquer alteração rastreada (working tree ou index) bloqueia o run.
Arquivos ignorados, como `.venv/`, podem ser criados ou vinculados.

```bash
./agent-loop run --repo /repo \
  --env-file ~/.config/codex-cursor-agent-loop/projects/<repo-id>/test.env \
  docs/tasks/TASK.md 3 main
```

Se a flag for omitida e esse arquivo XDG existir, ele é descoberto
automaticamente. Deve ser regular, não symlink, do usuário atual e `0600` (ou
mais restritivo). Chaves extras são ignoradas; somente nomes em
`environment.required` chegam ao bootstrap, Cursor e validações. Logs mostram
apenas `NOME=set|unset`, substituem valores por `[REDACTED]` e URLs por
`[REDACTED_URL]` nos artefatos finais. Durante a execução, stdout e stderr passam
por arquivos temporários brutos antes da sanitização; uma morte abrupta do
supervisor pode deixá-los no run directory. Proteja o state root contra outros
usuários locais e inspecione/remova esses arquivos após uma interrupção anormal.

## Timeout, scope systemd e heartbeat

Cada fase exige um scope transitório `systemd --user`. No timeout, o supervisor
envia `SIGTERM` a todo o cgroup, aguarda
`policy.terminate_grace_seconds`, usa `SIGKILL` se necessário e confirma que o
scope ficou inativo. Um descendente que crie outra sessão continua no cgroup. O
worktree permanece; o campo `failure` de `state.json` registra
`executor_timeout`, `reviewer_timeout`, `*_empty_report` etc., e o status fica
`BLOCKED`. Saída vazia nunca é sucesso. Sem acesso ao manager do usuário, a fase
é recusada antes de executar o comando.

Durante a fase, `heartbeat.json` é substituído atomicamente e uma linha segura
mostra fase, iteração, elapsed, PID, unidade systemd, última atividade, arquivos
modificados e estado. Nenhum conteúdo ou ambiente entra no heartbeat.

## Máquina de estados de retomada

O planner valida as precondições de domínio e então emite um evento tipado. A
publicação usa compare-and-set sob `.state.lock`; não existe mais writer
genérico capaz de atribuir um status arbitrário.

```text
EXECUTING/interrompido  -> executor da mesma iteração
REVIEWING/interrompido  -> nova revisão do snapshot pré-revisão
CHANGES_REQUESTED       -> executor da próxima iteração
BLOCKED + --review-only -> nova revisão do snapshot atual
BLOCKED/max_review_iterations + orçamento explícito -> executor em N+1
AWAITING_HUMAN_APPROVAL -> apenas retoma wait-decision
HUMAN_APPROVED          -> valida decisão/hash; não repete gate
```

```bash
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id>
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id> --review-only
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id> \
  --additional-iterations 3
```

O wrapper mantém `.resume.lock` durante toda a retomada. Antes de iniciar,
valida metadados, task no base commit, `HEAD`, repositório comum do worktree,
perfil congelado e hash pré-revisão. Drift durante/depois da revisão ou no gate
humano é recusado. Um `APPROVED` isolado sempre volta a uma nova revisão.

### Orçamento de iterações

`--additional-iterations` aceita de 1 a 20 e nunca altera o limite original em
`state.json`; o limite efetivo acumulado não pode ultrapassar 50. A autorização
exige simultaneamente:

- status `BLOCKED` e
  `state.json.failure.reason = "max_review_iterations"` (o legado seguro
  `"max_iterations"` é reconhecido para runs anteriores);
- cursor igual ao limite efetivo e último `review-N.json` em
  `CHANGES_REQUESTED`;
- resultado do reviewer concluído, executor report presente e worktree igual ao
  `review-N-snapshot.json`;
- ausência de artefatos de aprovação e locks concorrentes.

O campo `iteration_budget` de `state.json` contém `schema_version`, `run_id`,
limites original e efetivo e uma cadeia de extensões. Cada item registra
incremento, limites anterior/novo, origem `cli`, timestamp, iteração, hashes do
feedback/snapshot e um `idempotency_id` determinístico. Orçamento e status são
publicados juntos sob `.resume.lock` e `.state.lock`; repetições enquanto a
extensão está ativa não somam orçamento.

O feedback autorizado permanece no mesmo `review-N.json`; o executor seguinte
começa em `N+1`. Alterar o orçamento, feedback ou snapshot rompe os bindings e
impede a retomada quando a alteração atinge os campos vinculados. Os timestamps
`authorized_at`/`updated_at` não participam do identificador determinístico, e
os hashes não autenticam um adversário com o mesmo usuário capaz de reescrever
artefatos e recalculá-los. O botão Telegram de extensão não faz parte do DX-04:
fica como follow-up para evitar uma segunda superfície de autorização nesta
entrega.

## Evidência complementar

```bash
./agent-loop evidence --run-dir /state/.../runs/<run-id> --file /tmp/report.txt
./agent-loop resume --run-dir /state/.../runs/<run-id> --review-only
```

A origem é aberta com `O_NOFOLLOW`, deve ser regular e ter no máximo 1 MiB.
FIFO, socket, device, symlink, troca de inode e destino adulterado são recusados.
A cópia recebe nome pelo SHA-256, modo `0600`, timestamp e `trust = "untrusted"`.
Anexar não altera status. Somente uma nova revisão pode abrir o gate humano.

## Riscos residuais

- O motor não provisiona bancos/containers; o bootstrap somente prepara ou
  verifica recursos autorizados pelo projeto.
- Outro processo do mesmo usuário ainda pode alterar o worktree fora do lock;
  hashes antes/depois da revisão e `verify` detectam esse drift.
- Locks e hashes detectam corrupção e alterações acidentais, mas não autenticam
  o state root contra adulteração deliberada por outro processo com o mesmo UID.
- A unidade systemd atual escreve somente no state root e nunca executa Git para
  commit ou push.
- Saída de subprocessos e snapshots grandes não possuem cota de disco/memória;
  os arquivos brutos anteriores à sanitização podem sobreviver a uma morte
  abrupta do supervisor.
- Runs congelam o perfil serializado. Uma versão futura que altere defaults ou
  schema precisa de migração explícita para não tornar runs antigos incompatíveis.
- Autenticação e políticas server-side do remote ficam integralmente no fluxo
  Git manual do operador.
- `SIGKILL` aplicado ao próprio supervisor pode impedir sua gravação final; o
  próximo `resume` trata o artefato parcial como interrupção, nunca sucesso.
