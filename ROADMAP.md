# Roadmap para uso confiável por terceiros

Atualizado em 2026-07-25.

Estado atual: **pré-alpha**. O fluxo principal funciona e possui boa cobertura
local, mas ainda não deve ser oferecido como ferramenta confiável para terceiros
sem os bloqueadores P0 e P1 abaixo.

Este roadmap ordena o trabalho por dependência e risco. Datas devem ser definidas
somente depois que cada marco anterior cumprir seus critérios de saída.

## Objetivo de produto

Entregar uma ferramenta instalável e atualizável que execute Cursor e Codex em
worktrees isolados e mantenha aprovação humana vinculada ao snapshot revisado.
Commit, integração e push permanecem ações manuais fora do runner estável.

“Confiável para terceiros” significa:

- nenhuma operação Git remota ocorre pelo fluxo suportado;
- falhas, reinícios e concorrência terminam em sucesso comprovado ou falha fechada;
- tokens e credenciais não chegam a processos que não precisam deles;
- instalação, diagnóstico, atualização e remoção são reproduzíveis;
- versões suportadas e limites de segurança são explícitos;
- documentação, testes e artefatos de release permitem auditoria independente.

## Fundação existente

- [x] DX-01: gate humano autenticado pelo Telegram e vínculo ao hash revisado;
- [x] AG-01: ferramenta, target e state roots separados;
- [x] DX-02: perfil por projeto, bootstrap, timeout, heartbeat e retomada;
- [x] DX-03: resumo técnico e documentação obrigatória; o delivery opt-in foi
  posteriormente removido;
- [x] DX-04: extensão explícita e auditável do orçamento de iterações;
- [x] DX-05: experimento de fila/worker preservado no histórico e retirado da
  superfície estável;
- [x] DX-06C: delivery remoto removido e integração Git tornada manual;
- [x] DX-07: máquina de estados tipada com CAS, lock e writers centralizados
  (implementada; aguardando revisão formal);
- [ ] DX-08: persistência segura — candidato WIP após review 3; correções
  fatiadas em [DX-08A](docs/tasks/DX-08A.md) (autoridade/locks/I/O; implementada,
  aguarda revisão), [DX-08A1](docs/tasks/DX-08A1.md) (envelope Cursor privado na
  extensão de iterações; implementada, aguarda revisão), [DX-08A2](docs/tasks/DX-08A2.md)
  (status/heartbeat em diretório seguro; planejada) e
  [DX-08B](docs/tasks/DX-08B.md) (migration/backup/rollback; planejada). Não
  tratar DX-08/M1 como fechados até as fatias + revisão formal;
- [x] suíte local com testes determinísticos;
- [x] aprovação local não cria commit, branch ou job de rede.

## Ordem de entrega

| Marco | Prioridade | Resultado | Depende de |
|---|---|---|---|
| M0 | P0 | Fronteira segura entre Telegram e Git | fundação atual |
| M1 | P0 | Máquina de estados e persistência resistentes a falhas | M0 |
| M2 | P0 | Isolamento e limites reais de recursos/segredos | M1 |
| M3 | P1 | Bridge e outbox operacionalmente confiáveis | M0–M2 |
| M4 | P1 | Pacote, compatibilidade e upgrades suportados | M1 |
| M5 | P1 | CI e verificação contínua de segurança | M0–M4 |
| M6 | P1 | Operação e documentação para terceiros | M4–M5 |
| M7 | Gate | Alpha externa, beta pública e release estável | M0–M6 |

