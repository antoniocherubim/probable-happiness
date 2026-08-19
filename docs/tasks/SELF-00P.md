---
id: SELF-00P
status: completed
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

## Comportamento entregue

- Flag explícita: `--allow-candidate-profile` em `agent-loop run`, persistida em
  `state.json.metadata.candidate_profile_authorization` e no manifesto de
  `control-adapter/`.
- Captura imutável no `init-run` a partir dos blobs Git do commit-base: profile,
  instruções rastreadas/convencionais já existentes e o script de entrypoint de
  bootstrap/gates. Um entrypoint configurado ausente no commit-base falha fechado
  (`configured adapter entrypoint is missing from the base commit`); o candidato
  não pode criar esse arquivo depois e tê-lo executado na validação corrente.
- O resolver reconhece o script após flags de interpretador (`bash -e`,
  `bash -euo pipefail`, `/bin/bash -e`, `python3 -B`, …). `python3 -m` não é um
  entrypoint de arquivo; `rewrite_frozen_entrypoint()` insere `-P` para o módulo
  nomeado não ser carregado do cwd do worktree candidato. `bash -c` continua a
  não ser tratado como entrypoint de arquivo.
- Bootstrap, validações, documentação obrigatória e `instructions --run-dir`
  leem somente o adapter congelado. O worktree candidato permanece o objeto de
  teste. `rewrite_frozen_entrypoint()` substitui o operand do script por uma
  cópia cujo digest confere com o manifesto ou, na ausência de adapter
  materializado, pelo blob Git do commit-base; nunca pelo caminho relativo do
  worktree. Para `python -m`, o argv ganha `-P` quando ainda não há `-P`/`-I`,
  para o módulo do gate não vir do candidato; argumentos de caminho continuam
  relativos ao worktree. Cópia congelada ausente ou adulterada falha fechado.
- Sem a flag, a mensagem estável
  `executor modified .agent-loop/project.toml; resume settings must remain immutable`
  continua valendo.
- `resume` reusa a visão congelada. `verify`/`integrate` só transportam
  `.agent-loop/project.toml` quando autorização, manifesto e `candidate-profile.json`
  coincidem com o snapshot revisado.

## Gate executado

```bash
"$AGENT_LOOP_TARGET_REPO/venv/bin/python" -m pytest -q
python3 -m compileall -q scripts/agents
bash -n agent-loop scripts/agents/*.sh
git diff --check
```

## Evidência de testes

Executado neste worktree em 2026-08-19, com o Python do repositório canônico:

| Comando | Resultado |
|---|---|
| `venv/bin/python -m pytest -q` | **240 passed**, 0 failed, 0 skipped, 0 errors em 67.26s |
| `python3 -m compileall -q scripts/agents` | exit 0 |
| `bash -n agent-loop scripts/agents/*.sh` | exit 0 |
| `git diff --check` | exit 0 |

Os 16 testes em `tests/unit/test_agent_self00p.py` cobrem recusa sem flag,
autorização explícita, gate congelado após substituição candidata, injeção de
`reviewer.md`, profile inválido, autorização adulterada, divergência
metadata/snapshot, interface pública sem a flag, um run autorizado que chega a
`verify`/`resume`/`integrate`, e os negativos de revisão: candidato cria um gate
configurado ausente no base (`bash -e scripts/gate.sh`) e a inicialização/rewrite
falham fechados; candidato substitui um gate invocado com opções de
interpretador e só o snapshot congelado do base é executado; candidato planta
`compileall.py` no worktree para sombrear o `python3 -m compileall` congelado e
o módulo candidato não é executado (`-P` isola a resolução; o compileall da
stdlib ainda processa o objeto de teste).

Não há SHA de commit nem URL de branch neste registro: a integração permanece
manual e ainda não ocorreu.

## Riscos / observações

- Esta é uma predecessora de bootstrap descoberta na inspeção do código; sem
  ela, `SELF-00A` não é executável pelo loop atual.
- O gate programático congelado deste primeiro run ainda é o profile atual;
  por isso a suíte completa acima é critério explícito do reviewer.
- A captura de entrypoints cobre o script nomeado no argv após flags do
  interpretador, não helpers `source`d internamente nem arquivos carregados por
  `--rcfile`/`--init-file`.
- O rewrite insere `-P` em `python3 -m` (ou preserva `-P`/`-I` já presentes),
  então o módulo do gate é resolvido sem o cwd do candidato. `PYTHONPATH`
  autorizado que aponte para o worktree ainda pode sombrear o módulo;
  `python3 -c` e `bash -c` continuam inline e podem importar/ler o cwd.
- Runs criados antes desta mudança não têm `control-adapter/` materializado;
  o resume lê instruções do commit-base via Git. Validação de um script relativo
  nesses runs usa o blob Git do commit-base, não o worktree candidato. `python -m`
  nesses runs ainda recebe `-P` no rewrite, sem depender da cópia materializada.
- Hashes e o manifesto detectam drift e adulteração parcial; reescrita
  coordenada de metadata e manifesto pelo mesmo UID permanece fora do modelo
  autenticado.
- O parser do profile candidato não foi relaxado; schema desconhecido continua
  recusado.
