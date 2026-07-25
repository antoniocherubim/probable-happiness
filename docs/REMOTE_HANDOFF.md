# Handoff remoto do Personal Stable

Este runbook permite continuar sem acesso à conversa que preparou as tasks.

## Regra de decisão

- `APPROVED`: aprove pelo Telegram, verifique o snapshot, faça commit local e
  inicie a próxima task.
- `CHANGES_REQUESTED` na primeira revisão: permita que a segunda iteração já
  orçada corrija o finding.
- `CHANGES_REQUESTED` depois da segunda revisão: preserve o worktree e pare.
- timeout, erro de infraestrutura, processo órfão ou dúvida: pare; nunca aumente
  novamente o orçamento.

Findings fora do objetivo único e do modelo pessoal não bloqueiam a task. Eles
devem aparecer apenas como risco residual/backlog no resumo do reviewer.

## Estado inicial

```bash
ROOT="/home/cherubim/Área de trabalho/Projects/codex-cursor-agent-loop"
STATE="/home/cherubim/.local/state/codex-cursor-agent-loop/projects/codex-cursor-agent-loop-a54e295c140c"
```

Antes da PS-01, limpe os wrappers antigos com o procedimento sudo fornecido ao
operador. Depois inicie:

```bash
tmux new-session -d -s ps-01 -c "$ROOT" \
  'source venv/bin/activate && ./agent-loop run --repo "$PWD" docs/tasks/PS-01.md 2 wip/dx-08a1-review-2; exec bash'
```

## Acompanhar

```bash
tmux capture-pane -pt ps-01 -S -160
tmux attach-session -t ps-01
```

O run directory aparece no terminal. Também pode ser localizado por:

```bash
ls -td "$STATE"/runs/ps-01-* | head -1
```

Leia o status e o último review sem editar os artefatos:

```bash
RUN_DIR="$(ls -td "$STATE"/runs/ps-01-* | head -1)"
sed -n '1p' "$RUN_DIR/status"
python3 -m json.tool "$(ls -t "$RUN_DIR"/review-*.json | head -1)"
```

## Resultado aprovado

Depois do botão Aprovar no Telegram e do estado `HUMAN_APPROVED`:

```bash
"$ROOT/agent-loop" verify --run-dir "$RUN_DIR"
WORKTREE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worktree"])' "$RUN_DIR/run.json")"
git -C "$WORKTREE" status --short
git -C "$WORKTREE" switch -c personal/ps-01
git -C "$WORKTREE" add .
git -C "$WORKTREE" commit -m "feat: complete PS-01"
```

Não faça push ou merge.

## Iniciar a task seguinte

Use o `agent-loop` do worktree aprovado anterior. Assim o próprio supervisor já
inclui as correções recém-aprovadas.

| Task | Executável | Base |
|---|---|---|
| PS-02 | worktree da PS-01 | `personal/ps-01` |
| PS-03 | worktree da PS-02 | `personal/ps-02` |
| PS-04 | worktree da PS-03 | `personal/ps-03` |
| PS-05 | worktree da PS-04 | `personal/ps-04` |

Exemplo para PS-02:

```bash
TOOL="$STATE/worktrees/ps-01/agent-loop"
tmux new-session -d -s ps-02 -c "$ROOT" \
  "\"$TOOL\" run --repo \"$ROOT\" \"$ROOT/docs/tasks/PS-02.md\" 2 personal/ps-01; exec bash"
```

Após aprovação, repita o bloco anterior mudando:

- `ps-01` para o ID atual;
- branch para `personal/ps-02`, depois `personal/ps-03` e assim por diante;
- mensagem do commit;
- padrão do run directory.

## Resultado não aprovado

Depois da segunda revisão, preserve sem promover:

```bash
WORKTREE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worktree"])' "$RUN_DIR/run.json")"
git -C "$WORKTREE" switch -c wip/ps-01-review-2
git -C "$WORKTREE" add .
git -C "$WORKTREE" commit -m "WIP: preserve PS-01 review 2 candidate"
```

Pare nesse ponto. Não inicie a task seguinte sobre uma candidata não aprovada.

## Controle de processos

Depois de cada task:

```bash
ps -C cursorsandbox -o pid=,ppid=,etime= | awk '$2 == 1 {print}'
```

Após a PS-01, a lista deve ficar vazia. Se não ficar, pare a sequência. Nunca
mate um PGID da sessão gráfica.

