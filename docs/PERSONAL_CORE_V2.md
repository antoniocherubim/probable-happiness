# Personal Core v2

Status: reorganização em andamento; PC-02 concluída.

Esta linha substitui o roadmap de produto público por um runner pequeno para
uso pessoal. A branch `personal-stable` permanece como referência do protótipo
anterior; runs antigos não serão migrados.

## Objetivo

Executar uma task em worktree Git isolada, revisar o resultado, pedir uma
decisão humana opcional e preservar o snapshot aprovado para integração manual.

Fluxo:

1. criar worktree destacada em um commit local;
2. executar Cursor Agent;
3. executar validações configuradas;
4. revisar o diff com Codex;
5. repetir no máximo uma vez quando houver defeito pertencente à task;
6. opcionalmente pedir aprovação pelo Telegram;
7. verificar novamente o hash e preservar a worktree.

## Invariantes

- Git é local: o runner nunca executa commit, merge, rebase, tag, push, pull,
  fetch, criação de PR ou deploy;
- somente um loop pode estar ativo por state root;
- toda fase roda em scope `systemd --user`;
- finalizar, interromper ou exceder limite deixa o scope sem processos;
- stdout/stderr, arquivos e duração possuem limites configurados;
- `state.json`, sob um único `flock`, é a única autoridade de estado;
- escrita de estado usa arquivo temporário, `fsync`, replace e `fsync` do
  diretório;
- reports são evidência não confiável; quando relevantes, o estado guarda seu
  hash;
- o snapshot aprovado é imutável e verificado antes da integração manual;
- state root, run dirs e artefatos privados usam `0700`/`0600`;
- segredos são allowlisted por fase e nunca entram em logs.

## Modelo de confiança

- computador, usuário local e processos do mesmo UID são confiáveis;
- root, host comprometido e adulteração deliberada pelo mesmo UID estão fora;
- tasks podem falhar, travar ou produzir saída excessiva;
- repositórios deliberadamente hostis não são suportados;
- Telegram é um adaptador opcional; indisponibilidade não pode corromper o run.

## Estado simples

Cada run novo terá um único `state.json` versionado contendo:

- identidade do run, repositório, task, worktree e base commit;
- status, fase, iteração e orçamento;
- resultado/falha estruturados;
- hash do diff revisado;
- decisão humana, quando utilizada;
- timestamp da última atualização.

Artefatos grandes continuam em arquivos separados e são referenciados por hash.
Uma escrita incompleta nunca substitui o último `state.json` válido. Arquivo
publicado antes de uma queda e ainda não referenciado é órfão descartável.

Não haverá journal transacional próprio, audit chain, registry de migrations,
rollback de schema ou compatibilidade de escrita com runs anteriores. A branch
antiga continua disponível para inspeção histórica.

## Componentes

### Núcleo obrigatório

- configuração por projeto;
- worktree local e snapshot;
- supervisor systemd;
- estado/resume;
- executor, validação e reviewer;
- limites de duração, saída e arquivos.

### Adaptadores opcionais

- Telegram para notificação e decisão humana;
- operação persistente por tmux/SSH.

### Removido

- qualquer delivery Git;
- estados e locks de delivery;
- transações JSON multi-artefato;
- migration/backup/rollback automático de runs;
- audit trail encadeado;
- hardening contra processos deliberadamente hostis do mesmo UID;
- roadmap, empacotamento e compatibilidade para terceiros.

## Política de review

O reviewer valida somente:

1. critérios de aceite da task atual;
2. invariantes deste documento tocadas pela mudança;
3. regressões demonstráveis nos testes relevantes.

Concorrência, segurança, migrations ou refatorações não citadas pela task não
podem bloquear o run. Devem aparecer, no máximo, como nota de backlog. O
orçamento normal é uma iteração de implementação e uma corretiva.

## Sequência de construção

| Etapa | Resultado |
|---|---|
| PC-00 | contrato pessoal e reviewer limitado ao escopo — concluída |
| PC-01A | remoção completa do vocabulário de delivery — concluída |
| PC-01B1 | `state.json` para metadata e status — concluída |
| PC-01B2a | failure dentro do estado — concluída |
| PC-01B2b | orçamento dentro do estado — concluída |
| PC-01B2c | decisão humana dentro do estado — concluída |
| PC-02a | fases em scope `systemd --user` — concluída |
| PC-02b | cotas essenciais — concluída |
| PC-02c | gate systemd real — concluída |
| PC-03 | Telegram opcional, resume e E2E real |

## Critério de conclusão

Um teste ponta a ponta deve provar: execução, mudança, validação, review,
aprovação, verificação do hash e interrupção por sinal/timeout/limite sem
processos sobreviventes. Nenhuma etapa pode acessar uma rede Git.
