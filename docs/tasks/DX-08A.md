# DX-08A — Autoridade de eventos, locks e I/O persistente segura

Status: implementada; aguardando revisão formal.

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

## Implementação (evidência local)

Comportamento entregue neste worktree:

- `CRITICAL_BINDINGS` em `txn.py` é a classificação única. A API pública
  `transition_run()` **sempre** recusa eventos críticos (sem flag bypass);
  somente `LogicalTransaction` aplica status via helper privado sob lock,
  depois de validar `event == status_event` e o contrato semântico dos
  artefatos exigidos (schema atual — schemas futuros falham fechados;
  `technical_status` do request deve ser `APPROVED`; `diff_hash` hex 64 /
  `callback_token` hex 32; `decision == "approve"` / `"reject"` conforme o
  evento; `run_id` do run; Telegram IDs positivos; request presente na txn ou
  em disco com token consumido quando a decisão/rejeição exige).
  `iteration-budget.json` reusa `validate_iteration_budget_document` (cadeia
  não vazia, limites, idempotency, bindings de review/snapshot em disco) —
  `effective_limit` com `extensions: []` é recusado antes do journal.
  `commit_status_with_audit_locked` também recusa críticos antes de
  journal/status/audit. Recovery reusa o mesmo validador e não promove
  `HUMAN_APPROVED` nem budget com bindings forjados.
- Replay no estado-destino crítico: só `already_applied` com binding
  contrato-válido no disco (byte-idêntico ao proposto, ou — só para
  `failure.json` — first-failure-wins com falha válida já publicada). Binding
  ausente, `{"corrupt": true}`, schema futuro ou mismatch (exceto
  first-failure-wins) **recusa sem mutação** de status/audit/journal/artefato;
  destino sozinho nunca autoriza inventar ou “reparar” o binding.
  `add_json` recusa nomes reservados (`.state.lock`, `.approval.lock`,
  `.resume.lock`, `.delivery.lock`, `status`, `.txn.json`, `audit-trail.json`
  e qualquer dotfile).
- Destinos de artefato inseguros (symlink/modo/tipo) e payloads vazios/parciais
  são recusados antes do journal.
- `secure_acquire_lock_fd` valida o caminho canônico (sem symlink
  intermediário), o diretório/lock sem reparo (owner, modo, hard link,
  symlink, inode); `run_scoped_lock`, `cmd_resume_exec` e
  `_probe_delivery_lock` revalidam o inode do pathname após `flock`.
  `authorize_iteration_extension` / `plan_resume` / `attach_evidence` /
  `validate_run` usam `assert_path_components_unlinked` (não `.resolve()` que
  esconderia symlink intermediário). Locks novos `0600` com `fsync` do
  diretório.
- `secure_write_bytes` / exclusive write: write completo (loop até esgotar
  bytes; short `os.write` falha fechado) → fsync → `fchmod` → fsync →
  replace/link → dir fsync.
- Runtime lê status via `read_status` e publica heartbeat na API segura; falha
  em leitura de status ou escrita de heartbeat **termina e recolhe** o process
  group supervisionado antes de propagar o erro (fail-closed).
- `plan_resume` / `_review_hash` falham fechados em snapshot/report dangling,
  symlink, modo/owner incorreto, **report privado zero-byte** (corrupt, não
  ausente), campos desconhecidos, chaves obrigatórias ausentes, tipos inválidos
  ou schema futuro; iteration corrompida nunca vira `1`;
  resume de EXECUTING/REVIEWING valida o envelope real do Cursor Agent
  `--output-format json` persistido por `run_task.sh`/`supervise`
  (`type`/`subtype`/`is_error`/`duration_*`/`result`/`session_id`/`request_id`/
  `usage`) — o fixture sintético `{"summary":…}` **não** é contrato de
  produção e é recusado;
  `create-request` valida `technical_summary.json` (schema, campos **e tipos**,
  `telegram_messages`, `file_count`) pela API segura e falha fechado em
  malformação/`null`; evidence valida o manifesto **antes** de publicar blob
  (fonte nova + manifesto future-schema não deixa evidence dir/lock/blob);
  `_test_summary` / `prepare_review_artifacts` falham fechados em validation
  logs/results symlinkados **ou com modo 0644/0666**, findings aninhados
  malformados, task symlinkada/insegura e `validation-*-result.json` com
  schema futuro/campos inválidos **antes** de publicar
  `reviewed_manifest.json` / `technical_summary.json` (artefatos já publicados
  permanecem byte-idênticos na recusa).
