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

O plano canônico, com dependências e critérios de saída, está em
[`ROADMAP.md`](ROADMAP.md). Em resumo:

- empacotar uma distribuição instalável com entrypoint no `PATH`;
- versionar migrações do schema de `.agent-loop/project.toml` e de runs antigos;
- manter integração Git manual no M0 e reavaliar delivery automático somente
  com isolamento real de credenciais em marco posterior;
- persistir/rotacionar offsets do Telegram para reduzir replays após restart;
- impor singleton/claim durável para o outbox da ponte;
- adicionar cotas de processo, memória, disco e saída por fase;
- testar a matriz em CI para versões suportadas de Python e systemd;
- oferecer instalador e desinstalador opcionais para a unidade de usuário.

## Critério alcançado

Um repositório Git contendo apenas sua task versionada pode usar o runner por
um comando externo. Scripts, schema, testes e estado permanecem fora do
repositório-alvo.

## Remoção do push automático (DX-06C)

Profiles novos aceitam somente ausência de `[delivery]` ou:

```toml
[delivery]
mode = "none"
```

Remova `push_branch`, remote, templates e `push_after_human_approval`. A
aprovação termina em `HUMAN_APPROVED`; execute `agent-loop verify` e faça
commit/push manualmente. Runs antigos de delivery permanecem legíveis, mas esta
versão não cria nem retoma `delivery-job.json`.

## Máquina de estados central (DX-07)

O arquivo `status` não deve mais ser editado por scripts ou integrações. O
runner usa eventos tipados, compare-and-set e `.state.lock`; o comando interno
arbitrário `set-status` foi removido. Runs legados em `DELIVERING`,
`DELIVERY_FAILED` ou `PUSHED` continuam inspecionáveis, mas são terminais.