Próxima entrega recomendada: **DX-08A2** (status/heartbeat em diretório
canônico privado), depois **DX-08B** (migration/backup/rollback), depois
revisão formal de DX-07/DX-08A/DX-08A1/DX-08A2/DX-08B antes de avançar para
DX-09 / M2. DX-08A1 provou localmente que `authorize_iteration_extension`
aceita somente `cursor-<iteration>.json` com modo **exatamente** `0600`
(owner esperado, arquivo regular sem symlink/hard link), lido sem seguir
links e válido pelo mesmo `validate_executor_envelope` de
`prepare_review_artifacts`/resume — JSON malformado/truncado/vazio, fixture
`{"summary":…}`, campos extras/ausentes/tipos inválidos, symlink, hard link,
owner inesperado e modos `0644`/`0666`/`0400`/`0500`/`0700` recusam sem mutar
budget/status/audit/journal nem o diretório (com `.resume.lock` pré-presente);
envelope real de `cursor --output-format json` ainda autoriza. DX-08A (base) provou que a API
pública não promove estado crítico sem artefato vinculado com contrato
semântico, replay no estado-destino com binding contrato-válido, resume
EXECUTING/REVIEWING com envelope Cursor real, `review-status`/
`prepare_review_artifacts` com `require_private=True`, e locks que recusam
symlink intermediário/inode pós-flock (461 passed na suíte unitária); M1
permanece aberto até DX-08A2/DX-08B e revisão formal.

### Tasks preparadas até M2

| Ordem | Marco | Task | Resultado |
|---|---|---|---|
| 1 | M0 | [DX-05](docs/tasks/DX-05.md) | experimento histórico de fila/worker |
| 2 | M0 | [DX-06](docs/tasks/DX-06.md) | candidata de hardening não aprovada |
| 3 | M0 | [DX-06B](docs/tasks/DX-06B.md) | experimento de staging/push não aprovado |
| 4 | M0 | [DX-06C](docs/tasks/DX-06C.md) | concluída: aprovação local terminal |
| 5 | M1 | [DX-07](docs/tasks/DX-07.md) | implementada; aguarda revisão formal |
| 6 | M1 | [DX-08](docs/tasks/DX-08.md) | candidata; review 3 abriu DX-08A/DX-08B |
| 6a | M1 | [DX-08A](docs/tasks/DX-08A.md) | implementada; aguarda revisão formal |
| 6a1 | M1 | [DX-08A1](docs/tasks/DX-08A1.md) | implementada; envelope Cursor na extensão |
| 6a2 | M1 | [DX-08A2](docs/tasks/DX-08A2.md) | planejada: status/heartbeat diretório seguro |
| 6b | M1 | [DX-08B](docs/tasks/DX-08B.md) | planejada: migration/backup/rollback |
| 7 | M2 | [DX-09](docs/tasks/DX-09.md) | cgroups e limites de recursos/saída |
| 8 | M2 | [DX-10](docs/tasks/DX-10.md) | segredos por fase, streaming e retenção segura |

## M0 — Aprovação local com menor privilégio

Resultado: o processo que conhece o token Telegram apenas registra decisões; o
runner não possui caminho suportado de commit ou push automático.

Tasks: [DX-05](docs/tasks/DX-05.md), [DX-06](docs/tasks/DX-06.md) e
[DX-06B](docs/tasks/DX-06B.md) como histórico, concluído por
[DX-06C](docs/tasks/DX-06C.md).

### Trabalho

- [x] responder ao callback imediatamente, sem aguardar Git ou rede;
- [x] não importar módulos de delivery na bridge;
- [x] aceitar somente `delivery.mode = "none"`;
- [x] remover `delivery-worker`, jobs e comandos internos de push;
- [x] terminar aprovação válida em `HUMAN_APPROVED`;
- [x] preservar worktree e validar novamente seu hash com `agent-loop verify`;
- [x] documentar integração, commit e push como ações manuais;
- [x] concluir revisão formal da DX-06C.

### Critérios de saída

- bridge e aprovação não importam módulos Git/delivery;
- nenhum profile aceito consegue habilitar push;
- callback não cria `delivery-job.json`, commit ou branch;
- `resume` de aprovação apenas verifica o snapshot, sem acesso remoto;
- documentação e ajuda da CLI não oferecem worker ou entrega automática.

## M1 — Centralizar estado e tornar persistência recuperável

Resultado: toda transição é válida, condicionada, auditável e recuperável após
queda abrupta.

