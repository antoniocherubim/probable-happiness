# Probable Happiness

**Verification-driven agentic execution harness for software changes.**

`probable-happiness` é um harness local para executar mudanças de software com
agentes de IA sem conceder ao agente executor autoridade unilateral sobre o
estado canônico do repositório. A implementação atual usa **Cursor Agent** como
executor e **Codex** como reviewer, combinando execução em worktree isolado,
validação programática, revisão separada e aprovação vinculada ao conteúdo.
Depois do aceite técnico, o profile escolhe entre integração local explícita,
encadeamento local YOLO ou publicação isolada em uma branch dedicada no GitHub.

O projeto nasceu da automação de um workflow pessoal de desenvolvimento e é
mantido como ferramenta operacional: o objetivo é executar, revisar, retomar e
integrar mudanças com limites claros de autoridade.

## Ideia central

```text
task versionada
      │
      ▼
candidate worktree isolado
      │
      ▼
executor (Cursor, hoje)
      │
      ▼
validações programáticas
      │
      ▼
reviewer separado (Codex, hoje)
      │
      ├── CHANGES_REQUESTED ──► nova iteração
      │
      └── APPROVED
              │
              ▼
     manifesto + hash revisado
              │
              ▼
       ├─ verify / integrate explícito ─► branch local canônica
       ├─ yolo ─► commit local ─► próxima task ─► review final .md
       └─ github_branch ─► branch dedicada ─► fluxo remoto manual
```

A mudança candidata permanece fora da branch canônica durante execução e
review. Uma aprovação válida fica vinculada ao conteúdo revisado por manifesto
e hashes. O worktree em si **não é imutável**: drift posterior é detectado e
bloqueia `verify`/`integrate`.

`agent-loop integrate` continua sendo uma operação local e explícita nos modos
`none` e `telegram`: revalida o
snapshot, constrói um commit a partir dos bytes vinculados ao manifesto usando
um index temporário e avança a branch somente por fast-forward. O modo opt-in
`yolo` reutiliza esse mesmo integrador automaticamente para uma lista explícita
de tasks, sem limite numérico de iterações e sem acesso remoto. Somente o modo
opt-in `github_branch` possui autoridade remota: ele publica exatamente o
snapshot revisado em uma branch dedicada; não abre PR, não faz merge nem deploy.

## Adapter de controle congelado

Para permitir que o harness evolua a própria configuração sem deixar o
candidato controlar o run corrente:

- profile, instruções e entrypoints relevantes do adapter que controlam um run
  são capturados do commit-base;
- alterar `.agent-loop/project.toml` continua fail-closed por padrão;
- `--allow-candidate-profile` autoriza explicitamente que um profile candidato
  seja revisado como conteúdo para runs futuros, sem fazê-lo controlar o run
  que o produz;
- gates de script relativos são resolvidos a partir da visão congelada do
  adapter quando aplicável;
- `python -m` reescrito usa `-P` quando necessário para impedir que um módulo
  plantado no cwd do candidate worktree substitua o módulo do gate;
- `resume`, `verify` e `integrate` reusam/verificam os bindings de controle
  aplicáveis ao run.

## O que o sistema garante — dentro do modelo de confiança declarado

O runtime implementa mecanismos para:

- isolar o estado candidato da branch local canônica durante execução/review;
- separar a fase de execução da fase de review;
- executar validações não-LLM configuradas pelo projeto;
- detectar mutação do candidate worktree durante a revisão;
- vincular aprovação técnica ao conteúdo revisado;
- detectar drift entre aprovação e integração;
- serializar transições relevantes por máquina de estados e locks locais;
- manter integração local separada e explicitamente acionada;
- preservar runs/worktrees interrompidos para retomada controlada.

Esses mecanismos **não** significam que:

- o reviewer esteja semanticamente correto;
- testes passando provem correção do software;
- o projeto forneça verificação formal;
- processos deliberadamente hostis sob o mesmo UID sejam isolados
  criptograficamente;
- todo comando de validação configurável seja intrinsecamente determinístico;
- o sistema implemente *Speculative Reasoning* no sentido de raciocínio
  especulativo durante inferência de LLM.

Uma descrição mais precisa é **verification-driven agentic execution**, com
execução candidata/especulativa no nível de ação/estado.

## Uso em outros projetos

O harness continua externo ao repositório-alvo. Um projeto consumidor mantém sua
policy local — por exemplo profile, instruções, bootstrap e gates — sem copiar o
núcleo do loop.

Projetos consumidores podem manter dependências de tasks, bootstrap,
validações e regras arquiteturais no próprio adapter. O núcleo continua ligado a
Cursor/Codex como implementações concretas dos papéis de executor/reviewer.

## Preparação

