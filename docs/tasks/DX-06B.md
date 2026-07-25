# DX-06B — Contexto Git confiável e delivery por allowlist positiva

Status: experimento encerrado; candidata do review 8 preservada e não aprovada.

Substituída por: [DX-06C](DX-06C.md), que remove push automático do produto
estável. Este documento permanece como registro do experimento de staging Git e
não descreve uma feature disponível.

Marco: M0, parte 3 de 3.
Depende de: [DX-06](DX-06.md).
Próxima task: [DX-07](DX-07.md).

## Contexto

A implementação candidata da DX-06 separou bridge e worker, removeu segredos
do ambiente do worker, adicionou timeout, launcher pré-Python, unidades systemd
e amplo hardening Git. Ela não foi aprovada nem integrada.

Revisões adversariais demonstraram que bloquear opções do Git uma a uma não é
uma fronteira convergente. Configuração controlada pelo repositório ainda podia
selecionar programas por mecanismos como:

- `remote.<name>.uploadpack` e `remote.<name>.receivepack`;
- protocolos/helpers remotos, proxy e reescrita de URL;
- filtros escolhidos por `.git/info/attributes`;
- opções de push capazes de publicar refs adicionais.

O problema arquitetural é carregar o Git common dir e a configuração local do
projeto dentro do processo que possui rede, SSH agent e autoridade de push.
Esta task substitui a blacklist crescente por um contexto Git novo, mínimo e
controlado pelo runner.

M0 continua aberto. Nenhuma documentação pode declarar DX-06, DX-06B ou M0
concluídos antes de todos os gates desta task.

## Modelo de ameaça

Depois que o executor começa, tratar como não confiáveis:

- worktree, `.git` file e Git common dir do projeto;
- `.git/config`, includes, refs, hooks e `info/*`;
- `.gitattributes`, `.gitignore`, submodules e configuração de remote;
- qualquer comando, caminho, protocolo ou ref derivado desses arquivos.

Permanecem confiáveis somente:

- código versionado do runner no commit-base;
- perfil congelado e metadata do run;
- decisão e manifesto aprovados;
- diretórios criados pelo runner com owner/mode verificados;
- descritor de remote congelado antes do executor e sem credencial embutida;
- `SSH_AUTH_SOCK` e known-hosts explicitamente aprovados pelo operador.

Processos com o mesmo UID e acesso irrestrito ao state root permanecem fora do
modelo de proteção até o isolamento por identidade previsto em M1/M2.

## Objetivo

Construir e publicar o commit aprovado sem carregar configuração executável do
repositório-alvo:

```text
worktree/common dir não confiáveis (somente leitura de bytes/objetos)
  → manifesto aprovado
  → repositório bare temporário confiável no project state
  → tree/commit determinísticos
  → push por descritor explícito e allowlisted
  → confirmação do único ref remoto aprovado
```

O worker não deve precisar escrever no Git common dir do projeto e não deve
criar branch local nele.

## Escopo obrigatório

### 1. Descritor de remote congelado

Na criação do run, antes do executor:

- resolver exatamente um push target;
- aceitar inicialmente somente SSH sem userinfo secreto e paths locais
  canônicos usados por testes;
- rejeitar `ext::`, `git://`, helpers desconhecidos, URL rewrite, proxy
  executável, múltiplas push URLs e credencial embutida;
- persistir um descritor normalizado, seguro para logs, ligado por hash ao run;
- congelar host, porta, repositório, base branch e branch de entrega;
- validar known-hosts e disponibilidade do SSH agent sem imprimir valores.

HTTPS autenticado e credential helpers ficam fora de M0. Sua inclusão futura
exige política de credenciais separada e teste equivalente.

### 2. Repositório de staging confiável

Para cada delivery, criar sob o project state um bare repo dedicado:

- diretório `0700`, arquivos/config `0600`, sem symlink;
- `HOME` e XDG privados e vazios;
- sem config system/global e sem leitura do config local do projeto;
- config construída integralmente pelo runner a partir de allowlist;
- sem hooks, signing, credential helper, fsmonitor, filters, attributes,
  external diff, proxy, remote helper, submodule recursion ou prompt;
- objeto-base acessível somente por object directory canônico/alternate
  controlado pelo runner.

Repos cujo object database use alternates, replace objects ou layout não
suportado devem falhar fechados no preflight nesta versão.

Nenhum comando de staging, hash, manifesto, `ls-remote` ou push pode usar
`git -C <repo-alvo>` nem carregar o Git common dir como `--git-dir`.

### 3. Snapshot e commit sem filtros do projeto

- listar o tree-base a partir do staging repo limpo;
- ler bytes do worktree com as proteções no-follow já existentes;
- usar o manifesto revisado como fonte exata de paths, modos e hashes;
- gravar blobs via stdin, sem path/filter;
- construir tree e commit no object database de staging;
- fixar parent, identidade, timestamps e mensagem já congelados;
- verificar novamente manifesto e diff hash imediatamente antes do push.

Não usar `git diff` contra o Git dir do projeto para uma decisão de segurança.
Se Git for usado para comparação, deve operar apenas no staging repo limpo.

### 4. Push de um único ref

- invocar a URL/descritor explícito, nunca um nome de remote do projeto;
- allowlist fechada de protocolo;
- SSH com `BatchMode`, sem config do projeto, askpass, senha, identity file ou
  comando selecionado pelo repositório;
- refspec exato `<commit>:refs/heads/<branch>`;
- desabilitar follow-tags, mirror, atomic/ref expansion e recurse-submodules;
- capturar refs remotos antes/depois e provar que somente o ref aprovado mudou;
- confirmar que o OID remoto é exatamente o commit construído;
- replay com o mesmo OID é idempotente; qualquer outro OID falha fechado.

