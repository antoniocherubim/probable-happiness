# PC-01B2c — Incorporar decisão humana no estado

Status: concluída.

Objetivo único: substituir `human_approval_decision.json` pelo campo
`human_decision` de `state.json`, preservando exatamente uma decisão válida sob
os locks existentes.

Não alterar request, notification ou reports nesta etapa.

## Evidência

- aprovação publica `human_decision` e `HUMAN_APPROVED` em uma escrita;
- rejeição publica `human_decision` e `BLOCKED` em uma escrita;
- qualquer decisão humana torna o run terminal para novas transições;
- decisão e status inconsistentes fazem a leitura falhar fechada;
- corridas de callback, notificação, timeout e recriação continuam cobertas;
- código de produção não contém caminhos para os arquivos antigos de decisão;
- suíte completa: 197 testes aprovados.