Tasks: [DX-07](docs/tasks/DX-07.md), [DX-08](docs/tasks/DX-08.md),
[DX-08A](docs/tasks/DX-08A.md), [DX-08A1](docs/tasks/DX-08A1.md),
[DX-08A2](docs/tasks/DX-08A2.md) e [DX-08B](docs/tasks/DX-08B.md).

### Trabalho

- [x] definir enum e tabela única de transições permitidas;
- [x] trocar escritas diretas de status por compare-and-set sob lock;
- [ ] exigir estado aprovado válido antes de qualquer integração futura;
- [x] entradas inválidas devem falhar sem sobrescrever o estado anterior;
- [x] centralizar leitura/escrita segura com `O_NOFOLLOW`, arquivo regular,
  owner e modo (`require_private=True` nos caminhos de produção; inspect/migrate
  legados podem relaxar explicitamente; containment não resolve symlinks);
- [x] usar `umask 077`, diretórios `0700` e arquivos sensíveis `0600`;
- [x] executar `fsync` do arquivo e do diretório após replace/link; aplicar modo
  e re-fsync do arquivo antes de publicar (DX-08A);
- [x] recusar eventos críticos artifactless; somente `LogicalTransaction` com
  binding (`event`↔`status_event` + contrato semântico do artefato, incluindo
  request/decision) aplica status via helper privado — sem flag pública
  spoofável (DX-08A);
- [x] locks inseguros falham sem `chmod`/replace; lock novo `0600` + dir fsync;
  owner/dir/inode-swap cobertos em teste (DX-08A);
- [x] vincular `authorized_at`, `updated_at` e cada entrada do ledger à cadeia
  de integridade;
- [x] escolher e documentar o modelo de ameaça:
  - baseline: processos com o mesmo UID são confiáveis;
  - hardened: não anunciado nesta release (exige chave inacessível ao executor);
- [ ] criar migrations versionadas para run, profile, approval e ledger com
  preflight/backup/rollback sem mutação indevida (DX-08B; candidata DX-08 ainda
  não é confiável para migration);
- [x] avaliar SQLite/WAL vs JSON+journal (ADR-001 escolheu JSON+journal).

### Critérios de saída

- nenhuma API pública consegue promover um run `BLOCKED` ou não aprovado sem o
  artefato vinculado (provado em DX-08A, incluindo recusa de bypass spoofado,
  mismatch event/status, matriz semântica decision/request e matriz de replay
  no estado-destino com binding ausente/corrupt/future/mismatched; suíte local
  458 passed);
- fault injection cobre fronteiras da API comum (write completo/short-write/
  fsync/chmod/segundo fsync/replace/link/dir fsync); matriz exaustiva por
  **cada** evento DX-07 ainda é risco residual;
- qualquer arquivo truncado, symlink, modo/owner incorreto ou schema futuro falha
  fechado e com diagnóstico acionável (resume com envelope Cursor Agent real,
  snapshot/report zero-byte, `review-status`/`prepare_review_artifacts` com
  modo 0644/0666 e contratos malformados de reviewer/executor sem republicar
  manifest/summary, task symlinkada e validation result future-schema,
  technical summary, evidence sem publicar blob antes do manifesto, validation
  logs/results symlinkados, reap do supervisor em falha de heartbeat/status
  em DX-08A, e `authorize_iteration_extension` só com envelope Cursor privado
  válido em DX-08A1; migration ainda depende de DX-08B);
- runs das duas versões persistidas anteriores migram ou recusam retomada sem
  mutação (pendente DX-08B);
- o baseline declara o mesmo UID como fronteira de confiança; se um modo
  hardened for anunciado, ele demonstra que o executor não consegue forjar
  aprovação ou extensão.

M1 permanece **aberto**: DX-07, DX-08A e DX-08A1 implementadas com evidência
local; DX-08A2, DX-08B e revisão formal ainda pendentes. Não declarar o marco
concluído.

## M2 — Limitar processos, recursos e exposição de segredos

Resultado: timeout significa encerramento real, e uma task não pode esgotar o
host ou acessar credenciais desnecessárias.