O fluxo não cria nem atualiza refs locais no repositório-alvo.

### 5. Worker e systemd

Preservar da DX-06, após nova revisão:

- entrypoint bridge-only sem imports de delivery/Git;
- launcher POSIX + `env -i` antes do Python;
- ambiente do worker sem Telegram/projeto/executor;
- supervisor com timeout e encerramento do process group;
- erros públicos categóricos, sem stderr arbitrário;
- unidade worker por projeto.

Como staging e objetos novos ficam no project state, reduzir
`ReadWritePaths=` ao project state. Repo/worktree/common dir devem ser somente
leitura. O e2e precisa provar write-denial no Git common dir.

### 6. Doctor e auditoria

`doctor-delivery` deve reportar apenas nomes/estado:

- transport aceito/rejeitado e descritor congelável;
- object directory, alternates/replace objects e compatibilidade;
- SSH agent e known-hosts disponíveis;
- staging/state owner e permissões;
- configuração perigosa do projeto que será completamente ignorada;
- ausência de segredos no ambiente efetivo do worker.

Adicionar modo de auditoria que liste as origens da configuração efetiva do
staging repo e prove que nenhuma origem pertence ao repositório-alvo.

### 7. Migração do candidato DX-06

O candidato preservado é referência não confiável, não código aprovado.

- portar seletivamente launcher, unidades, bridge-only, supervisor, doctor e
  testes úteis;
- substituir, não ampliar, a blacklist de `delivery_git.py`;
- descartar claims de conclusão e contagens frágeis de testes;
- não aplicar patch ou commit WIP sem inspeção cumulativa;
- revisar o diff completo desde o commit-base DX-05
  `4e9c7cadfced3c6e3c19ab24332810e5ab2c3610`, inclusive o WIP importado.

O parecer final deve declarar explicitamente que inspecionou o diff cumulativo,
não apenas a última iteração.

## Invariantes de segurança

- bridge com token não importa nem executa delivery/Git;
- worker recebe somente ambiente allowlisted antes do primeiro interpretador;
- nenhum programa ou argumento executável deriva da configuração do projeto;
- nenhum Git de delivery carrega config, hooks ou `info/*` do projeto;
- worktree e Git common dir do projeto permanecem read-only para o worker;
- somente o commit e `refs/heads/<branch>` aprovados alcançam o remote;
- timeout/falha nunca promove `PUSHED`;
- stderr remoto não entra em artefatos ou Telegram;
- remote descriptor, manifesto, decisão, commit e OID permanecem vinculados;
- ausência de suporte resulta em falha fechada, não fallback para o Git antigo.

## Compatibilidade e rollback

- runs antigos sem descritor confiável não são migrados implicitamente;
- delivery legado deve ser desabilitado ou exigir migração explícita;
- rollback remove a unidade/staging sem remover jobs, decisões ou worktrees;
- falha deixa staging e evidência suficientes para diagnóstico seguro;
- sistemas sem systemd podem usar worker foreground com a mesma fronteira;
- não criar branch local é uma mudança documentada de comportamento.

## Fora de escopo

- HTTPS autenticado e credential helpers;
- repositórios com alternates/replace objects não suportados;
- merge, PR, tag, deploy ou atualização da base branch;
- cgroups gerais de Cursor/Codex (DX-09);
- isolamento por outro UID (M1/M2);
- outbox/claim avançado (M3).

## Critérios de aceite

1. Nenhum comando Git do delivery lê `.git/config` ou `.git/info/*` do alvo.
2. Markers em todos os mecanismos executáveis conhecidos nunca são acionados.
3. Worker conclui delivery com Git common dir montado read-only.
4. Remote recebe exatamente uma branch e o OID aprovado.
5. Remote/config/protocolo não suportado falha antes de abrir rede.
6. Bridge e worker mantêm isolamento de token/processo da DX-06.
7. Foreground e unidade systemd usam o mesmo staging e políticas.
8. Teste systemd real executa sem skip no gate M0.
9. Parecer final cobre o diff cumulativo desde o commit DX-05 indicado acima.
10. M0 permanece aberto se qualquer gate obrigatório estiver skipped.

## Testes obrigatórios

- config system/global/local, includes e `url.*.insteadOf/pushInsteadOf`;
- `uploadpack`, `receivepack`, remote helpers, `ext::`, `gitProxy`;
- hooks, fsmonitor, aliases, pager/editor, signing e credential helpers;
- `.gitattributes`, `.git/info/attributes`, `core.attributesFile`,
  clean/smudge/process, textconv e external diff;
- follow-tags, mirror, push options, recurse-submodules e refs extras;
- SSH prompt/askpass/config/ProxyCommand/identity file;
- target Git dir, worktree e home sem permissão de escrita;
- paths com espaços, symlinks e troca de inode;
- timeout de staging, `ls-remote` e push com descendentes;
- idempotência, remote divergente e confirmação do OID;
- inspeção de config origins do staging;
- bridge/worker com segredos fake e captura somente de nomes;
- suíte completa, `git diff --check`, shell syntax, compileall,
  `systemd-analyze verify` e e2e systemd real.

## Entrega obrigatória

- implementação do contexto/staging Git confiável;
- schema e documentação do remote descriptor;
- migração explícita para runs antigos;
- doctor/preflight e evidência de config origins;
- unidades/launcher revisados;
- testes adversariais e e2e real;
- documentação operacional, limitações e rollback;
- relatório cumulativo do candidato importado.
