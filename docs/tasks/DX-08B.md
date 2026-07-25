# DX-08B — Preflight, backup, rollback e migrations sem mutação indevida

Status: planejada.

Marco: M1, correção 2 de 2 da DX-08.

Depende de: [DX-08A](DX-08A.md).

Próxima task: [DX-09](DX-09.md).

## Contexto

Depois da DX-08A, a fronteira de eventos, locks e I/O comum estará segura. O
review 3 da DX-08 ainda exige correções de migration: schemas futuros de
artefatos causam mutação parcial, rollback pode apagar diretórios preexistentes
e arquivos de lock entram no backup/restauração enquanto seus inodes estão
flockados.

## Objetivo

Fazer migration, backup e rollback falharem sem mutação quando a entrada não for
suportada, preservarem exatamente o estado preexistente e nunca substituírem um
pathname de lock ativo.

## Escopo obrigatório

### 1. Preflight read-only completo

Antes de criar lock, backup, manifesto ou alterar qualquer entrada, inspecionar
todos os artefatos persistentes reconhecidos:

- `run.json` e profile congelado;
- request, decision, rejection e outbox;
- iteration budget;
- audit trail e transaction journal;
- snapshots/manifests com schema versionado;
- artefatos históricos declarados como suportados.

Schema futuro ou documento inseguro deve deixar nomes, bytes, modos, inodes e
mtimes observáveis inalterados. O preflight não pode normalizar
`profile_schema`, permissões ou defaults.

### 2. Inventário completo do backup

O manifesto deve registrar arquivos e diretórios preexistentes, inclusive
árvore aninhada, tipos, modos e hashes de arquivos. Evidence e outros
subdiretórios válidos precisam sobreviver ao round-trip.

Excluir explicitamente do conteúdo restaurável:

- pathnames de locks ativos;
- temporários validados;
- o próprio diretório/manifesto de backup quando recursivo.

O archive deve ter hash autenticado pelo manifesto e ser verificado antes de
qualquer restore.

### 3. Rollback exato e seguro

Rollback deve:

- recusar quando houve evento novo depois do backup;
- verificar archive e inventário antes de alterar o run;
- restaurar conteúdo e modos;
- remover somente entradas comprovadamente criadas pela migration;
- preservar diretórios/arquivos preexistentes;
- validar novamente hashes e modos restaurados;
- nunca unlinkar, substituir ou restaurar `.state.lock`, `.migration.lock`,
  `.approval.lock`, `.resume.lock` ou outro lock ativo.

Falha de validação deixa o run inspecionável e não anuncia `rolled_back`.

### 4. Concorrência

Migration mantém ordem de locks compatível com DX-08A e falha/retry de forma
determinística sob contenção. Testes multiprocess não podem depender de qual
processo vence uma tentativa nonblocking: devem coordenar ou fazer retry
limitado somente para erros de contenção.

### 5. Compatibilidade real

Fixtures DX-01 a DX-07 devem representar formatos históricos relevantes, não o
mesmo `run.json` com apenas o nome da task alterado. Migration repetida não soma
budget, não promove estado e não inventa decisão, audit ou remote OID.

### 6. Documentação e fechamento

Atualizar runbook, migration notes, DX-08 e `ROADMAP.md` de acordo com os testes.
M1 só pode ser marcado concluído quando DX-08A e DX-08B estiverem aprovadas e a
suíte obrigatória estiver verde.

## Fora de escopo

- migration de schema futuro desconhecido;
- proteção contra root/administrador;
- delivery remoto;
- cgroups, cotas e retenção.

## Critérios de aceite

1. Todo schema futuro suportado pelo preflight é recusado sem qualquer mutação.
2. Backup é verificado e cobre a árvore preexistente relevante.
3. Rollback preserva nested evidence, conteúdo, modos e locks ativos.
4. Entradas criadas pela migration são removidas somente quando comprovadas.
5. Fixtures históricas e iteration budget migram idempotentemente.
6. Dry-run permanece byte-a-byte e metadata-a-metadata read-only.
7. Documentação descreve exatamente as garantias demonstradas.

## Testes obrigatórios

- schema futuro em cada família de artefato, com snapshot integral antes/depois;
- dry-run com diretório sem locks prévios;
- backup adulterado, manifesto adulterado e restore hash mismatch;
- round-trip com nested evidence, diretórios vazios, modos e arquivos extras;
- locks ativos durante migration/rollback, provando inode/pathname estáveis;
- crash/fault injection antes/depois de backup, manifesto, restore e dir-fsync;
- fixtures históricas distintas DX-01 a DX-07;
- corrida multiprocess repetida sem flakiness;
- suíte completa, compileall, `bash -n` e `git diff --check`.

## Entrega

- código de migration/backup/rollback;
- fixtures históricas;
- matriz de preflight e crash points;
- `docs/BACKUP_RECOVERY.md`, `MIGRATION_NOTES.md`, DX-08 e roadmap alinhados;
- evidência reproduzível dos testes.
