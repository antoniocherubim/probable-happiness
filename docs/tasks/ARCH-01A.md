---
id: ARCH-01A
status: planned
depends_on:
  - SELF-01A
  - TASK-01B
---

# ARCH-01A — Define project adapter contract

## Contexto

A fronteira `engine genérico + policy/adapter do projeto` já existe de fato em
profiles, hooks, instruções e tasks. Ela ainda está distribuída entre código e
documentos e não possui um contrato único e testado.

## Objetivo

Tornar explícita a interface estável entre Agent Loop Engine e Project Adapter,
com o menor refactor necessário e sem criar framework de plugins.

## Escopo permitido

- contrato de profile, bootstrap, validation, instructions e task policy;
- cwd, variáveis `AGENT_LOOP_*`, argv, exit status, limites e trust;
- distinção entre adapter congelado e conteúdo candidato;
- documentação e tipos/helpers internos pequenos para reduzir duplicação;
- fixture compatível com o padrão observado em `artang-platform`;
- testes, esta task e `ROADMAP.md`.

## Fora de escopo

- sistema de plugins, entry points, discovery dinâmico ou marketplace;
- importar regras, IDs, `uv`, Docker ou PostgreSQL do consumidor;
- alterar o protocolo de run/review/approval/integration;
- driver abstraction, banco, telemetria ou operação remota.

## Decisões fixadas

- O adapter continua versionado no target repo e é composto por recursos já
  conhecidos; não recebe código executável arbitrário por discovery.
- Profile escolhe hooks e limites; o engine fornece contexto mínimo, supervisão,
  estado e bindings de conteúdo.
- Policy de domínio e dependências permanece no adapter; o engine conhece
  somente contratos genéricos.
- Bytes do adapter que controlam um run vêm do base e ficam congelados; mudança
  candidata torna-se elegível somente para o próximo run.
- A primeira versão é documentação/refactor do que existe, sem nova camada
  dinâmica.

## Critérios bloqueantes

1. Um documento/tipo central define responsabilidades, inputs, outputs, falhas,
   mutabilidade e trust de cada superfície do adapter.
2. Fixture externa usa profile, bootstrap, validation, instructions e task
   preflight sem copiar engine nem depender de internals DX.
3. Paths absolutos/`..`, unknown keys, hook ausente, ambiente extra e mutação
   bootstrap continuam falhando fechados.
4. Contrato registra que validação candidata testa N+1, mas não é a única
   garantia que protege o controller N.
5. A integração observada do consumidor permanece válida sem migração imediata.
6. Nenhum novo dependency/framework externo ou efeito Git remoto é introduzido.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_dx02.py \
  tests/unit/test_agent_pc03.py \
  tests/unit/test_agent_local_only.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar o contrato versionado e uma fixture de adapter externo. Atualizar esta
task e roadmap com regressões executadas, sem modificar `artang-platform`.

## Riscos / observações

- A interface documentada deve refletir comportamento implementado, não uma
  plataforma de plugins futura.

