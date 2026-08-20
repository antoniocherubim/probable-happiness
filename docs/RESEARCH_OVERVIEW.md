# Research overview

## Resumo

`probable-happiness` é um artefato de software que surgiu da automação de um
workflow pessoal: um agente implementava mudanças, outro revisava, validações
eram executadas e o resultado só era incorporado conscientemente depois de
revisado.

A implementação evoluiu para um protocolo local com candidate worktrees,
validação programática, revisão separada, bindings de conteúdo, máquina de
estados e integração local explícita. O projeto não começou como pesquisa; a
pergunta científica foi reconhecida **depois** que a arquitetura já estava em
uso.

Este documento descreve como transformar o artefato em objeto de investigação
sem confundir mecanismo implementado com resultado científico.

## Problema de pesquisa

Uma formulação central possível é:

> **Como permitir que agentes probabilísticos proponham mudanças em sistemas de
> software sem conceder-lhes autoridade unilateral sobre quais estados se
> tornam canônicos?**

Em termos de processo:

```text
probabilistic proposal
        ↓
candidate state
        ↓
programmatic evidence
        ↓
separate review
        ↓
content-bound approval
        ↓
controlled canonical transition
```

A contribuição potencial a investigar está no **processo/arquitetura de
controle**, não em um novo modelo de linguagem.

## Origem do artefato

O projeto foi construído para automatizar uma prática existente de trabalho com
agentes de programação. Isso é relevante porque a arquitetura original não foi
retrospectivamente desenhada para satisfazer uma hipótese acadêmica.

A tag `self-hosting-bootstrap` (`785485f`) preserva uma baseline útil desse
estado orgânico. A partir dela, o roadmap atual começou uma fase deliberada de
hardening e observabilidade.

Essa distinção permite tratar:

```text
E0 = arquitetura orgânica antes do hardening orientado à pesquisa
E1+ = arquitetura evoluída após limitações serem explicitadas
```

sem reescrever a história do artefato.

## O que está implementado

No estado pós-`SELF-00P`, o harness possui mecanismos para:

- executar uma task versionada em candidate worktree separado;
- usar Cursor como executor e Codex como reviewer em fases distintas;
- executar validações programáticas configuradas por projeto;
- retornar `CHANGES_REQUESTED` ao executor dentro de um orçamento explícito;
- detectar mutação do conteúdo durante review;
- vincular `APPROVED` ao conteúdo revisado por manifesto/hashes;
- revalidar bindings antes de integração;
- integrar localmente após comando explícito ou publicar o snapshot aprovado
  em branch/PR isolados quando o profile selecionar `github_pr`;
- persistir estado e permitir retomada controlada;
- congelar elementos do project adapter que governam o run enquanto uma versão
  candidata desse adapter é proposta para runs futuros.

