# Plano de execução — DX-06B

> Encerrado no review 8 sem aprovação. A decisão posterior foi remover o push
> automático e executar a DX-06C. Este plano é histórico e não deve ser
> retomado.

## Decisão

Não continuar a execução DX-06 após o `review-12.json`. O run e seu worktree
permanecem preservados como evidência. A repetição de novos mecanismos
executáveis do Git demonstrou que a estratégia de blacklist não converge.

DX-06B substitui essa fronteira por um bare repo de staging criado e configurado
pelo runner, sem carregar config ou `info/*` do projeto.

## Gate 0 — Preservar e importar o WIP com rastreabilidade

Não commitar ou publicar o WIP como se estivesse aprovado.

1. Manter o run original em `BLOCKED/CHANGES_REQUESTED`.
2. Confirmar o base original:
   `4e9c7cadfced3c6e3c19ab24332810e5ab2c3610`.
3. Exportar para `archive/` local e ignorado:
   - `git diff --binary` contra o base;
   - lista e arquivo dos untracked;
   - `review-12.json`, `run.json`, manifesto e hashes;
   - checksums SHA-256 do material exportado.
4. Criar, somente após inspeção do inventário, branch local
   `wip/dx-06-candidate` e commit claramente marcado como
   `WIP/unapproved`; nunca enviar essa branch isoladamente.
5. Levar este plano, a task DX-06B e o profile canônico para a branch WIP.
6. O novo run usa essa branch como base imutável, delivery automático
   desabilitado, e exige revisão cumulativa desde o commit DX-05 acima.

O base commit do novo run vincula o WIP exato. O report final precisa registrar
que revisou também o conteúdo importado. Após `HUMAN_APPROVED`, integrar somente
se a árvore final da branch local for idêntica à árvore aprovada.

## Gate 1 — Remote descriptor

Criar schema versionado e parser puro:

- SSH: host/porta/path separados, sem shell e sem userinfo arbitrário;
- local-test: path real, sob raiz explicitamente permitida;
- HTTPS/unknown/helper: rejeitado em M0;
- branch/ref validada por `git check-ref-format` em contexto limpo;
- serialização sem token, query secreta ou credential material;
- hash do descriptor congelado em `run.json`/delivery job.

Resolver o descriptor antes do executor. Na retomada, nunca reler remote do
projeto; validar somente binding, suporte e disponibilidade externa.

## Gate 2 — Trusted staging repository

Adicionar módulo dedicado, separado de `delivery_git.py`, por exemplo
`trusted_git.py`:

1. criar `<run>/delivery-git/` com `0700`;
2. validar owner, inode e ausência de symlink;
3. criar HOME/XDG/config vazios no run;
4. iniciar bare repo com config allowlisted;
5. apontar object alternates somente ao object dir canônico do projeto;
6. falhar se alternates/replace objects forem necessários;
7. provar por `git config --show-origin` que não há origem do target repo.

O helper recebe tipos fechados (`LocalRead`, `BuildObject`, `RemoteRead`,
`PushExactRef`) em vez de argv/operação livres.

## Gate 3 — Manifest-to-commit

Reusar somente as rotinas no-follow e hashes de conteúdo já revisadas:

- carregar manifesto aprovado;
- comparar base/tree/path/mode/hash;
- ler arquivos por FD, sem filtros;
- `hash-object --stdin` no staging;
- index temporário no staging;
- `write-tree` e `commit-tree` determinísticos;
- revalidar manifesto imediatamente antes da rede.

Remover decisões baseadas em `git diff` executado contra o repo-alvo.

## Gate 4 — Exact push

No staging limpo:

- SSH command constante e construído pelo runner;
- URL explícita derivada do descriptor;
- protocolos default-deny;
- `ls-remote` somente do ref exato;
- push com um refspec exato e opções negativas explícitas;
- refs remotos antes/depois comparados;
- sucesso somente com OID exato;
- nenhum `update-ref` no target repo.

## Gate 5 — Process/systemd boundary

Portar do candidato e revisar:

- bridge-only entrypoint;
- POSIX trampoline e scrub antes do Python;
- supervisor/timeout/categorical errors;
- unit generator e path validation.

Alterar o worker para:

- RW somente no project state;
- target repo/common dir read-only;
- staging no project state;
- mesma implementação no foreground e systemd.

## Gate 6 — Adversarial tests

Usar matriz parametrizada de config executável, não testes isolados copiados:

| Classe | Exemplos |
|---|---|
| Config loading | system/global/local/include, `url.*`, remote config |
| Programs | hooks, helpers, proxy, upload/receive-pack, fsmonitor |
| Content | attributes/info attributes, filters, textconv, external diff |
| Ref expansion | tags, mirror, recurse-submodules, push options |
| Auth | askpass, tty prompt, ssh config, identity files |

Cada caso cria marker inacessível ao código normal e exige:

- marker ausente;
- erro categórico ou sucesso esperado;
- nenhum ref extra;
- nenhum segredo em log/artefato.

Executar e2e systemd real com Git common dir read-only. Skip não fecha M0.

## Gate 7 — Cumulative review and integration

O reviewer final deve:

1. inspecionar `git diff
   4e9c7cadfced3c6e3c19ab24332810e5ab2c3610...HEAD`;
2. listar todos os arquivos importados do WIP;
3. confirmar que blacklist antiga não é a fronteira primária;
4. confirmar config origins do staging;
5. conferir testes completos e e2e systemd;
6. manter DX-06B/M0 abertos se qualquer requisito falhar.

Como o profile inicial mantém delivery desabilitado, a primeira integração é
manual após `HUMAN_APPROVED`, comparando a árvore aprovada com a árvore a
integrar. Habilitar dogfooding de `push_branch` somente numa task posterior.

## Arquivos esperados

| Ação | Área |
|---|---|
| Novo | trusted staging/context + remote descriptor/schema |
| Refatorar | delivery, snapshot/approval Git calls, worker |
| Portar | launcher, bridge-only, units, doctor, timeout |
| Substituir | blacklist/argv livre do executor Git |
| Testar | matriz adversarial + systemd e2e |
| Documentar | task, roadmap, operação, limitações, rollback |

## Stop conditions

Parar e retornar `BLOCKED`, sem ampliar silenciosamente o escopo, se:

- o staging ainda precisar carregar config/info do target;
- for necessário escrever no target Git common dir;
- autenticação exigir helper/segredo não previsto;
- o repo usar alternates/replace objects não suportados;
- o e2e systemd obrigatório não puder ser executado;
- o parecer não cobrir o diff cumulativo.