O runtime requer Python 3.11 ou posterior e ferramentas do sistema. `pytest` é
necessário para desenvolvimento:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Também são necessários `git`, `flock`, `systemd-run`, `systemctl`, `prlimit`,
Cursor Agent e Codex CLI autenticados.

## Uso externo

```bash
./agent-loop run --repo /caminho/do/projeto docs/tasks/TASK-01.md 3 main
./agent-loop yolo --repo /caminho/do/projeto \
  docs/tasks/TASK-01.md docs/tasks/TASK-02.md
./agent-loop run --repo /caminho/do/projeto --allow-candidate-profile \
  docs/tasks/EVOLVE-ADAPTER.md 3 main
./agent-loop review --repo /caminho/do/projeto docs/tasks/TASK-01.md
./agent-loop resume --run-dir /caminho/externo/para/o/run
./agent-loop resume --run-dir /caminho/externo/para/o/run --review-only
./agent-loop resume --run-dir /caminho/externo/para/o/run --additional-iterations 3
./agent-loop evidence --run-dir /caminho/externo/para/o/run --file /tmp/relatorio.txt
./agent-loop verify --run-dir /caminho/externo/para/o/run
./agent-loop integrate --run-dir /caminho/externo/para/o/run
```

Por padrão, runs e worktrees ficam em:

```text
$XDG_STATE_HOME/codex-cursor-agent-loop/projects/<nome-hash>/
```

Sem `XDG_STATE_HOME`, usa `~/.local/state`. `--state-root` permite outro local.
O identificador inclui o caminho canônico do Git, isolando repositórios com o
mesmo nome e aliases por symlink.

## Profile e self-hosting

Projetos podem declarar bootstrap, ambiente allowlisted, timeouts, heartbeat,
validações e documentação obrigatória em `.agent-loop/project.toml`.

Por padrão, um candidato não pode trocar o profile que controla seu próprio
run. Para uma task que **deliberadamente** evolui o adapter, a autorização deve
ser explícita na criação do run:

```bash
./agent-loop run \
  --repo "$PWD" \
  --require-profile \
  --allow-candidate-profile \
  docs/tasks/EVOLVE-ADAPTER.md \
  3 \
  HEAD
```

O profile candidato é conteúdo de uma possível próxima versão. O run corrente
continua governado pela visão de controle congelada do commit-base, dentro das
limitações documentadas em
[`docs/PROJECT_PROFILE.md`](docs/PROJECT_PROFILE.md).

Quando o operador corrige manualmente o worktree depois de uma revisão
interrompida ou `CHANGES_REQUESTED`, `resume --review-only` inicia uma nova
revisão do snapshot atual. A retomada comum continua recusando drift inesperado.

## Aprovação e publicação

O profile escolhe:

```toml
[approval]
mode = "none"       # conclusão local
# mode = "telegram" # conclusão local + notificação terminal
# mode = "yolo"     # commits locais encadeados + review final
```

O default de compatibilidade é `telegram`. A ponte é exclusivamente de saída e
não decide integração. Configuração e unidade `systemd --user` estão descritas
em [`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md).

Com `mode = "yolo"`, use `agent-loop yolo` e passe as tasks na ordem desejada.
Todas precisam estar rastreadas no commit inicial. `CHANGES_REQUESTED` continua
voltando ao executor até `APPROVED`; esgotamento de tokens/contexto ou qualquer
outra falha encerra a sequência. Cada aceite gera um commit local antes da
próxima task. Ao final, somente o reviewer grava `final-review.md` no state root.
Não há push, branch auxiliar nem nova chamada ao executor depois desse review.

Para publicar automaticamente uma branch separada:

```toml
[approval]
mode = "github_branch"
remote = "origin"
base_branch = "main"
```

Esse modo usa apenas `git`, aceita somente remotes sem credenciais no
`github.com`, não cria outbox nem chama a ponte Telegram e não abre PR nem faz
merge. O perfil atualmente versionado continua em `telegram`; habilitar
`github_branch` é uma escolha explícita por projeto. O nome `github_pr` continua
aceito somente para compatibilidade com profiles e runs anteriores.

## Estrutura

- `agent-loop`: CLI externa (`run`, `yolo`, `review`, `resume`, `evidence`, `serve`,
  `verify`, `integrate`, `systemd-unit`);
- `scripts/agents/run_task.sh`: ciclo executor → validação → reviewer;
- `scripts/agents/dx/`: estado, locks, snapshot, adapter congelado, integração e
  demais mecanismos locais;
- `.agents/reviewer-output.schema.json`: contrato de saída do reviewer;
- `.agent-loop/project.toml`: profile deste próprio repositório;
- `tests/unit/`: suíte de regressão;
- `docs/tasks/`: especificações operacionais versionadas usadas pelo loop;
- `docs/`: arquitetura e operação.

## Documentação operacional

- [`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md)
- [`docs/PROJECT_PROFILE.md`](docs/PROJECT_PROFILE.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
