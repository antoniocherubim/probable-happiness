# Perfil de projeto e retomada segura

Este documento é o contrato operacional detalhado do profile/adapter. Para a
visão arquitetural e de pesquisa, veja [`ARCHITECTURE.md`](ARCHITECTURE.md) e
[`RESEARCH_OVERVIEW.md`](RESEARCH_OVERVIEW.md).

Após `SELF-00P`, é importante distinguir **adapter de controle do run corrente**
de **adapter candidato proposto para runs futuros**. A autorização de mudança
não transforma o candidato em controller; ela apenas permite que o conteúdo
candidato atravesse review/integração se todos os bindings exigidos conferirem.

O motor continua externo. Um repositório consumidor pode rastrear somente sua
integração em `.agent-loop/project.toml`, instruções Markdown e scripts de
bootstrap/teste. Estado, worktrees, evidências e credenciais permanecem no XDG.
Um exemplo completo está em [`docs/examples/project.toml`](examples/project.toml).

## Schema `project.toml` (versão 1)

O parser é estrito: tabelas/chaves desconhecidas, tipos incorretos, comandos
vazios, variáveis inválidas e caminhos absolutos/com `..` são recusados. Cada
comando é um array `argv`; nenhum valor passa por `eval` ou shell implícito.
O run congela esse parser no commit-base; um profile candidato autorizado é
validado com as mesmas regras e nunca relaxa o schema.

| Campo | Tipo | Default/restrição |
|---|---|---|
| `schema_version` | inteiro | obrigatório, `1` |
| `approval.mode` | `none` ou `telegram` | `telegram` |
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
| `limits.output_bytes` | inteiro | 16 MiB combinados de stdout/stderr |
| `limits.file_bytes` | inteiro | 64 MiB por arquivo |
| `limits.memory_bytes` | inteiro | 4 GiB no cgroup |
| `limits.tasks` | inteiro | 512 processos/threads no cgroup |
| `limits.run_files` | inteiro | 512 arquivos no run |
| `policy.missing_profile` | `allow` ou `deny` | `allow` |
| `policy.terminate_grace_seconds` | inteiro | `5`, 1–300 |

Projetos sem perfil usam defaults seguros e o formato antigo continua válido.
Use `--require-profile` para bloquear a criação de um run sem o arquivo. O valor
`policy.missing_profile = "deny"` documenta a política quando o perfil existe; a
flag é a proteção aplicável quando ele está ausente.

Templates são analisados sem `eval`. Documentação aceita somente `{task_id}` e
`{task_slug}`. Placeholder desconhecido e caminho absoluto/com `..` bloqueiam o
preflight.

## Adapter congelado e profile candidato

O default permanece fail-closed: sem autorização explícita, qualquer mudança em
`.agent-loop/project.toml` continua recusada com a mensagem estável de profile
imutável. A autorização é somente a flag de `agent-loop run`; ela é persistida
no metadata do run e não pode ser inferida pelo nome da task nem pelo conteúdo
candidato.

```bash
./agent-loop run --repo /repo --allow-candidate-profile \
  docs/tasks/SELF-00A.md 3 main
```

Antes do executor, o run captura do commit-base o profile, as instruções
rastreadas (incluindo `.agent-loop/{executor,reviewer}.md` só se já existirem
nesse commit) e o script de entrypoint de bootstrap/gates. Um entrypoint
configurado e ausente nesse commit falha na criação do run. Essa visão de
controle fica em `control-adapter/` no diretório do run e permanece imutável em
iterações e `resume`. Gates estáveis ainda recebem o worktree candidato como
objeto de teste, mas o argv do gate é reescrito para a cópia congelada (ou para
o blob Git do commit-base se o adapter ainda não estiver materializado). Flags
de interpretador como `bash -e scripts/gate.sh` continuam a identificar o
script; um arquivo criado ou substituído pelo candidato nesse caminho relativo
não é executado. `python3 -m compileall` (e qualquer `python -m`) recebe `-P`
para o módulo nomeado não ser carregado de um arquivo plantado no cwd do
candidato; caminhos passados ao módulo continuam relativos ao worktree.

Um profile candidato autorizado é validado como conteúdo de Engine/Adapter N+1
e pode entrar no snapshot revisado. Ele nunca fornece bootstrap, validações,
documentação obrigatória ou instruções ao run que o produz. `verify` e
`integrate` transportam essa mudança somente quando a autorização registrada e
o snapshot revisado coincidem; autorização ausente, adulterada ou divergente
bloqueia antes da transição canônica.

## Documentação obrigatória

Quando `documentation.required = true`, cada caminho renderizado deve ter sido
criado ou alterado no snapshot final. O executor recebe instrução para registrar
comportamento, testes e riscos; o reviewer valida a precisão. Ausência bloqueia
o aceite técnico. O loop não edita documentação por heurística e não exige SHA ou
URL de uma branch que ainda não existe.

## Conclusão local ou notificação Telegram

Com `approval.mode = "none"`, um review técnico válido termina em `APPROVED`,
sem outbox Telegram. Com `telegram`, também termina em `APPROVED` e enfileira
uma mensagem terminal sem botões ou ações. Ambos preservam o worktree. A
integração é uma ação local explícita e separada:

```bash
agent-loop integrate --run-dir /state/projects/<repo-id>/runs/<run-id>
```

