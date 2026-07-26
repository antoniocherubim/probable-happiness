# PC-01A — Remover o legado de delivery

Status: concluída.

## Objetivo

Remover do runtime todo estado, lock, configuração e mensagem referente a
delivery Git antigo.

## Aceite

- não existem estados `DELIVERING`, `DELIVERY_FAILED` ou `PUSHED`;
- não existe `.delivery.lock` nem `delivery.json` no fluxo;
- `[delivery]` é configuração desconhecida e falha no profile;
- resume aprovado apenas verifica o snapshot e preserva a worktree;
- Git continua sem push, pull, fetch, commit ou merge;
- suíte completa passa.

Não alterar ainda o formato de estado; isso pertence à PC-01B.

## Evidência

- 201 testes focados de estado, aprovação, orçamento e Git local;
- 245 testes unitários completos;
- `compileall`, `bash -n` e `git diff --check`;
- nenhuma referência de runtime a `.delivery.lock`, `DELIVERING`,
  `DELIVERY_FAILED` ou `PUSHED`.

A redução de 315 para 245 testes removeu matrizes dedicadas somente aos estados
e locks antigos; não houve remoção de comportamento do fluxo pessoal.
