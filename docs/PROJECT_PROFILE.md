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
| `delivery.mode` | `none` ou `push_branch` | `none` |
| `delivery.remote` | nome de remote | `origin` |
| `delivery.base_branch` | ref de branch | `main` |
| `delivery.branch_template` | template de ref | `{task_slug}` |
| `delivery.commit_message_template` | template de texto | `{task_id}: {task_title}` |
| `delivery.push_after_human_approval` | booleano | `false` |
| `policy.missing_profile` | `allow` ou `deny` | `allow` |
| `policy.terminate_grace_seconds` | inteiro | `5`, 1–300 |

Projetos sem perfil usam defaults seguros e o formato antigo continua válido.
Use `--require-profile` para bloquear a criação de um run sem o arquivo. O valor
`policy.missing_profile = "deny"` documenta a política quando o perfil existe; a
flag é a proteção aplicável quando ele está ausente.

Templates são analisados sem `eval`. Documentação aceita somente `{task_id}` e
`{task_slug}`; branch aceita os mesmos campos; mensagem de commit também aceita
`{task_title}`. Placeholder desconhecido, caminho absoluto/com `..`, remote
inválido ou ref rejeitada por `git check-ref-format` bloqueia o preflight.

## Documentação obrigatória

Quando `documentation.required = true`, cada caminho renderizado deve ter sido
criado ou alterado no snapshot final. O executor recebe instrução para registrar
comportamento, testes e riscos; o reviewer valida a precisão. Ausência bloqueia
o gate humano. O loop não edita documentação por heurística e não exige SHA ou
URL de uma branch que ainda não existe.

## Entrega opt-in

Remote, base, branch, mensagem e hash da URL de push são congelados em
`run.json`. Após a decisão humana, o loop revalida decisão, `HEAD`, hash e
manifesto, cria uma index temporária com somente as entradas revisadas, grava
`tree_oid`/`commit_oid`, cria a ref local e usa refspec explícito sem force:

```text
<commit_oid>:refs/heads/<branch>
```

`main`, `master` e a base configurada nunca são alvos. Branch remota diferente
gera `remote_branch_exists`; a mesma branch no mesmo commit é idempotente.
Credenciais Git não são copiadas para o run: usa-se apenas a autenticação já
configurada pelo usuário.

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
`[REDACTED_URL]`.

## Timeout, grupo de processos e heartbeat

Cada fase inicia uma nova sessão/grupo. No timeout, o supervisor envia `SIGTERM`
ao grupo, aguarda `policy.terminate_grace_seconds` e usa `SIGKILL` se necessário.
O worktree permanece; `failure.json` registra `executor_timeout`,
`reviewer_timeout`, `*_empty_report` etc., e o status fica `BLOCKED`. Saída vazia
nunca é sucesso.

Durante a fase, `heartbeat.json` é substituído atomicamente e uma linha segura
mostra fase, iteração, elapsed, PID/PGID, última atividade, arquivos modificados
e estado. Nenhum conteúdo ou ambiente entra no heartbeat.

## Máquina de estados de retomada

```text
EXECUTING/interrompido  -> executor da mesma iteração
REVIEWING/interrompido  -> nova revisão do snapshot pré-revisão
CHANGES_REQUESTED       -> executor da próxima iteração
BLOCKED + --review-only -> nova revisão do snapshot atual
AWAITING_HUMAN_APPROVAL -> apenas retoma wait-decision
HUMAN_APPROVED          -> valida decisão/hash; não repete gate
HUMAN_APPROVED + delivery -> DELIVERING -> PUSHED
DELIVERING/DELIVERY_FAILED -> retoma somente delivery
PUSHED                  -> terminal; não repete push
```

```bash
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id>
./agent-loop resume --run-dir /state/projects/<repo-id>/runs/<run-id> --review-only
```

O wrapper mantém `.resume.lock` durante toda a retomada. Antes de iniciar,
valida metadados, task no base commit, `HEAD`, repositório comum do worktree,
perfil congelado e hash pré-revisão. Drift durante/depois da revisão ou no gate
humano é recusado. Um `APPROVED` isolado sempre volta a uma nova revisão.

Exemplo abreviado de `delivery.json`:

```json
{
  "schema_version": 1,
  "task_id": "CP-00",
  "status": "PUSHED",
  "branch": "cp-00",
  "remote": "origin",
  "base_commit": "0123456789abcdef",
  "reviewed_diff_hash": "752aef57...",
  "commit_oid": "abc123...",
  "tree_oid": "def456...",
  "remote_oid": "abc123...",
  "push_result": "pushed",
  "branch_url": "https://github.com/org/repo/tree/cp-00",
  "compare_url": "https://github.com/org/repo/compare/main...cp-00"
}
```

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
  hashes antes/depois da revisão, antes do push e `verify` detectam esse drift.
- Autenticação e políticas server-side do remote continuam externas; falhas
  ficam em `DELIVERY_FAILED` e exigem correção operacional antes do `resume`.
- `SIGKILL` aplicado ao próprio supervisor pode impedir sua gravação final; o
  próximo `resume` trata o artefato parcial como interrupção, nunca sucesso.
