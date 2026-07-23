# Migração para ferramenta reutilizável

## Acoplamentos ainda existentes

1. `run_task.sh` procura o helper Python em
   `$REPO_ROOT/scripts/agents`.
2. Runs, locks e worktrees são criados em `$REPO_ROOT/.agents`.
3. O schema do revisor é procurado dentro do repositório-alvo.
4. A unidade systemd e a documentação usam caminhos e namespace do
   `chatbot-artang`.
5. `review_current.sh` ainda possui a implementação histórica de snapshot
   de untracked e deve ser alinhado ao hash canônico antes do uso universal.
6. Os testes assumem que a ferramenta está copiada para dentro do
   repositório executado.

## AG-01 proposta

- separar `TOOL_ROOT`, `TARGET_REPO` e `STATE_ROOT`;
- oferecer comandos `agent-loop run --repo ...` e
  `agent-loop review --repo ...`;
- usar por padrão
  `$XDG_STATE_HOME/codex-cursor-agent-loop/<repo-id>`;
- manter schema, Python e templates dentro da instalação da ferramenta;
- aceitar um arquivo opcional `.agent-loop.toml` no projeto somente para
  política, sem código executável ou segredos;
- gerar unidade systemd a partir de template, sem caminhos fixos;
- fazer a ponte Telegram descobrir múltiplos projetos no state root;
- testar dois repositórios simultâneos, nomes iguais, caminhos com espaços,
  symlinks e isolamento de runs;
- fornecer instalador reversível ou execução direta sem copiar arquivos para
  o repositório-alvo.

## Critério de pronto

Um repositório Git limpo, contendo apenas sua task versionada, deve conseguir
usar o loop por um comando externo sem receber `scripts/agents`, `.agents`,
serviço systemd ou testes da ferramenta em seu próprio Git.
