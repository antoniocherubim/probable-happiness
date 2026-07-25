# Operação pessoal por SSH

## Pré-requisitos

- computador ligado, conectado à rede e sem suspensão;
- servidor SSH ativo;
- autenticação por chave;
- acesso pela mesma LAN ou por VPN privada.

Não exponha a porta 22 diretamente à internet sem antes definir firewall,
autenticação e política de atualização. O servidor OpenSSH não faz parte do
`agent-loop`.

## Sessão persistente

O `tmux` mantém o processo vivo quando o terminal SSH desconecta.

```bash
tmux new-session -s agent-loop
cd "/home/cherubim/Área de trabalho/Projects/codex-cursor-agent-loop"
source venv/bin/activate
```

Execute o comando do loop dentro dessa sessão. Para desconectar sem encerrar:

```text
Ctrl-b d
```

Depois de reconectar por SSH:

```bash
tmux list-sessions
tmux attach-session -t agent-loop
```

Use apenas um executor por worktree/run. Uma segunda sessão pode observar
arquivos, mas não deve executar `resume` concorrentemente.

## Impedir suspensão enquanto houver trabalho remoto

Crie uma sessão separada e reversível:

```bash
tmux new-session -d -s keep-awake \
  'systemd-inhibit --what=sleep:idle --why="agent-loop remoto" --mode=block sleep infinity'
```

Ao terminar:

```bash
tmux kill-session -t keep-awake
```

## Observação

```bash
tmux list-sessions
tmux capture-pane -pt agent-loop -S -120
pgrep -af 'agent-loop|run_task|cursor-agent|codex'
```

O run informa seu diretório no início. Nesse diretório:

```bash
python3 -m json.tool heartbeat.json
python3 -m json.tool failure.json
```

Arquivos ausentes podem ser estados normais; não os crie manualmente.

## Interrupção e retomada

Peça interrupção graciosa:

```bash
tmux send-keys -t agent-loop C-c
```

Confirme que não restaram processos antes de retomar:

```bash
pgrep -af 'agent-loop|run_task|cursor-agent|codex'
```

Wrappers `cursorsandbox` com `PPID 1` são órfãos. Não mate o grupo de processos
deles, pois um grupo antigo pode coincidir com a sessão gráfica. Liste apenas os
órfãos:

```bash
ps -C cursorsandbox -o pid=,ppid=,etime= | awk '$2 == 1 {print}'
```

A limpeza exige privilégio do host e deve sinalizar somente os PIDs listados,
nunca `kill -- -PGID`.

Retome usando o caminho real exibido pelo run:

```bash
./agent-loop resume --run-dir /caminho/completo/do/run
```

Para autorizar somente uma correção adicional:

```bash
./agent-loop resume \
  --run-dir /caminho/completo/do/run \
  --additional-iterations 1
```

Não use placeholders com `<` e `>` no shell.

## Limites operacionais

- queda de energia ou rede interrompe o acesso;
- fechar a tampa pode suspender o host conforme firmware/configuração;
- `tmux` não substitui backup;
- não faça merge/push remotamente sem revisar `git status`, `git diff` e o
  relatório final;
- preserve o worktree antes de abandonar um run com changes requested.
