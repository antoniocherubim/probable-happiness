# Migração para ferramenta reutilizável

## AG-01 implementada

- `TOOL_ROOT`, `TARGET_REPO` e `STATE_ROOT` são independentes;
- `./agent-loop run|review --repo ...` opera sem copiar arquivos ao alvo;
- estado padrão em `$XDG_STATE_HOME/codex-cursor-agent-loop` ou
  `~/.local/state/codex-cursor-agent-loop`;
- IDs de projeto combinam nome e hash do caminho Git canônico;
- schema, Python e template systemd permanecem na instalação da ferramenta;
- a ponte Telegram descobre múltiplos projetos no mesmo state root;
- a unidade systemd é gerada de template com caminhos reais;
- `review_current.sh` usa o mesmo hash canônico no-follow do loop principal;
- chamadas legadas dentro de um repositório continuam usando `.agents`.

## Trabalho futuro

- empacotar uma distribuição instalável com entrypoint no `PATH`;
- adicionar política opcional e estritamente declarativa em `.agent-loop.toml`;
- persistir/rotacionar offsets do Telegram para reduzir replays após restart;
- testar a matriz em CI para versões suportadas de Python e systemd;
- oferecer instalador e desinstalador opcionais para a unidade de usuário.

## Critério alcançado

Um repositório Git contendo apenas sua task versionada pode usar o runner por
um comando externo. Scripts, schema, testes e estado permanecem fora do
repositório-alvo.
