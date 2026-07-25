# DX-08A — Autoridade de eventos, locks e I/O persistente segura

Status: planejada.

Marco: M1, correção 1 de 2 da DX-08.

Base: candidato WIP preservado após o review 3 da DX-08.

Próxima task: [DX-08B](DX-08B.md).

## Contexto

O candidato DX-08 introduziu persistência segura, journal e audit trail, mas o
review confirmou que a API pública ainda consegue promover eventos críticos sem
seus artefatos. Também encontrou locks que corrigem arquivos inseguros antes de
validá-los, durabilidade incompleta do modo e acessos persistentes fora da API
comum.

Esta task corrige apenas a fronteira de autoridade, locks, durabilidade básica e
I/O comum. Migration, backup e rollback ficam na DX-08B.

## Objetivo

Garantir que nenhuma API pública invente aprovação ou outro estado crítico, e
que locks e arquivos persistentes sejam abertos, validados e publicados sem
seguir links, reparar entradas inseguras ou perder metadados após crash.

## Escopo obrigatório

### 1. Eventos críticos exigem transação

Definir uma única classificação dos eventos que vinculam estado a artefatos:

- `run_blocked` + `failure.json`;
- `approval_requested` + `human_approval_request.json`;
- `human_approved` e `recover_human_approved` +
  `human_approval_decision.json`;
- `human_rejected` + `human_rejection.json`;
- `iteration_budget_extended` + `iteration-budget.json`.

`transition_run()` e qualquer API equivalente sem artefatos devem recusar esses
eventos antes de escrever status ou audit. Somente `LogicalTransaction`, com o
binding completo validado, pode publicá-los.

O replay idempotente também precisa comprovar o artefato vinculado; estar no
estado-destino não basta.

### 2. Locks falham sem mutação

Antes de `flock`, validar o diretório e o lock por `lstat`/open
`O_NOFOLLOW`/`fstat`:

- diretório canônico, owner esperado e modo privado;
- lock regular, owner esperado, modo `0600`, `nlink == 1`;
- inode aberto igual ao inspecionado;
- nenhuma troca de inode, hard link ou symlink.

Um lock existente em `0644`, com owner incorreto, hard-linked ou inseguro deve
ser recusado sem `chmod`, replace ou outra mutação. Lock novo deve ser criado
em `0600` e ter a criação persistida com `fsync` do diretório.

### 3. Durabilidade de conteúdo e modo

Em writes por replace/link:

1. escrever conteúdo;
2. `fsync` do arquivo;
3. aplicar modo final;
4. executar novo `fsync` do arquivo para incluir o modo;
5. publicar por replace/link;
6. `fsync` do diretório.

Falha em qualquer fronteira deve deixar o último estado válido ou um temporário
validável, nunca promover sucesso.

### 4. Centralizar acessos restantes

Migrar os caminhos de produção ainda diretos, incluindo:

- status/heartbeat em runtime;
- iteration e metadados de resume;
- `technical-summary.json` na criação do request;
- snapshots, reports e evidence acessados pelos fluxos alterados.

Arquivo ausente pode usar default somente quando o contrato permitir ausência.
Corrupção, schema futuro, symlink, tipo, owner ou modo incorreto falham
fechados; iteration corrompida nunca vira silenciosamente `1`.

### 5. Documentação honesta

Atualizar `ROADMAP.md` e esta task somente com garantias provadas. Não fechar
DX-08/M1 nem repetir claims de migration/rollback, que pertencem à DX-08B.

## Fora de escopo

- migration, backup e rollback;
- novos estados ou delivery remoto;
- cgroups, retenção e isolamento de segredos;
- proteção contra root ou processo deliberado com o mesmo UID.

## Critérios de aceite

1. Toda chamada artifactless de evento crítico falha antes de qualquer mutação.
2. Transação com artefato correto continua funcional e recuperável.
3. Lock inseguro é recusado sem alterar bytes, modo, inode ou diretório.
4. Lock novo e writes persistem conteúdo, modo e entrada de diretório.
5. Os acessos de produção listados usam a API segura comum.
6. Documentação não declara DX-08 ou M1 concluídos.

## Testes obrigatórios

- matriz de todos os eventos críticos via API artifactless;
- artefato ausente, incorreto, symlinkado e replay sem binding;
- lock symlink/hard link, modo/owner/diretório incorretos e troca de inode;
- snapshot byte-a-byte antes/depois de recusas;
- fault injection em chmod, segundo file-fsync, replace/link e dir-fsync;
- iteration/status/summary/heartbeat/snapshot/evidence inseguros;
- suíte completa, compileall, `bash -n` e `git diff --check`.

## Riscos residuais

Até a DX-08B, migration e rollback do candidato continuam não suportados para
uso confiável. O worktree deve permanecer local e sem merge de produto.