Tasks: [DX-09](docs/tasks/DX-09.md) e [DX-10](docs/tasks/DX-10.md).

### Trabalho

- [ ] executar fases em cgroups/systemd scopes transitórios;
- [ ] impor `MemoryMax`, `TasksMax`, CPU, tempo total e limite de arquivos abertos;
- [ ] limitar stdout/stderr por fase e truncar de forma explícita e auditável;
- [ ] gravar temporários brutos com `0600` e removê-los na recuperação;
- [ ] impor limite por arquivo, snapshot e total do diff;
- [ ] calcular hashes, diffs e blobs por streaming, sem carregar tudo em memória;
- [ ] separar ambiente requerido por bootstrap, executor e validação;
- [ ] bloquear rede do executor por fronteira verificável e impedir acesso a
  credenciais Git/SSH do operador;
- [ ] suportar credenciais efêmeras e documentar sua rotação;
- [ ] verificar que nenhum descendente permanece após timeout/cancelamento;
- [ ] definir política de espaço em disco e retenção de worktrees/runs.

### Critérios de saída

- fork bomb, processo com nova sessão, saída infinita e arquivo gigante são
  contidos em testes;
- um executor não recebe credenciais exclusivas das validações;
- OOM, disco cheio e timeout deixam run retomável ou terminalmente bloqueado;
- nenhum arquivo bruto sensível fica legível por outro usuário local.

## M3 — Tornar bridge e outbox previsíveis

Resultado: reinícios e concorrência podem duplicar uma notificação, mas nunca uma
decisão ou ação; duplicatas são raras, identificáveis e recuperáveis.

### Trabalho

- [ ] impor singleton por state root com lock global;
- [ ] persistir e rotacionar o último `update_id` processado;
- [ ] adicionar claim/lease durável por item do outbox;
- [ ] registrar chave idempotente visível por notificação e chunk;
- [ ] definir explicitamente semântica *at-least-once* da Bot API;
- [ ] responder callbacks repetidos com o estado final real;
- [ ] indexar jobs pendentes sem varrer todos os runs a cada ciclo;
- [ ] adicionar retry com backoff, jitter e limite;
- [ ] criar retenção/arquivamento de updates, outbox e runs concluídos.

### Critérios de saída

- duas instâncias não enviam o mesmo outbox simultaneamente;
- restart não reprocessa backlog já confirmado;
- crash antes/depois de cada chamada Telegram preserva segurança e recuperabilidade;
- teste de carga cobre milhares de runs arquivados e pendentes.

## M4 — Empacotar, versionar e suportar upgrades

Resultado: terceiros instalam uma versão identificável sem clonar o repositório.

### Trabalho

- [ ] criar `pyproject.toml` e entrypoint `agent-loop`;
- [ ] mover templates e schemas para package data;
- [ ] adotar SemVer e expor `agent-loop --version`;
- [ ] definir matriz suportada de Python, Git, Linux e systemd;
- [ ] fixar dependências de desenvolvimento e automatizar atualização;
- [ ] adicionar `agent-loop init`, `doctor`, `status` e diagnóstico redigido;
- [ ] criar instalação/desinstalação segura das unidades de usuário;
- [ ] testar instalação por wheel e ambiente virtual limpo;
- [ ] criar migrations e política de compatibilidade/depreciação;
- [ ] publicar checksums, SBOM e artefatos assinados.

### Critérios de saída

- wheel instala e executa sem caminhos relativos ao checkout;
- `doctor` detecta versões, autenticação, permissões, hooks, state root e systemd;
- upgrade e rollback preservam runs suportados;
- desinstalação não remove runs/worktrees sem confirmação explícita.

## M5 — CI e qualidade contínua

Resultado: cada mudança prova que não enfraquece os gates de segurança.

### Trabalho

- [ ] criar CI para a matriz suportada;
- [ ] executar `pytest`, `bash -n`, `git diff --check`, lint e type checking;
- [ ] medir cobertura, com meta mínima de 90% nos módulos críticos de estado,
  aprovação e bridge;
