# Arquitetura — verification-driven agentic execution

## Escopo deste documento

Este documento descreve **o comportamento implementado no estado pós-`SELF-00P`**
do `probable-happiness`. A baseline anterior ao hardening está preservada na tag
`self-hosting-bootstrap` (`785485f`); `SELF-00P` foi integrada em `bb00503`.

O objetivo aqui é documentar mecanismos reais e seus limites. Itens do roadmap
que ainda não foram implementados aparecem explicitamente como planejados.

## Problema de engenharia

Agentes baseados em LLM são úteis para produzir mudanças de software, mas a
saída de um agente não deve ser confundida com autoridade para tornar aquela
mudança canônica.

O harness separa dois conceitos:

```text
candidate state  !=  canonical repository state
```

A execução pode explorar/modificar um estado candidato. A promoção desse estado
para a branch local exige uma sequência distinta de validação, review, binding
de conteúdo e integração explícita.

## Fluxo atual

```text
Task versionada no base commit
            │
            ▼
   Candidate worktree isolado
            │
            ▼
        Executor
      (Cursor hoje)
            │
            ▼
 Programmatic validations
            │
            ▼
  Pre-review content hash
            │
            ▼
         Reviewer
      (Codex hoje)
            │
      ┌─────┴─────────┐
      │               │
CHANGES_REQUESTED   APPROVED
      │               │
      └─> Executor     ▼
                 reviewed manifest
                   + content hash
                        │
              ┌─────────┴─────────┐
              ▼                   ▼
      verify + integrate     github_branch publish
          (explicit)          (dedicated branch)
              │                   │
              ▼                   ▼
     local canonical branch   dedicated remote branch
```

O runtime atual possui papéis distintos de executor e reviewer, mas ainda não
possui uma interface genérica de drivers: Cursor e Codex são implementações
concretamente acopladas ao fluxo atual. A separação papel/vendor está no backlog.

## Componentes

### CLI e runner

`agent-loop` expõe os comandos de operação. `scripts/agents/run_task.sh`
orquestra o ciclo principal e usa os módulos de `scripts/agents/dx/` para
persistência, estado, profile, snapshot, supervisão e integração.

### Candidate worktree

Cada run normal cria um worktree Git externo ao checkout canônico, ancorado no
commit-base. O candidato modifica esse worktree; a branch local original não é
alterada durante execução/review.

O isolamento é principalmente **isolamento de estado Git**, não um sandbox de
segurança contra um repositório deliberadamente hostil. Processos do mesmo UID
fazem parte do modelo de confiança declarado.

### Programmatic validation

O projeto consumidor fornece comandos de validação em `.agent-loop/project.toml`.
Esses comandos são executados fora do LLM e geram evidência programática.

"Programmatic" ou "non-LLM validation" é a terminologia preferida. Como o
profile pode declarar comandos arbitrários dentro do contrato permitido, não se
alega que toda validação configurável seja matematicamente determinística.

### Separate review stage

Depois das validações, o reviewer recebe a task, o diff e a evidência disponível.
Relatórios do executor são tratados como evidência não confiável até serem
confrontados com o repositório e os gates.

A independência aqui é **arquitetural**: existe uma fase/revisor separado do
executor. Não se alega independência estatística, formal ou entre famílias de
modelo.

### Review mutation detection

O harness calcula o estado revisável antes e depois do reviewer. Se o conteúdo
muda durante o review, o run não aceita silenciosamente a decisão como se ela se
referisse ao snapshot original.

Isso é relevante porque o reviewer também é um processo agentic: a política não
depende apenas de uma instrução textual de "não editar".

### Content-bound approval

A aprovação técnica é ligada a um manifesto e a hashes do conteúdo revisado.
São considerados, entre outros elementos do snapshot, diff Git, untracked,
tipo/modo e conteúdo relevante.

A propriedade é:

```text
approval applies to reviewed content C
```

não:

