# PC-02c — Gate systemd real

Status: planejada.

Objetivo único: executar um E2E obrigatório no manager `systemd --user` real.

O gate deve provar scope vazio após sucesso, erro, timeout e sinal, incluindo um
descendente que chame `setsid`. Um skip não conclui a task.
