# Plano de avaliação experimental

## Status

Este é um **plano**, não um relatório de resultados. Nenhuma comparação abaixo
foi concluída e nenhuma hipótese é considerada confirmada.

O objetivo é converter o `probable-happiness` de artefato de engenharia em uma
plataforma experimental capaz de responder quais mecanismos contribuem para a
confiabilidade de mudanças produzidas por agentes.

## Objetivo primário

Medir, sob tasks e critérios externos controlados, se diferentes mecanismos do
workflow alteram:

- a taxa de defeitos que chegam a uma condição de aceitação;
- a taxa de tarefas concluídas corretamente;
- a quantidade de retrabalho;
- o custo de execução/review;
- as classes de falha detectadas em cada estágio.

## Unidade de análise

A unidade principal proposta é um **run de uma task versionada contra um
commit-base fixo**.

Uma observação experimental deve, no mínimo, poder ser vinculada a:

```text
repository/version
base commit
task/version
engine version
executor/reviewer configuration
validation configuration
iteration budget
candidate result
review result
final evaluation
```

A captura estruturada completa desses campos ainda depende de itens do roadmap,
em especial `PROV-01A` e `OBS-01A`.

## Separar mecanismo de avaliação externa

Os mesmos testes usados pelo agente durante o run não devem ser a única medida
de correção experimental.

Idealmente cada task possui:

```text
visible acceptance/gates
        +
hidden or held-out evaluation
```

O held-out evita concluir que um workflow é melhor apenas porque aprendeu a
satisfazer exatamente o gate visível.

Para mudanças reais de projetos, uma revisão humana cega ou uma suíte externa
pode complementar o oracle quando não houver teste automático suficiente.

## Condições experimentais candidatas

A comparação exata depende de como o orientador quiser delimitar a IC. Uma
primeira matriz possível é:

### A — Executor only

```text
executor → candidate → external evaluation
```

Sem usar a decisão do próprio executor como oracle.

### B — Executor + self-review

```text
executor → same agent/model reviews → candidate → external evaluation
```

Essa condição depende de tornar papéis/drivers configuráveis ou de um harness
experimental separado. Não está implementada no runtime atual.

### C — Separate review

```text
executor → programmatic validation → separate reviewer → external evaluation
```

### D — Full workflow

```text
executor
  → candidate isolation
  → programmatic validation
  → separate reviewer
  → content-bound approval
  → integration revalidation
  → external evaluation
```

Para comparações que deliberadamente removem safety gates, usar repositórios e
branches descartáveis. **Não integrar variantes inseguras em projetos reais só
para produzir uma ablation.** Em vários casos é suficiente avaliar o candidate
offline sem executar `integrate`.

## Ablations candidatas

Depois de uma baseline funcional, estudar uma dimensão por vez:

```text
full workflow
  - separate reviewer
  - programmatic validation
  - content binding
  - candidate isolation
  - integration revalidation
```

Nem toda ablation precisa ser implementada no produto. Algumas podem ser
simuladas/reproduzidas em um runner experimental para não enfraquecer o harness
operacional.

## Métricas primárias

### Accepted defect rate

Proporção de runs que seriam considerados aceitos pelo workflow mas falham no
oracle externo/held-out.

```text
accepted defective runs / accepted runs
```

Essa é a métrica mais diretamente ligada à hipótese de confiabilidade.

### Correct task completion rate

Proporção de tasks que satisfazem o oracle externo final, independentemente do
número de iterações.

### Detection yield por estágio

Quantos defeitos/falhas são detectados por:

- validation;
- reviewer;
- integration revalidation;
- oracle externo somente.

O objetivo é identificar sobreposição e contribuição marginal, não apenas uma
contagem total de findings.

## Métricas secundárias

Quando a instrumentação permitir:

- first-pass approval rate;
- número de `CHANGES_REQUESTED` por run;
- iterações até conclusão/bloqueio;
- validation failures por estágio;
- findings por severidade/categoria;
- tempo de executor, validation e reviewer;
- tokens/uso de modelo quando recuperável de fonte confiável;
- custo monetário estimado quando houver dados suficientes;
- arquivos/linhas alteradas;
- violações de escopo detectadas;
- false rejection rate, quando um oracle externo indicar que uma mudança
  rejeitada estava correta.