O comando exige aceite técnico válido, snapshot intacto, manifesto correspondente,
checkout limpo e `HEAD` no commit-base; então cria um commit pelo index
temporário e faz fast-forward com hooks desativados. Não há fetch, pull, push,
branch remota ou conexão Git. `agent-loop verify --run-dir ...` permanece como
checagem somente-leitura.

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
./agent-loop run --repo /repo --allow-candidate-profile \
  docs/tasks/SELF-00A.md 3 main
```

Se a flag for omitida e esse arquivo XDG existir, ele é descoberto
automaticamente. Deve ser regular, não symlink, do usuário atual e `0600` (ou
mais restritivo). Chaves extras são ignoradas; somente nomes em
`environment.required` chegam ao bootstrap, Cursor e validações. Logs mostram
apenas `NOME=set|unset`, substituem valores por `[REDACTED]` e URLs por
`[REDACTED_URL]` nos artefatos finais. Artefatos JSON válidos são desserializados,
sanitizados recursivamente apenas em seus valores textuais e serializados de novo,
preservando escapes e a validade estrutural. Durante a execução, stdout e stderr passam
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

O ambiente de cada fase também fixa `GIT_ALLOW_PROTOCOL=file` e
`GIT_PROTOCOL_FROM_USER=0`. Operações locais continuam disponíveis, mas
transportes Git remotos são recusados antes da rede. Isso não bloqueia outros
clientes de rede e não amplia o modelo para repositórios hostis.

`MemoryMax` e `TasksMax` são aplicados diretamente ao scope. `prlimit` fixa um
limite hard de tamanho por arquivo para a árvore de processos. Stdout e stderr
compartilham o orçamento de `limits.output_bytes`; ao excedê-lo, o scope é
encerrado e o resultado registra `*_output_limit`. O número e tamanho dos
artefatos do run são verificados antes de qualquer leitura/sanitização.

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
AWAITING_HUMAN_APPROVAL -> migra run legado para APPROVED
HUMAN_APPROVED          -> valida decisão/hash legado
APPROVED                -> valida manifesto/hash; não repete review
```

```bash
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id>
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id> --review-only
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id> \
  --additional-iterations 3
```

O wrapper mantém `.resume.lock` durante toda a retomada. Antes de iniciar,
valida metadados, task no base commit, `HEAD`, repositório comum do worktree,
adapter de controle congelado e hash pré-revisão. Drift durante/depois da revisão
é recusado. Nos dois modos, `APPROVED` só é terminal com manifesto técnico
válido. `resume` não aceita `--allow-candidate-profile`; a autorização é a
gravada na criação do run.

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
- ausência de artefatos terminais e locks concorrentes.

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
artefatos e recalculá-los. Extensão por Telegram não existe: a autorização
permanece exclusivamente na CLI para evitar uma segunda superfície.

## Evidência complementar

```bash
./agent-loop evidence --run-dir /state/.../runs/<run-id> --file /tmp/report.txt
./agent-loop resume --run-dir /state/.../runs/<run-id> --review-only
```

A origem é aberta com `O_NOFOLLOW`, deve ser regular e ter no máximo 1 MiB.
FIFO, socket, device, symlink, troca de inode e destino adulterado são recusados.
A cópia recebe nome pelo SHA-256, modo `0600`, timestamp e `trust = "untrusted"`.
Anexar não altera status. Somente uma nova revisão pode produzir um novo aceite
técnico.

## Riscos residuais

- O motor não provisiona bancos/containers; o bootstrap somente prepara ou
  verifica recursos autorizados pelo projeto.
- Outro processo do mesmo usuário ainda pode alterar o worktree fora do lock;
  hashes antes/depois da revisão e `verify` detectam esse drift.
- Locks e hashes detectam corrupção e alterações acidentais, mas não autenticam
  o state root contra adulteração deliberada por outro processo com o mesmo UID.
- A unidade systemd atual escreve somente no state root e nunca executa Git para
  commit ou push.
- Há cotas por saída, arquivo e quantidade de artefatos, mas não uma cota total
  de disco acumulada entre runs/worktrees; arquivos brutos anteriores à
  sanitização podem sobreviver a uma morte abrupta do supervisor.
- Runs congelam o perfil serializado e, nos runs novos, também o adapter de
  controle capturado do commit-base. Uma versão futura que altere defaults ou
  schema precisa de migração explícita para não tornar runs antigos incompatíveis.
- A captura de entrypoints cobre o script nomeado no argv do gate após flags do
  interpretador (`bash -e`, …), não helpers sourced internamente nem arquivos
  puxados por `--rcfile`/`--init-file`. `python3 -m` não carrega o módulo
  nomeado do cwd do candidato (`-P` no rewrite). `PYTHONPATH` autorizado que
  aponte para o worktree, bem como `python3 -c` e `bash -c`, ainda podem
  resolver código a partir do candidato.
- Adulteração deliberada do manifesto congelado e do metadata pelo mesmo UID
  continua fora do modelo de ameaça autenticado; hashes detectam drift e
  corrupção acidental.
- Autenticação e políticas server-side do remote ficam integralmente no fluxo
  Git manual do operador.
- `SIGKILL` aplicado ao próprio supervisor pode impedir sua gravação final; o
  próximo `resume` trata o artefato parcial como interrupção, nunca sucesso.
