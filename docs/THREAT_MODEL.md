# Modelo de ameaça — persistência (DX-08)

Atualizado em 2026-07-25.

## Baseline suportado

- state root e run dirs devem ser `0700`; artefatos sensíveis `0600`;
- processos com o **mesmo UID** do operador são confiáveis entre si;
- `flock`, hashes canônicos e a cadeia de audit/ledger detectam corrupção,
  truncamento, symlink/FIFO/socket/device, hard link inesperado, drift de inode e
  adulteração de timestamp na trilha;
- leitura de produção nunca segue symlink e rejeita modo com bits de
  grupo/outros (`require_private=True`); arquivo inseguro **não** é sobrescrito
  em silêncio para “consertar” permissões — a escrita falha fechada para
  auditoria;
- inspect/migrate podem ler legados com `require_private=False` de forma
  explícita, sem promover nem reparar modos;
- recovery/migration **não** inventam aprovação, decisão humana ou remote OID;
- administrador/root permanece **fora** do modelo.

## Hardened (não anunciado nesta release)

Um modo hardened só pode ser declarado quando:

1. a chave ou autoridade de autenticação do ledger/audit ficar inacessível ao
   processo executor; e
2. testes demonstrarem que o executor não forja eventos de aprovação/extensão.

Esta release **não** implementa HMAC com chave legível pelo mesmo UID e não
chama isso de proteção. Qualquer anúncio prematuro seria desonesto.

## Dados proibidos no audit trail e backups diagnósticos

- tokens de callback Telegram;
- variáveis de ambiente e segredos;
- URLs com credenciais;
- corpo completo de reports de executor/revisor.

## Superfície residual

- um processo hostil com o mesmo UID pode ainda substituir arquivos se obtiver
  o lock ou vencer uma condição de corrida de escrita — o baseline assume que
  esse UID é confiável;
- filesystems de rede podem mentir sobre durabilidade mesmo após `fsync`;
- containment depende de callers passarem `containment_root` nos hot paths de
  transação; escapes fora desses callers ainda são responsabilidade do layout
  do state root `0700`;
- outbox publicado por `enqueue_notification` isolado (fora do
  `create-request` transacional) permanece best-effort e não reabre o gate.