Não inferir contagem de testes a partir de texto livre do agente quando o dado
não estiver disponível em fonte estruturada.

## Dataset e observabilidade

`OBS-01A` pretende normalizar os artefatos já persistidos em um export JSON por
run. Antes de uma análise, definir um schema versionado e registrar quais campos
são:

- observados diretamente;
- derivados deterministicamente;
- indisponíveis;
- provenientes de texto agentic e, portanto, não confiáveis para métricas sem
  validação externa.

Um valor ausente deve permanecer ausente. O caso `SELF-00P`, em que a suíte foi
executada mas a contagem terminal não estava disponível ao notifier, é um bom
exemplo de por que não se deve preencher dados por inferência.

## Seleção de tasks

Evitar escolher apenas tasks em que o workflow atual já teve sucesso.

Uma amostra inicial deve variar pelo menos:

- tipo de mudança: bugfix, feature pequena, refactor, testes, documentação com
  comportamento executável;
- tamanho/complexidade;
- presença de testes existentes;
- linguagem/ecossistema, se o escopo permitir;
- probabilidade de exigir múltiplas iterações.

Se `artang-platform` for usado como fonte de tasks, congelar commits e critérios
para evitar que evolução contínua do produto altere o benchmark no meio da
comparação.

## Repetições e stochasticity

Uma única execução por condição não é suficiente para caracterizar modelos
estocásticos.

Idealmente cada task/condição terá múltiplas repetições, com:

- versão/configuração dos agentes registrada;
- prompts/instruções versionados;
- base commit idêntico;
- limite de iterações idêntico;
- mesmos gates/oracle externo;
- ordem de execução randomizada quando possível.

O número de repetições deve ser definido com o orientador após um piloto e uma
estimativa de variância/custo.

## Caso observacional `SELF-00P`

O run de `SELF-00P` pode aparecer na apresentação como **caso motivador**:

```text
candidate implementation
      ↓
review encontra gap em python -m
      ↓
CHANGES_REQUESTED
      ↓
correção + teste adversarial
      ↓
APPROVED na iteração 3/3
```

Ele demonstra que o mecanismo de revisão produziu uma correção relevante em um
caso real. Não deve ser incluído sozinho como evidência de H1/H2.

## Ameaças à validade

### Construct validity

"Testes passaram" não é sinônimo de "software correto". O oracle precisa medir
o conceito que a pesquisa chama de defeito/conclusão correta.

### Internal validity

Diferenças de modelo, contexto, prompt, tool version, latência externa ou
ambiente podem explicar resultados atribuídos ao workflow. Provenance e
controle experimental são essenciais.

### External validity

Resultados em um repositório, linguagem ou conjunto pequeno de tasks podem não
generalizar para outros projetos.

### Reviewer dependence

Um reviewer LLM pode ter vieses correlacionados com o executor, especialmente
quando modelos/famílias são semelhantes. "Separate reviewer" não deve ser
interpretado automaticamente como avaliação independente.

### Benchmark leakage / contamination

Tasks públicas ou muito conhecidas podem ter aparecido em dados de treino.
Preferir tasks próprias congeladas ou mutações controladas quando apropriado.

### Tool/version drift

Cursor, Codex e seus modelos podem mudar fora do repositório. Sem provenance
suficiente, execuções temporalmente separadas podem deixar de ser comparáveis.

### Environment effects

Parte da suíte atual depende de `systemd --user`. Falhas de ambiente devem ser
separadas de defeitos do candidate, como já ocorreu durante a revisão de
`SELF-00P`.

## Critério para começar a coleta formal

Antes de chamar qualquer run de "dado experimental", idealmente concluir:

1. engine provenance suficiente para identificar o runtime (`PROV-01A`);
2. política explícita de drift para runs longos/retomados (`PROV-01B` ou decisão
   metodológica equivalente);
3. export estruturado (`OBS-01A`);
4. schema de findings/métricas necessário para a análise escolhida;
5. versão congelada de benchmark/tasks;
6. protocolo experimental escrito antes de observar os resultados comparativos.

Isso não impede apresentar ou demonstrar o sistema agora. Apenas separa
**engenharia existente** de **coleta científica formal**.