```text
the worktree can never change after approval
```

O worktree continua mutável pelo ambiente local. Se os bytes mudarem, a
verificação deixa de corresponder ao conteúdo aprovado e a integração falha.
Por isso a terminologia correta é **content-bound approval** ou
**content-addressed review binding**, e não "immutable stored snapshot".

### Integração local ou publicação isolada

`agent-loop integrate` é uma ação explícita do operador. Ela:

1. verifica estado terminal e bindings relevantes;
2. exige branch/check-out nas condições esperadas e repositório limpo;
3. relê os bytes associados ao manifesto e valida hashes;
4. monta a tree usando index temporário;
5. executa checks locais como `git diff --check` sobre o conteúdo a integrar;
6. cria um commit local com hooks desativados;
7. revalida antes da atualização final;
8. avança apenas por fast-forward local.

O comando **faz commit e fast-forward local quando explicitamente solicitado**.
Nos modos `none` e `telegram`, não há publicação remota. No modo explícito
`github_branch`, o controller reutiliza a construção segura do commit, mantém a
branch canônica intacta e publica uma branch dedicada sem force. Abertura de PR,
merge, deploy e decisão humana permanecem fora do runtime.

## Máquina de estados

As transições relevantes passam por uma máquina de estados tipada e locks
locais. Estados correntes incluem:

```text
EXECUTING
REVIEWING
CHANGES_REQUESTED
APPROVED
BLOCKED
```

Estados de aprovação humana permanecem apenas para compatibilidade de runs
legados.

O objetivo do compare-and-set sob lock é impedir que uma aresta inesperada
substitua silenciosamente o último estado válido. Isso é um mecanismo local de
consistência; não é um protocolo distribuído tolerante a processos maliciosos.

## Controlled project-adapter evolution (`SELF-00P`)

Self-hosting criou um problema circular: se o candidate worktree puder trocar
os arquivos que definem como ele próprio será validado/revisado, a distinção
entre controller e candidate enfraquece.

`SELF-00P` introduziu uma separação entre:

```text
control adapter for current run
             !=
candidate adapter proposed for future runs
```

### Visão de controle congelada

Para runs novos, elementos relevantes do adapter são capturados a partir do
commit-base e persistidos no run. O objetivo é que alterações candidatas em
profile/instruções/gates não passem a governar retroativamente o run que as
produz.

Por padrão, mudar `.agent-loop/project.toml` continua recusado. Uma mudança
deliberada exige `--allow-candidate-profile`; a autorização é registrada e o
profile candidato ainda precisa ser validado e vinculado ao snapshot revisado.

### Entry points congelados

Quando um comando usa um script relativo reconhecido como entrypoint, o runtime
substitui esse operand pela cópia congelada/validada correspondente em vez de
executar o script candidato como controller.

A implementação reconhece opções de interpretadores em casos suportados. Para
`python -m`, `SELF-00P` adicionou `-P` quando necessário para impedir que o
módulo nomeado seja sombreado pelo cwd do candidate worktree.

O teste de regressão correspondente planta um `compileall.py` no candidato e
verifica que ele não substitui o módulo usado pelo gate.

### Limites conhecidos dessa proteção

A captura atual não pretende resolver toda forma possível de carregamento
indireto de código. Entre os limites documentados:

- helpers `source`d internamente por scripts não são automaticamente congelados
  só por serem dependências do entrypoint;
- `--rcfile`/`--init-file` e formas inline como `bash -c`/`python -c` exigem
  análise própria;
- um `PYTHONPATH` explicitamente autorizado apontando para o candidato ainda
  pode influenciar resolução de imports;
- adulteração coordenada de metadata/manifesto por processo malicioso do mesmo
  UID está fora do modelo autenticado.

Esses limites são parte do modelo de ameaça, não garantias omitidas.

## Engine N e candidate N+1