Essas propriedades estão documentadas em [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Observações já disponíveis

### Uso externo: `artang-platform`

O harness é usado como runtime externo durante o desenvolvimento do
`artang-platform`. O consumidor mantém localmente regras de domínio como
bootstrap, dependências de tasks, gates específicos e instruções de
executor/reviewer, sem duplicar o núcleo do loop.

**Interpretação permitida:** existe evidência prática de reutilização e de uma
fronteira útil entre núcleo e policy de projeto.

**Interpretação não permitida:** isso não demonstra que a arquitetura produz
software melhor, mais rápido ou mais confiável que alternativas.

### Self-hosting: `SELF-00P`

A primeira task de hardening self-hosted foi concluída em três iterações. Durante
o processo, a revisão identificou que `python -m` poderia resolver o módulo a
partir do cwd candidato; a implementação foi revisada para usar `-P` quando
necessário e ganhou um teste negativo que planta um módulo homônimo no candidate
worktree.

A task registra uma execução completa de `240 passed` em ambiente com
`systemd --user`. Em outro ambiente de review, parte da suíte dependente de
`systemd --user` não pôde executar nas mesmas condições.

**Interpretação permitida:** é um caso concreto em que o loop encontrou e
corrigiu um problema relevante antes da aprovação final.

**Interpretação não permitida:** um caso isolado não estabelece causalidade nem
superioridade estatística do processo.

### Gap de observabilidade observado

O resumo terminal de um run pode não recuperar uma contagem estruturada de
testes mesmo quando a suíte foi executada por fora do validation log que o
parser reconhece. No caso de `SELF-00P`, a evidência da task registrou os testes,
mas o resumo apresentado pelo notifier indicou contagem indisponível.

Isso motiva `OBS-01A`: normalizar artefatos persistidos em um export estruturado
antes de usar os runs como dataset experimental.

## Hipóteses candidatas

As hipóteses abaixo **não foram testadas**.

### H1 — review separado

Adicionar uma fase de review separada do executor reduz a taxa de defeitos que
seriam aceitos quando comparado a um fluxo executor-only ou self-review, sob
mesmas tasks e critérios externos.

### H2 — composição de mecanismos

Validação programática, review separado, content-bound approval e revalidação de
integração detectam classes parcialmente diferentes de falha; a composição pode
ser mais efetiva que qualquer mecanismo isolado.

### H3 — custo de confiabilidade

O ganho potencial de detecção vem acompanhado de custo em latência, tokens,
execuções de ferramentas e ciclos de retrabalho. Existe um trade-off mensurável
entre custo e defeitos evitados.

### H4 — policy/controller separation em self-hosting

Congelar a policy que controla o run reduz uma classe de falhas em que o
candidate altera as regras pelas quais ele próprio é avaliado. A generalização
desse princípio para provenance e engine drift ainda precisa ser implementada e
testada.

## Perguntas de pesquisa candidatas

### RQ1

**Uma fase de review separada reduz defeitos aceitos em comparação com
executor-only e self-review?**

### RQ2

**Qual contribuição marginal de validação programática, review separado,
content binding e revalidação de integração para a detecção de falhas?**

### RQ3

**Qual é o custo adicional por defeito evitado em termos de latência, iterações,
tokens e execução de ferramentas?**

### RQ4

**Quais falhas aparecem quando uma ferramenta agentic participa da evolução de
seu próprio runtime, e quais bindings são necessários para manter um controller
estável durante essa transição?**

RQ4 pode ser uma sublinha de engenharia/self-hosting; ela não precisa ser o foco
principal de uma primeira iniciação científica.

## Delimitação sugerida para uma IC

Para uma primeira investigação, o escopo mais tratável é **Engenharia de
Software experimental**:

1. congelar a versão experimental do harness;
2. escolher um conjunto de tasks/repositórios controlados;
3. executar baselines e variantes sob condições comparáveis;
4. coletar evidência estruturada;
5. medir defeitos aceitos, detecções, retrabalho e custo;
6. analisar ameaças à validade.

A formalização de propriedades do state machine ou do stable controller pode ser
um desenvolvimento posterior ou uma linha complementar, não uma condição para
começar o estudo empírico.

## Posição sobre novidade

Neste momento, o projeto **não reivindica**:

- invenção do padrão generate-review-revise;
- invenção de sistemas multiagentes para programação;
- invenção de proposer/verifier architectures;
- implementação de verificação formal;
- implementação de *Speculative Reasoning* no sentido de inference-time
  reasoning;
- superioridade empírica sobre outros workflows.

Uma revisão bibliográfica sistemática ainda deve comparar o artefato com linhas
como iterative refinement, self-review, multi-agent software engineering,
LLM-as-a-judge, proposer/verifier systems, process assurance e técnicas de
verificação/validação de software.

A pergunta de novidade deve permanecer aberta até essa revisão.

## Terminologia recomendada

Preferir:

- **verification-driven agentic execution**;
- **candidate/speculative execution at the action/state level**;
- **programmatic/non-LLM validation**;
- **separate review stage**;
- **content-bound approval**;
- **controlled canonical state transition**;
- **self-hosting** para o harness trabalhando em sua própria evolução.

Evitar como claim principal:

- "formal verification";
- "autonomous self-improving AI";
- "Speculative Reasoning" sem a qualificação de que a analogia está no nível de
  ação/estado;
- "independent reviewer" quando a frase puder ser interpretada como
  independência estatística/formal, em vez de separação arquitetural.

## Próximo passo científico

O próximo passo não é adicionar mais agentes. É tornar os runs comparáveis e
mensuráveis, preservar versões/provenance e executar um desenho experimental
pré-definido. O plano inicial está em [`EVALUATION_PLAN.md`](EVALUATION_PLAN.md).
