# PC-02a — Executar fases em scope

Status: concluída; matriz E2E obrigatória permanece em PC-02c.

Objetivo único: substituir supervisão por grupo de processos por um scope
`systemd --user` obrigatório e fail-closed.

Cada saída — sucesso, erro, timeout, sinal ou exceção interna — deve parar o
scope e confirmar que ele ficou inativo. Não adicionar cotas nesta etapa.

## Evidência

- ausência do manager recusa a fase antes de executar o comando;
- timeout e sucesso foram executados no `systemd --user` real;
- suíte completa: 199 testes aprovados sem skip;
- zero unidades `agent-loop-*.scope` permaneceram após a suíte.