- Produção: `review-status` (consumido por `run_task.sh`) e
  `prepare_review_artifacts` leem reviewer/executor/validation via a API
  segura no-follow com `require_private=True` (symlink **e** modo inseguro
  falham fechados). `prepare_review_artifacts` valida o contrato completo do
  reviewer (mesmo de `review-status`) e o envelope Cursor Agent do executor
  **antes** de qualquer publicação derivada; invocação direta com reports
  malformados não reescreve manifest/summary seed. Arquivos de task no worktree
  Git continuam legíveis com `require_private=False` (somente no-follow/
  containment), pois o checkout tipicamente não é `0600`.

Evidência de teste (local, worktree isolado):

- `pytest tests/unit`: **445 passed**;
- `python3 -m compileall -q scripts/agents/dx`: ok;
- `bash -n` em scripts shell sob `scripts/agents`: ok;
- `git diff --check` (tracked): ok;
- whitespace explícito em untracked (incl. `tests/unit/test_agent_dx08a.py`): ok.

Arquivos de regressão principais: `tests/unit/test_agent_dx08a.py` (matriz
artifactless, matriz semântica de request/decisão, matriz de replay no
estado-destino, nomes reservados + segundo lock concorrente, symlink
intermediário/pós-flock em CLI/authorize/probe, envelope Cursor Agent real em
resume EXECUTING/REVIEWING, task symlinkada + validation future-schema sem
republicar manifest/summary, modos 0644/0666 em reviewer/executor/validation
sem mutar bytes, contratos malformados de reviewer/executor em
`prepare_review_artifacts` sem publicar derivados, summary/report/evidence
fail-closed byte-a-byte, spoof de autoridade, supervisor com reap) e ajustes em
`test_agent_state_machine.py` / `test_agent_human_approval.py` /
helpers DX-02/DX-04/DX-08.

## Riscos residuais

- Migration, backup e rollback do candidato DX-08 continuam **não** cobertos por
  esta task; uso confiável desses caminhos depende da DX-08B.
- O worktree permanece local e sem merge de produto; nenhum hash de commit de
  entrega nem URL de branch foram publicados aqui.
- Baseline de ameaça inalterado: processos com o mesmo UID são confiáveis; root
  ou peer hostil sob o mesmo UID permanece fora de escopo. Helpers privados
  (`_transition_under_lock`) não são API pública; abuso deliberado do mesmo UID
  importando internos continua fora do modelo.
- Matriz de fault injection cobre as fronteiras da API comum de write (incluindo
  short `os.write`); não afirma cobertura exaustiva de cada evento DX-07 sob
  cada ponto de falha de recovery (risco residual herdado, documentado na
  DX-08).
- Leituras pontuais legadas (ex.: `human_approval_request.json` via `read_json`
  em alguns caminhos de resume) ainda dependem da API segura de `atomic`;
  hardening adicional de todos os `Path.is_file()` restantes fora dos fluxos
  alterados não é escopo desta task. Arquivos de task no worktree Git não
  exigem modo privado (`require_private=False` só nessa leitura de título).
- O teste de supervisor que força status `0644` pode falhar em sandboxes que
  bloqueiam `killpg`/sockets Unix com `PermissionError` após a falha fechada
  esperada; fora do sandbox a regressão passa (445 passed com sockets
  permitidos).
