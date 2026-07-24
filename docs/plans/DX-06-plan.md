# Plano detalhado — DX-06 (pós DX-05)

Atualizado após conclusão da DX-05 (2026-07-24).

## Estado herdado da DX-05 (não reabrir)

- Bridge (`scripts/agents/dx/bridge.py`) importa só `delivery_job`; callback responde imediato.
- Job imutável: `scripts/agents/dx/delivery_job.py` + `docs/schemas/delivery-job.schema.json`.
- CLI/worker foreground: `agent-loop delivery-worker --project-state|--run-dir [--once]`.
- `run_task.sh` / resume usam `delivery-worker --once`, não `deliver-run` no caminho suportado.
- `deliver-run` interno permanece só como helper de transição.
- Suíte: **138 passed** (inclui `tests/unit/test_agent_dx05.py`).
- **M0 ainda aberto** — falta hardening Git + unidades + e2e systemd.
- **DX-08:** escolha JSON vs SQLite permanece **aberta até ADR** (não decidir agora).

## Objetivo DX-06

Fechar M0: o processo/unidade que executa Git **não** recebe `AGENT_TELEGRAM_*`;
hooks desligados; timeouts; `ReadWritePaths` mínimos; teste e2e systemd
**obrigatório e executado** no gate Linux (não apenas skipped).

```text
bridge unit (token Telegram, RW = state root global)
  → só enfileira delivery-job.json

worker unit (sem token, RW = project state + git common dir)
  → delivery-worker --project-state <dir>
  → git allowlisted + hooksPath=/dev/null + timeouts
```

## 1. Executor Git central

Substituir `_git()` em `scripts/agents/dx/delivery.py` (~L40–60) e qualquer
`subprocess.run(["git", ...])` restante em delivery por um único helper,
ex. `run_delivery_git(...)`:

| Requisito | Implementação |
|---|---|
| Env | Allowlist: `HOME`, `PATH`, `LANG`/`LC_*`, `SSH_AUTH_SOCK`, XDG aprovados; **strip** `AGENT_*`, `GIT_*` arbitrárias |
| Prompt | `GIT_TERMINAL_PROMPT=0`; credential helper não interativo |
| Hooks | sempre `git -c core.hooksPath=/dev/null ...` |
| Timeout | obrigatório por chamada; kill do process group no estouro |
| Erros | stderr sanitizado (sem URL/token); motivo estruturado → `DELIVERY_FAILED` |

Cobrir: `rev-parse`, `hash-object`, `update-index`, `commit-tree`, `update-ref`,
`ls-remote`, `push`, `remote get-url`.

## 2. Política de timeout

Constantes documentadas (ou freeze em metadata), com defaults/limites seguros:

- Git local (build tree / commit)
- `ls-remote` / descoberta
- `push`
- grace de kill

Timeout **nunca** promove `PUSHED`; preserva decisão/job/worktree;
`resume`/`delivery-worker` reprocessa.

## 3. Unidades systemd

Manter bridge: `scripts/agents/telegram-bridge.service.in`
(credential file + RW só `@STATE_ROOT@`).

Novo template `scripts/agents/delivery-worker.service.in` + render em
`scripts/agents/dx/paths.py`:

```bash
agent-loop systemd-worker-unit \
  --repo /projetos/alvo \
  --state-root ~/.local/state/codex-cursor-agent-loop \
  --output ~/.config/systemd/user/agent-delivery-<repo-id>.service
```

Unidade do worker:

- **sem** `EnvironmentFile` Telegram / sem `AGENT_TELEGRAM_*`
- `ExecStart=... delivery-worker --project-state <canônico>`
- `ReadWritePaths=` somente project state + Git common dir resolvidos
  (recusar symlink ambíguo / escape)
- `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`, `PrivateTmp`
- Restart só em falha operacional recuperável
- `systemd-analyze verify` nos testes

Documentar geração, enable/start manual, logs, remoção (state/jobs preservados).

## 4. Preflight / doctor mínimo

Diagnóstico sem valores: remote/push URL presente?, auth não interativa?,
common dir + permissões, hooks que serão ignorados, **ausência** de token
Telegram no env efetivo do worker.

## 5. Teste e2e systemd (gate M0)

Obrigatório em Linux com systemd; **não** fechar M0 se o teste crítico só
existir como skip.

Cenário:

1. repo + remote bare temporários
2. bridge + worker em unit/scope endurecidos
3. callback autorizado → resposta rápida + job pendente
4. worker publica branch; confirma OID; `main` intacta
5. hook de teste (`pre-push` / `reference-transaction`) **não** executa
6. worker **não** recebe token Telegram

Se o host de desenvolvimento não tiver systemd, o teste pode skip localmente,
mas o **CI Linux do gate M0** deve executá-lo de fato. Até o CI existir, rodar
o e2e neste host Linux com systemd e anexar evidência antes de marcar M0
concluído.

## Arquivos previstos

| Ação | Arquivo |
|---|---|
| Alterar | `delivery.py`, `cli.py`, `paths.py`, `agent-loop` |
| Novo | `scripts/agents/delivery-worker.service.in` |
| Novo | `tests/unit/test_agent_dx06.py` + integração systemd e2e |
| Docs | `docs/tasks/DX-06.md`, README, `AGENT_ORCHESTRATION.md`, ROADMAP M0 |

## Testes obrigatórios (nenhum skip como único gate)

- Env efetivo bridge vs worker (token ausente no worker)
- Hooks: `pre-push`, `reference-transaction`, `core.hooksPath` custom
- Prompt interativo / remote travado → timeout → `DELIVERY_FAILED`
- Redaction de stderr com URL/token fake
- Paths com espaços / caracteres systemd
- Bridge sem RW no repo; worker sem RW fora de project state + common dir
- `systemd-analyze verify`
- E2E systemd real (executado no gate)
- Suíte completa + `bash -n` + `compileall` + `git diff --check`

## Critério de saída M0

Todos os checkboxes M0 do `ROADMAP.md` com evidência; DX-06 marcada concluída;
**nenhum** critério de aceite coberto apenas por teste skipped.

## Explicitamente fora

- DX-07+ (state machine, persistência, cgroups, segredos)
- Claim/lease outbox (M3)
- **ADR de backend da DX-08** — permanece aberta
