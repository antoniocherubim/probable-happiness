---
id: SELF-00P
status: planned
depends_on: []
---

# SELF-00P — Controlled project-adapter evolution

## Contexto

O controller atual recusa qualquer diff em `.agent-loop/project.toml`, compara o
profile vivo do candidato com o profile congelado e recarrega profile e
instruções a partir do worktree durante o run. Por isso `SELF-00A` não pode
atualizar o gate nem criar `reviewer.md` sem ser bloqueada ou influenciar sua
própria avaliação.

## Objetivo

Permitir uma evolução explicitamente autorizada do adapter do projeto mantendo
separadas a configuração que controla o run atual e a configuração candidata
destinada somente a runs futuros.

## Escopo permitido

- captura imutável do profile, das instruções e dos entrypoints de hooks/gates
  do adapter no base commit;
- flag CLI explícita para autorizar mudança do profile candidato;
- metadata necessária para auditar essa autorização;
- validação estrita do profile candidato sem ativá-lo no run corrente;
- caminhos de `run`, `resume`, `verify` e `integrate` afetados por essa separação;
- testes, esta task e `ROADMAP.md`.

## Fora de escopo

- criar o adapter self-hosted de `SELF-00A`;
- provenance geral do engine;
- executar controller, schema ou instruções a partir do candidato;
- relaxar o parser do profile ou permitir alteração silenciosa;
- plugin discovery, operação Git remota, deploy ou limpeza automática.

## Decisões fixadas

- O default permanece fail-closed: sem autorização explícita, mudar
  `.agent-loop/project.toml` continua sendo recusado.
- A autorização deve ser uma flag de `agent-loop run`, persistida no metadata;
  não pode ser inferida pelo nome da task nem por conteúdo candidato.
- Profile, comandos, documentação obrigatória, instruções e recursos
  executáveis do adapter que controlam um run são resolvidos do base commit
  antes do executor e permanecem imutáveis. Gates estáveis ainda recebem o
  worktree candidato como objeto de teste, sem executar um gate substituído
  pelo próprio candidato.
- Um profile candidato autorizado é validado como conteúdo de Engine/Adapter
  N+1, mas nunca fornece bootstrap, gates ou instruções ao run que o produz.
- `resume` reutiliza exatamente a visão de controle congelada. A integração
  pode transportar o profile candidato somente quando a autorização registrada
  e o snapshot revisado coincidirem.

## Critérios bloqueantes

1. Um run sem a nova flag continua recusando qualquer mudança do profile com
   mensagem estável e sem enfraquecer os artefatos existentes.
2. Um run autorizado pode revisar e integrar um profile candidato válido, mas
   bootstrap, validações e documentação do run corrente usam apenas o profile
   congelado do base.
3. Criar ou alterar `.agent-loop/reviewer.md` no candidato não muda as
   instruções do reviewer daquele run; o mesmo vale para iterações e resume.
4. Profile candidato inválido, autorização ausente/adulterada ou divergência
   entre metadata e snapshot bloqueiam antes da transição canônica.
5. Testes negativos tentam remover gates e injetar instruções pelo candidato e
   demonstram que a configuração estável continua controlando a avaliação.
6. Perfis e comandos atuais de consumidores externos continuam aceitos sem a
   nova flag e com a mesma interface pública.

## Gate focado

Esta task não pode depender da política que está criando. No primeiro run, a
suíte deve ser executada com o Python/pytest já instalado no repositório
canônico:

```bash
"$AGENT_LOOP_TARGET_REPO/venv/bin/python" -m pytest -q
python3 -m compileall -q scripts/agents
bash -n agent-loop scripts/agents/*.sh
git diff --check
```

## Entrega obrigatória

Entregar a separação entre controle congelado e adapter candidato, testes
positivos/negativos e documentação operacional. Atualizar esta task e o roadmap
com resultados reais; não registrar evidência antes da execução.

## Riscos / observações

- Esta é uma predecessora de bootstrap descoberta na inspeção do código; sem
  ela, `SELF-00A` não é executável pelo loop atual.
- O gate programático congelado deste primeiro run ainda é o profile atual;
  por isso a suíte completa acima é critério explícito do reviewer.
