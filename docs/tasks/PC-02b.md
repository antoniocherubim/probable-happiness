# PC-02b — Aplicar cotas essenciais

Status: concluída.

Objetivo único: limitar bytes de stdout/stderr, quantidade/tamanho de artefatos
operacionais e memória/processos do scope.

Não alterar o protocolo de estado ou Telegram nesta etapa.

## Evidência

- `MemoryMax` e `TasksMax` são propriedades obrigatórias do scope;
- `prlimit` aplica limite hard de tamanho por arquivo;
- stdout/stderr compartilham um orçamento e excesso encerra o scope;
- quantidade e tamanho de artefatos são verificados antes da leitura;
- suíte completa: 202 testes aprovados sem skip;
- zero unidades `agent-loop-*.scope` permaneceram após a suíte.