- [ ] executar testes de integração Git local com worktrees;
- [ ] executar systemd sandbox em ambiente Linux compatível;
- [ ] adicionar property-based tests para máquina de estados e schemas;
- [ ] adicionar fault injection em todas as escritas e chamadas externas;
- [ ] testar concorrência real com processos, não apenas threads/fakes;
- [ ] adicionar análise de dependências, segredos e vulnerabilidades;
- [ ] bloquear merge quando um gate obrigatório falhar.

### Critérios de saída

- CI reproduz toda a suíte versionada e os novos cenários em ambiente limpo;
- cada bug de segurança recebe teste de regressão;
- nenhuma mudança de schema entra sem migration e teste de upgrade;
- release é criada somente a partir de commit aprovado por todos os gates.

## M6 — Documentação, governança e suporte

Resultado: uma pessoa sem contexto interno consegue avaliar, instalar, operar e
recuperar a ferramenta com segurança.

### Trabalho

- [ ] escolher e adicionar licença;
- [ ] criar `SECURITY.md`, política de divulgação e versões com suporte;
- [ ] criar `CONTRIBUTING.md`, código de conduta e template de issues;
- [ ] manter `CHANGELOG.md` e notas de upgrade/rollback;
- [ ] publicar quickstart do primeiro run até aprovação e integração manual;
- [ ] documentar threat model, trust boundaries e diferenças baseline/hardened;
- [ ] criar runbook para timeout, corrupção, drift e disco cheio;
- [ ] fornecer repositório-exemplo mínimo sem credenciais reais;
- [ ] documentar backup, retenção, limpeza e recuperação do state root;
- [ ] documentar claramente que Telegram não é terminal remoto;
- [ ] definir canal e expectativa de suporte.

### Critérios de saída

- instalação limpa é concluída seguindo apenas a documentação;
- operador recupera cenários de falha exercitados sem editar JSON manualmente;
- todos os exemplos são executados na CI;
- auditor externo consegue identificar o que é garantido e o que permanece fora
  do modelo de ameaça.

## M7 — Gates de release

### Alpha externa

- [ ] M0, M1 e M2 concluídos;
- [ ] usada em pelo menos três repositórios com perfis distintos;
- [ ] no mínimo 30 runs completos, incluindo retomadas e falhas induzidas;
- [ ] nenhum push não aprovado e nenhum vazamento de segredo conhecido;
- [ ] instalação ainda pode exigir suporte direto do mantenedor.

### Beta pública

- [ ] M3, M4, M5 e documentação essencial de M6 concluídos;
- [ ] wheel e unidades publicadas como artefatos versionados;
- [ ] upgrade a partir da versão anterior testado;
- [ ] dois ciclos de release sem incidente P0;
- [ ] issues conhecidas classificadas e limites publicados.

### Release estável 1.0

- [ ] todos os critérios de M0–M6 concluídos;
- [ ] nenhum P0/P1 aberto;
- [ ] auditoria de segurança focada em state machine, subprocessos, Git e segredos;
- [ ] recuperação validada para crash, reboot, rede, disco cheio e corrupção;
- [ ] política de suporte, compatibilidade e vulnerabilidades publicada;
- [ ] release reproduzível, assinada e acompanhada de SBOM.

## Fora do escopo da versão 1.0

- merge automático, push na branch base, force-push ou tags;
- criação ou aprovação automática de pull request;
- deploy ou início automático da próxima task;
- terminal, shell ou controle genérico do host via Telegram;
- proteção criptográfica contra administrador/root do host;
- suporte oficial a Windows ou macOS sem um backend de isolamento equivalente.

## Política de manutenção deste roadmap

- cada item deve apontar para uma issue/task com critério de aceite e teste;
- um checkbox só é concluído com evidência reproduzível;
- novos riscos P0/P1 entram antes de funcionalidades de conveniência;
- mudanças no threat model, formatos persistidos ou suporte de versões exigem
  atualização simultânea deste arquivo, README e documentação operacional.