Após `SELF-00P`, já existe um mecanismo concreto para impedir que **parte da
policy candidata** governe o run atual. O roadmap pretende ampliar e tornar
explícito o princípio:

```text
Engine N
   │
   └── controls/evaluates ──> candidate Engine N+1
```

Durante a construção de N+1, o candidato não deve assumir o papel de controller
do próprio julgamento.

Importante: a captura completa de **engine provenance**, fail-closed geral em
**engine drift** e a formalização completa do **stable-controller invariant**
ainda são tasks planejadas (`PROV-01A`, `PROV-01B`, `SELF-01A`). Não devem ser
descritas como garantias já implementadas.

## Project adapter e uso externo

Na prática, projetos consumidores já mantêm uma camada específica contendo
profile, instruções, bootstrap e gates. O núcleo do loop continua externo.

O `artang-platform` é um caso real de consumo: sua integração local cresceu
para expressar dependências de tasks e gates de domínio sem duplicar o núcleo
do harness. Isso sustenta a observação de que existe uma fronteira útil entre
**engine** e **project policy**, mas a interface formal de `Project Adapter`
ainda é trabalho planejado (`ARCH-01A`).

## Trust boundaries

### Autoridades diferentes

```text
Executor
  pode propor mudanças no candidate worktree

Reviewer
  pode avaliar e pedir mudanças/aprovar tecnicamente
  mutação do snapshot durante review é detectada

Programmatic validators
  fornecem evidência externa ao LLM conforme profile

Operator
  configura o modo e decide explicitamente integrate ou review/merge do PR

Remote repository
  recebe somente a branch no modo github_branch; aplica suas próprias proteções
```

### Modelo de confiança

Assume-se confiável o host local, o usuário e processos do mesmo UID. Root,
host comprometido e adversário com capacidade de reescrever coordenadamente o
state root estão fora do modelo.

O bloqueio de protocolos Git remotos (`GIT_ALLOW_PROTOCOL=file`) reduz uma
classe específica de efeitos remotos via Git, mas não é um namespace de rede e
não bloqueia clientes HTTP genéricos.

## Garantias implementadas vs. não-garantias

| Propriedade | Estado |
|---|---|
| Candidate separado da branch canônica durante run normal | Implementado |
| Executor e reviewer em fases separadas | Implementado |
| Validações programáticas configuráveis | Implementado |
| Mutação durante review detectada | Implementado |
| Aprovação vinculada ao conteúdo revisado | Implementado |
| Drift pós-review detectado antes da integração | Implementado |
| Integração local explícita e sem Git remoto | Implementado |
| Branch dedicada sem PR ou merge no modo `github_branch` | Implementado |
| Exclusão de comunicação Telegram no modo `github_branch` | Implementado |
| Adapter de controle congelado para self-hosting/profile evolution | Implementado em `SELF-00P` |
| Provenance exata da versão do engine em todo run | Planejado |
| Fail-closed geral ao retomar run com engine diferente | Planejado |
| Stable-controller invariant completo do engine | Planejado |
| Export estruturado de dataset de runs | Planejado |
| Project Adapter formalizado como contrato | Planejado |
| Executor/reviewer desacoplados de Cursor/Codex | Planejado |
| Verificação formal de correção do software | Não fornecido |
| Isolamento contra adversário do mesmo UID | Não fornecido |
| Garantia de que reviewer está correto | Não fornecido |

## Por que isso é interessante para pesquisa

A arquitetura torna explícita uma fronteira entre computação probabilística e
transições de estado controladas. A hipótese a ser testada não é "dois agentes
são melhores", mas quais mecanismos — validação programática, review separado,
binding de conteúdo, isolamento candidato e revalidação — contribuem para
reduzir mudanças defeituosas ou não intencionais que chegam ao estado canônico,
e a qual custo.

Veja [`RESEARCH_OVERVIEW.md`](RESEARCH_OVERVIEW.md) e
[`EVALUATION_PLAN.md`](EVALUATION_PLAN.md).
