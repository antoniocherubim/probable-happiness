# Personal Core v2

Status: Personal Core v2 concluído para uso pessoal.

Esta linha substitui o roadmap de produto público por um runner pequeno para
uso pessoal. A branch `personal-stable` permanece como referência do protótipo
anterior; runs antigos não serão migrados.

## Objetivo

Executar uma task em worktree Git isolada, revisar o resultado, enviar uma
notificação Telegram opcional e preservar o snapshot aprovado para integração
manual.

Fluxo:

1. criar worktree destacada em um commit local;
2. executar Cursor Agent;
3. executar validações configuradas;
4. revisar o diff com Codex;
5. repetir somente dentro do orçamento de iterações autorizado;
6. opcionalmente enviar a conclusão pelo Telegram;
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
- reports são evidência não confiável e recebem bindings por hash quando
  relevantes;
- o hash do snapshot aprovado é imutável e verificado antes da integração
  manual;
- state root, run dirs e artefatos privados usam `0700`/`0600`;
- segredos são allowlisted por fase e redigidos dos artefatos finais;
- fases supervisionadas aceitam somente o protocolo Git local `file`;
  transportes Git remotos falham antes de abrir conexão.

## Modelo de confiança

- computador, usuário local e processos do mesmo UID são confiáveis;
- root, host comprometido e adulteração deliberada pelo mesmo UID estão fora;
- tasks podem falhar, travar ou produzir saída excessiva;
- repositórios deliberadamente hostis não são suportados;
- Telegram é um adaptador opcional; indisponibilidade não pode corromper o run.

## Estado simples

Cada run novo tem um único `state.json` versionado contendo:

- identidade do run, repositório, task, worktree e base commit;
- status atual;
- falha estruturada, quando houver;
- orçamento adicional, quando autorizado;
- estado técnico terminal.

Cursor de iteração, reports, manifestos e diffs continuam em arquivos separados.
Os artefatos relevantes são vinculados por hash. Uma escrita incompleta nunca
substitui o último `state.json` válido; um arquivo publicado antes de uma queda
e ainda não referenciado é órfão descartável.

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

- Telegram para notificação terminal;
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
orçamento deve permanecer pequeno e ser estendido somente por decisão explícita.

## Estado atual

- `state.json` concentra metadata, status, falha e orçamento;
- todas as fases exigem scope `systemd --user` e cotas configuradas;
- Telegram é opcional e nunca executa Git;
- `resume` e `verify` revalidam o snapshot congelado;
- integração e publicação Git permanecem manuais.

## Critério de conclusão

Um teste ponta a ponta deve provar: execução, mudança, validação, review,
aprovação, verificação do hash e interrupção por sinal/timeout/limite sem
processos sobreviventes. Nenhuma etapa pode acessar uma rede Git.

Gate final: **211 testes passaram**, sem skips. O E2E local provou execução,
mudança, validação, review, aceite técnico, `verify`, `resume` e recusa de
transportes Git HTTPS/SSH. Os testes systemd reais provaram limpeza após sucesso,
erro, sinal, timeout e cotas; nenhum scope do runner permaneceu ativo.
