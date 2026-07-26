# Personal Stable v0.1

Este é o marco de confiabilidade para uso pessoal do `agent-loop`. Ele substitui,
para o uso atual, o roadmap de distribuição a terceiros.

## Modelo de confiança

- o computador, o usuário local e os processos com o mesmo UID são confiáveis;
- o state root permanece local, pertencente ao usuário e com modo `0700`;
- repositórios, dependências e comandos podem falhar ou consumir recursos, mas
  não são tratados como adversários com acesso deliberado ao UID;
- root, adulteração concorrente pelo mesmo UID e host comprometido estão fora do
  escopo;
- commit, merge e push continuam manuais;
- runs de schema antigo não são migrados automaticamente: devem ser arquivados
  e reiniciados.

## Baseline aceito

O candidato `wip/dx-08a1-review-2` é a base funcional. São riscos aceitos:

- troca deliberada de inode no pequeno intervalo entre validação e leitura;
- criação persistente de `.resume.lock` na primeira retomada;
- ausência de migration/rollback automático para runs históricos.

Esses riscos não autorizam declarar proteção contra processos maliciosos com o
mesmo UID.

## Progresso PS-01

Status: **aceita para o baseline pessoal após revisão manual**. Isolamento de
fase passou de process group para scope `systemd --user` exclusivo.

Comportamento:

- cada fase sobe em `agent-loop-<run>-<phase>-<iteration>.scope` via
  `systemd-run --user --scope --collect --property=Delegate=yes`;
- heartbeat/result registram `systemd_unit`; `process_group` fica `null`;
- timeout, sinal, erro, falha de heartbeat/status **e sucesso** param a
  unidade, aguardam inativo e exigem cgroup recursive-empty (`cgroup.events`);
- `start_scoped_popen` reaps wrapper/scope em qualquer exceção após o `Popen`
  (incl. falha de query `systemctl`) antes de devolver o processo; depois do
  retorno, `supervise_command` reaps em qualquer exceção (incl. setup de
  threads/handlers) e no exit do comando;
- falhas de query/stop do `systemctl --user` e de leitura de cgroup falham
  fechado (não são tratadas como unidade ausente/vazia);
- start sem manager/usuário, sem scope ativo com filho ainda rodando, ou com
  recusa de criação transitória (evidência positiva no stderr do
  `systemd-run`) falha fechado (`SystemdScopeError`), sem fallback silencioso
  para process group; exit nonzero de comando curto legítimo — inclusive
  `/bin/false` sob race com `--collect` — continua sendo falha de comando;
- stdout/stderr sanitizados e artefatos de fase permanecem como antes.
- `systemd_unit` é aceito como campo opcional pelos contratos estritos, sem
  invalidar artefatos legados;
- respostas incompletas de `systemctl show` falham fechado.

Evidência local (host pessoal, `systemctl --user is-system-running` →
`degraded`, bus de usuário alcançável):

- `tests/unit/test_agent_ps01.py`: 22 passed (inclui integração real, contrato
  novo/legado, resposta incompleta fail-closed, setsid em sucesso/timeout, cgroup
  aninhado, fail-closed de systemctl/cgroup, query pós-Popen reaped, criação
  transitória vs `/bin/false` repetido, SIGINT/SIGTERM/SIGHUP, erro
  pré-handler; nenhum skip);
- suíte `tests/unit`: 483 passed;
- `python3 -m compileall -q scripts/agents/dx` e `git diff --check`: ok.

Restrições operacionais desta fatia: apenas um loop ativo por vez; evitar
interrupção manual durante a janela inicial de criação do scope; manager
`systemd --user` saudável e alcançável. Se uma falha persistente do systemctl
impedir a confirmação do cleanup, não retomar até restaurá-lo e conferir o
scope. Esses pontos, a possível colisão de nomes entre projetos e a
classificação baseada no stderr ficaram como hardening não bloqueante para o
uso pessoal.

Riscos residuais: escape deliberado para outro cgroup pelo mesmo UID; ausência
de quotas Memory/Tasks/CPU (PS seguintes / DX-09); criação transitória sem
stderr reconhecível pode aparecer como falha de comando; limpeza de unidade
órfã pós-reboot no resume ainda não feita. A terceira revisão automática
permaneceu em `CHANGES_REQUESTED`; a aceitação é uma decisão manual do operador
após corrigir os dois defeitos diretamente relevantes ao baseline pessoal.


## Trabalho necessário

| Ordem | Task | Resultado |
|---|---|---|
| 1 | [PS-01](tasks/PS-01.md) | scope systemd e cleanup de todos os descendentes — implementada |
| 2 | [PS-02](tasks/PS-02.md) | stdout/stderr com limite |
| 3 | [PS-03](tasks/PS-03.md) | arquivos e snapshot com limite |
| 4 | [PS-04](tasks/PS-04.md) | segredos separados por fase |
| 5 | [PS-05](tasks/PS-05.md) | teste completo e runbook pessoal |

Cada task possui um único objetivo. O orçamento normal é uma iteração, com no
máximo uma iteração corretiva quando o finding pertencer diretamente ao objetivo.
Finding adjacente vira backlog; não expande a task em execução.

O procedimento autônomo por SSH está em
[Handoff remoto do Personal Stable](REMOTE_HANDOFF.md).

Durante este marco, a documentação obrigatória de cada review é este documento
e a task atual. O roadmap para terceiros permanece histórico/opcional e não deve
ser alterado por microtasks pessoais.

## Fora do marco pessoal

- proteção contra adulteração deliberada pelo mesmo UID;
- migrations, backup transacional e rollback automático;
- quotas completas, fork-bomb adversarial e isolamento multiusuário;
- outbox Telegram de alta disponibilidade;
- pacote público, compatibilidade multiplataforma, SBOM e release assinada;
- governança, suporte e documentação para terceiros.

## Critérios de saída

1. Um run real executa, revisa, solicita decisão e verifica o snapshot aprovado.
2. Timeout ou erro de heartbeat deixa o scope da fase vazio.
3. Saída e snapshot acima dos limites falham preservando o worktree.
4. Executor, bootstrap e validação recebem somente seus segredos declarados.
5. Retomada, rejeição e orçamento adicional funcionam em teste ponta a ponta.
6. Nenhuma etapa cria commit, merge, branch remota ou push.
7. O runbook SSH permite observar e interromper o loop sem perder o worktree.
