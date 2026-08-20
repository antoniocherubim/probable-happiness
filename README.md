# Probable Happiness

**Verification-driven agentic execution harness for software changes.**

`probable-happiness` é um harness local para executar mudanças de software com
agentes de IA sem conceder ao agente executor autoridade unilateral sobre o
estado canônico do repositório. A implementação atual usa **Cursor Agent** como
executor e **Codex** como reviewer, combinando execução em worktree isolado,
validação programática, revisão separada e aprovação vinculada ao conteúdo.
Depois do aceite técnico, o profile escolhe entre integração local explícita ou
publicação isolada em um pull request para revisão humana no GitHub.

O projeto nasceu da automação de um workflow pessoal de desenvolvimento. Ele não
foi originalmente desenhado como experimento acadêmico. A documentação de
pesquisa adicionada posteriormente separa cuidadosamente **mecanismos já
implementados**, **observações de uso**, **hipóteses** e **experimentos ainda não
realizados**.

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
       ┌─ verify / integrate explícito ─► branch local canônica
       └─ github_pr ─► branch dedicada + PR ─► review humano
```

A mudança candidata permanece fora da branch canônica durante execução e
review. Uma aprovação válida fica vinculada ao conteúdo revisado por manifesto
e hashes. O worktree em si **não é imutável**: drift posterior é detectado e
bloqueia `verify`/`integrate`.

`agent-loop integrate` continua sendo uma operação local e explícita: revalida o
snapshot, constrói um commit a partir dos bytes vinculados ao manifesto usando
um index temporário e avança a branch somente por fast-forward. Somente o modo
opt-in `github_pr` possui autoridade remota: ele publica exatamente o snapshot
revisado em uma branch dedicada e abre um PR; não faz merge nem deploy.

## Estado atual

A baseline orgânica anterior ao hardening de self-hosting está preservada na
tag `self-hosting-bootstrap` (`785485f`). A primeira mudança de hardening,
`SELF-00P`, foi integrada em `bb00503` e introduziu **controlled
project-adapter evolution**.

No estado pós-`SELF-00P`:

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

A implementação de `SELF-00P` foi aprovada na terceira iteração. A evidência
registrada na task inclui uma execução completa de `240 passed` em ambiente com
`systemd --user`. Esse caso é **ilustrativo**, não uma demonstração estatística
de superioridade da arquitetura.

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

O `artang-platform` é um consumidor real desse modelo: mantém uma camada local
específica do projeto para dependências de tasks, bootstrap, validações e regras
arquiteturais, enquanto aponta para a instalação externa do harness. Esse uso é
tratado como **evidência de reutilização/uso longitudinal**, não como prova de
melhor confiabilidade.

A arquitetura de adapter ainda está sendo formalizada no roadmap; hoje ela é
uma fronteira observável na implementação, não um framework genérico de plugins.
Da mesma forma, os papéis conceituais de executor/reviewer ainda estão ligados a
Cursor/Codex no runtime atual; a separação entre papel e vendor driver é trabalho
planejado.

## Pesquisa

O projeto pode servir como artefato para investigar a pergunta:

> **Como permitir que agentes probabilísticos proponham mudanças em software sem
> conceder-lhes autoridade unilateral sobre quais estados se tornam canônicos?**

A documentação voltada a pesquisa está em:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — arquitetura, trust boundaries,
  garantias e não-garantias;
- [`docs/RESEARCH_OVERVIEW.md`](docs/RESEARCH_OVERVIEW.md) — problema, hipóteses,
  perguntas de pesquisa e evidência já disponível;
- [`docs/EVALUATION_PLAN.md`](docs/EVALUATION_PLAN.md) — desenho experimental
  proposto, métricas e ameaças à validade;
- [`ROADMAP.md`](ROADMAP.md) — hardening e observabilidade planejados.

Não há, neste momento, alegação de novidade acadêmica, ganho causal de
confiabilidade ou superioridade frente a baselines. Uma revisão bibliográfica
sistemática e os experimentos comparativos ainda precisam ser realizados.

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
./agent-loop run --repo /caminho/do/projeto --allow-candidate-profile \
  docs/tasks/SELF-00A.md 3 main
./agent-loop review --repo /caminho/do/projeto docs/tasks/TASK-01.md
./agent-loop resume --run-dir /caminho/externo/para/o/run
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
  docs/tasks/SELF-00A.md \
  3 \
  HEAD
```

O profile candidato é conteúdo de uma possível próxima versão. O run corrente
continua governado pela visão de controle congelada do commit-base, dentro das
limitações documentadas em
[`docs/PROJECT_PROFILE.md`](docs/PROJECT_PROFILE.md).

## Aprovação e publicação

O profile escolhe:

```toml
[approval]
mode = "none"       # conclusão local
# mode = "telegram" # conclusão local + notificação terminal
```

O default de compatibilidade é `telegram`. A ponte é exclusivamente de saída e
não decide integração. Configuração e unidade `systemd --user` estão descritas
em [`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md).

Para publicar automaticamente uma branch separada e abrir um PR não-draft:

```toml
[approval]
mode = "github_pr"
remote = "origin"
base_branch = "main"
```

Esse modo exige `gh` autenticado, aceita somente remotes sem credenciais no
`github.com`, não cria outbox nem chama a ponte Telegram e nunca faz merge. O
perfil atualmente versionado continua em `telegram`; habilitar `github_pr` é
uma escolha explícita por projeto.

## Estrutura

- `agent-loop`: CLI externa (`run`, `review`, `resume`, `evidence`, `serve`,
  `verify`, `integrate`, `systemd-unit`);
- `scripts/agents/run_task.sh`: ciclo executor → validação → reviewer;
- `scripts/agents/dx/`: estado, locks, snapshot, adapter congelado, integração e
  demais mecanismos locais;
- `.agents/reviewer-output.schema.json`: contrato de saída do reviewer;
- `.agent-loop/project.toml`: profile deste próprio repositório;
- `tests/unit/`: suíte de regressão;
- `docs/tasks/`: backlog versionado usado pelo próprio loop;
- `docs/`: arquitetura, operação e proposta de avaliação.

## Documentação operacional

- [`docs/AGENT_ORCHESTRATION.md`](docs/AGENT_ORCHESTRATION.md)
- [`docs/PROJECT_PROFILE.md`](docs/PROJECT_PROFILE.md)
- [`docs/PERSONAL_CORE_V2.md`](docs/PERSONAL_CORE_V2.md) — registro histórico da
  linha de implementação anterior ao hardening atual.
