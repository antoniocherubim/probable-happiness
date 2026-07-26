# PC-02c — Gate systemd real

Status: concluída.

Objetivo único: executar um E2E obrigatório no manager `systemd --user` real.

O gate deve provar scope vazio após sucesso, erro, timeout e sinal, incluindo um
descendente que chame `setsid`. Um skip não conclui a task.

## Evidência

- gate real: 4 cenários aprovados, sem skip;
- sucesso, erro, timeout e `SIGTERM` encerraram descendente em nova sessão;
- `MemoryMax` e `TasksMax` foram confirmados no scope ativo;
- suíte completa: 206 testes aprovados;
- zero unidades `agent-loop-*.scope` permaneceram após a suíte.
