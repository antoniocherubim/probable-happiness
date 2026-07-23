# Codex Cursor Agent Loop

Snapshot externo da ferramenta de orquestração extraída do projeto
`new_chatbot`.

## Estado

- implementação DX-01 aprovada;
- 48 testes focados aprovados;
- gate humano via Telegram, sem shell remoto;
- nenhuma automação de commit, merge, push, deploy ou próxima task.

Este diretório preserva a implementação e seus testes antes da generalização.
Ele ainda contém acoplamentos ao layout original e não deve ser considerado
instalador universal até a conclusão da etapa AG-01 descrita em
`MIGRATION_NOTES.md`.

## Conteúdo

- `scripts/agents/`: loop, revisão local e ponte Telegram;
- `.agents/reviewer-output.schema.json`: contrato do revisor;
- `tests/unit/test_agent_*.py`: suíte focada;
- `docs/AGENT_ORCHESTRATION.md`: operação atual;
- `docs/tasks/DX-01.md`: especificação aprovada;
- `archive/`: runs antigos preservados fora do chatbot.

## Próximo objetivo

Transformar a ferramenta em um runner instalado externamente que recebe
`--repo /caminho/do/projeto`, mantém estado fora do repositório-alvo e usa
configuração por projeto.
