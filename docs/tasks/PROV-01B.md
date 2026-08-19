---
id: PROV-01B
status: planned
depends_on:
  - PROV-01A
---

# PROV-01B — Fail closed on engine drift

## Contexto

Após `PROV-01A`, novos runs conhecem o Engine N que os criou. Hoje `resume`,
`verify` e `integrate` ainda usam a instalação invocada no momento, sem comparar
essa identidade com o metadata do run.

## Objetivo

Aplicar um preflight único e fail-closed que impeça continuação, afirmação de
verificação completa ou integração quando o engine atual divergir do registrado.

## Escopo permitido

- comparação compartilhada de provenance para `resume`, `verify` e `integrate`;
- política explícita para runs legados;
- mensagens/exit codes estáveis e diagnóstico sanitizado;
- modo somente-leitura explícito para inspecionar snapshot legado;
- testes positivos, negativos e de ausência de mutação;
- documentação, esta task e `ROADMAP.md`.

## Fora de escopo

- bypass silencioso, match apenas por versão declarada ou timestamp;
- migrar/inventar provenance de run antigo;
- permitir `resume` ou `integrate` de legado sem identidade;
- assinatura remota, download de engine, push, deploy ou troca automática de
  controller.

## Decisões fixadas

- Engine match exige schema suportado e igualdade exata de `tool_root`,
  `engine_schema_version`, `source_fingerprint`, `git_commit` e `git_dirty`,
  inclusive valores `null`. Timestamps operacionais são ignorados. Commit ou
  dirty state divergentes bloqueiam mesmo quando os bytes cobertos pelo
  fingerprint coincidirem; nenhum campo relaxa outro mismatch.
- `resume`, `verify` normal e `integrate` fazem o mesmo preflight antes de
  qualquer mutação ou afirmação de sucesso.
- Run legado sem provenance é recusado nesses caminhos. `verify` pode oferecer
  uma flag explicitamente nomeada `--legacy-read-only` que verifica apenas os
  bindings antigos, marca `engine_match = unknown` e não habilita integração.
- Não haverá override administrativo para mismatch nesta task. Reproduzir o
  Engine N correto é o caminho normal.
- Mensagens estáveis distinguem `engine provenance missing`, `unsupported
  provenance schema` e `engine provenance mismatch`.

## Critérios bloqueantes

1. Match exato permite `resume`, `verify` e `integrate` preservando o
   comportamento atual.
2. Root, fingerprint, schema, commit ou dirty state incompatíveis produzem
   diagnóstico determinístico e bloqueiam sem alterar estado, locks persistentes,
   branch, worktree ou artefato terminal.
3. Provenance ausente/malformada em run novo falha fechada; run legado só admite
   a inspeção explicitamente somente-leitura definida acima.
4. O preflight não confia em campos vindos do worktree candidato nem segue
   symlinks para reconstruir a identidade atual.
5. Uma matriz de testes cobre cada operação, match/mismatch, run legado,
   corrupção e replay idempotente.
6. Consumidores externos com runs novos e engine estável mantêm sua CLI e seu
   fluxo; a mudança incompatível para runs legados fica documentada.

## Gate focado

```bash
venv/bin/python -m pytest -q \
  tests/unit/test_agent_dx02.py \
  tests/unit/test_agent_dx04.py \
  tests/unit/test_agent_pc03.py \
  tests/unit/test_agent_integration.py
bash scripts/agent-loop/test.sh
git diff --check
```

## Entrega obrigatória

Entregar um único comparador reutilizado pelos três comandos, contrato de erro
documentado e regressões de nenhuma mutação em falha. Atualizar task/roadmap com
resultados reais.

## Riscos / observações

- Mover fisicamente uma instalação idêntica é mismatch intencional nesta
  primeira política; qualquer override futuro exige task separada e auditoria.
