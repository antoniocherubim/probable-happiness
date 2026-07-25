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

## Trabalho necessário

| Ordem | Task | Resultado |
|---|---|---|
| 1 | [PS-01](tasks/PS-01.md) | scope systemd e cleanup de todos os descendentes |
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
