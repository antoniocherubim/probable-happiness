# Roadmap para uso confiável por terceiros

Atualizado em 2026-07-24.

Estado atual: **pré-alpha**. O fluxo principal funciona e possui boa cobertura
local, mas ainda não deve ser oferecido como ferramenta confiável para terceiros
sem os bloqueadores P0 e P1 abaixo.

Este roadmap ordena o trabalho por dependência e risco. Datas devem ser definidas
somente depois que cada marco anterior cumprir seus critérios de saída.

## Objetivo de produto

Entregar uma ferramenta instalável e atualizável que execute Cursor e Codex em
worktrees isolados, mantenha aprovação humana vinculada ao snapshot revisado e,
quando configurado, publique apenas uma branch segura e auditável.

“Confiável para terceiros” significa:

- nenhuma alteração não revisada pode alcançar o remote pelo fluxo suportado;
- falhas, reinícios e concorrência terminam em sucesso comprovado ou falha fechada;
- tokens e credenciais não chegam a processos que não precisam deles;
- instalação, diagnóstico, atualização e remoção são reproduzíveis;
- versões suportadas e limites de segurança são explícitos;
- documentação, testes e artefatos de release permitem auditoria independente.

## Fundação existente

- [x] DX-01: gate humano autenticado pelo Telegram e vínculo ao hash revisado;
- [x] AG-01: ferramenta, target e state roots separados;
- [x] DX-02: perfil por projeto, bootstrap, timeout, heartbeat e retomada;
- [x] DX-03: resumo técnico, documentação obrigatória e delivery opt-in;
- [x] DX-04: extensão explícita e auditável do orçamento de iterações;
- [x] suíte local com 122 testes determinísticos;
- [x] push sem force, sem merge e com confirmação do OID remoto.

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

Próxima entrega recomendada: **DX-05 / M0 — worker seguro de delivery**. Ela
remove simultaneamente o bloqueio funcional da unidade systemd, a exposição do
token Telegram a subprocessos Git e o push síncrono dentro do callback.

### Tasks preparadas até M2

| Ordem | Marco | Task | Resultado |
|---|---|---|---|
| 1 | M0 | [DX-05](docs/tasks/DX-05.md) | aprovação enfileira delivery; bridge não executa Git |
| 2 | M0 | [DX-06](docs/tasks/DX-06.md) | worker Git endurecido e unidades systemd separadas |
| 3 | M1 | [DX-07](docs/tasks/DX-07.md) | máquina de estados central e transições condicionais |
| 4 | M1 | [DX-08](docs/tasks/DX-08.md) | persistência segura, durável e migrável |
| 5 | M2 | [DX-09](docs/tasks/DX-09.md) | cgroups e limites de recursos/saída |
| 6 | M2 | [DX-10](docs/tasks/DX-10.md) | segredos por fase, streaming e retenção segura |

## M0 — Separar Telegram de delivery Git

Resultado: o processo que conhece o token Telegram nunca executa Git no
repositório-alvo.

Tasks: [DX-05](docs/tasks/DX-05.md) e [DX-06](docs/tasks/DX-06.md).

### Trabalho

- [ ] fazer a bridge registrar aprovação e enfileirar um job de delivery durável;
- [ ] responder ao callback imediatamente, sem aguardar rede ou `git push`;
- [ ] criar worker separado, sem `AGENT_TELEGRAM_BOT_TOKEN`;
- [ ] usar ambiente Git allowlisted e remover variáveis `AGENT_*`, `GIT_*` não
  autorizadas, askpass e prompts interativos;
- [ ] desabilitar hooks no worker com configuração Git explícita;
- [ ] aplicar timeout a `ls-remote`, criação de commit e push;
- [ ] conceder ao worker escrita somente no Git common dir e no state root do
  projeto selecionado;
- [ ] gerar unidades systemd distintas para bridge e worker;
- [ ] validar remote, branch, decisão, estado e snapshot novamente no worker;
- [ ] manter idempotência quando o commit ou remote OID já existir.

### Critérios de saída

- um hook `pre-push` de teste não recebe o token Telegram nem outros segredos;
- callback continua responsivo quando o remote trava;
- a unidade endurecida completa delivery sem liberar escrita genérica na home;
- matar bridge ou worker em qualquer etapa não gera push não aprovado;
- teste end-to-end executa aprovação e delivery dentro do sandbox systemd real.

## M1 — Centralizar estado e tornar persistência recuperável

Resultado: toda transição é válida, monotônica, auditável e recuperável após
queda abrupta.

Tasks: [DX-07](docs/tasks/DX-07.md) e [DX-08](docs/tasks/DX-08.md).

### Trabalho

- [ ] definir enum e tabela única de transições permitidas;
- [ ] trocar escritas diretas de status por compare-and-set sob lock;
- [ ] exigir estado aprovado válido antes de iniciar ou retomar delivery;
- [ ] entradas inválidas devem falhar sem sobrescrever o estado anterior;
- [ ] centralizar leitura segura com `O_NOFOLLOW`, arquivo regular, owner e modo;
- [ ] usar `umask 077`, diretórios `0700` e arquivos sensíveis `0600`;
- [ ] executar `fsync` do diretório após `replace`, link e criação de artefatos;
- [ ] vincular timestamps e cada entrada do ledger à cadeia de integridade;
- [ ] escolher e documentar o modelo de ameaça:
  - baseline: processos com o mesmo UID são confiáveis;
  - hardened: worker/bridge em usuários separados e autenticação keyed do ledger;
- [ ] criar migrations versionadas para run, profile, approval, delivery e ledger;
- [ ] avaliar SQLite/WAL para transações, índices e outbox, mantendo export JSON
  auditável.

### Critérios de saída

- nenhuma API pública consegue entregar um run `BLOCKED` ou não aprovado;
- fault injection cobre cada fronteira entre artefato, status e notificação;
- qualquer arquivo truncado, symlink, modo/owner incorreto ou schema futuro falha
  fechado e com diagnóstico acionável;
- runs das duas versões persistidas anteriores migram ou recusam retomada sem
  mutação;
- o baseline declara o mesmo UID como fronteira de confiança; se um modo
  hardened for anunciado, ele demonstra que o executor não consegue forjar
  aprovação ou extensão.

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
- [ ] suportar credenciais efêmeras e documentar sua rotação;
- [ ] verificar que nenhum descendente permanece após timeout/cancelamento;
- [ ] definir política de espaço em disco e retenção de worktrees/runs.

### Critérios de saída

- fork bomb, processo com nova sessão, saída infinita e arquivo gigante são
  contidos em testes;
- um executor não recebe credenciais exclusivas das validações ou do delivery;
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
  aprovação, delivery e bridge;
- [ ] executar testes de integração Git com remotes bare;
- [ ] executar systemd sandbox em ambiente Linux compatível;
- [ ] adicionar property-based tests para máquina de estados e schemas;
- [ ] adicionar fault injection em todas as escritas e chamadas externas;
- [ ] testar concorrência real com processos, não apenas threads/fakes;
- [ ] adicionar análise de dependências, segredos e vulnerabilidades;
- [ ] bloquear merge quando um gate obrigatório falhar.

### Critérios de saída

- CI reproduz os 122 testes atuais e os novos cenários em ambiente limpo;
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
- [ ] publicar quickstart do primeiro run até aprovação e delivery;
- [ ] documentar threat model, trust boundaries e diferenças baseline/hardened;
- [ ] criar runbook para timeout, corrupção, remote indisponível e disco cheio;
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
