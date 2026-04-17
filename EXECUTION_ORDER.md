# Execution Order

**Source of truth for what gets built next, and in what order.**

Generated 2026-04-17 from a triage of 188 multica issues + the post-#192/#193 codebase. After cleanup (close 14 stale, reassign 56 mis-tagged, dedupe 13 duplicates), 77 active issues remain. Of those, **73 are mapped here**; the rest are either parent meta-issues or deferred sub-tasks.

## How to read this

Every active multica issue has been renamed with a tag of the form **`[N.L.k]`**:

| Token | Meaning |
|---|---|
| `N` | **Phase number** (1–7). Phases run sequentially: Phase 2 work doesn't start until Phase 1 gates are closed. |
| `L` | **Lane letter** (A, B, C, …) within a phase. Lanes within a phase run **in parallel** — pick any lane to staff. |
| `k` | **Position within a lane** (1, 2, 3, …). Items in a lane are sequenced — work the lower number first. |

Examples:
- `[1.1]` — sequential phase 1 (no lanes — only one chain).
- `[3.A.2]` — Phase 3, Lane A, second item. Done after `[3.A.1]`. **Independent** of `[3.B.x]`, `[3.C.x]`, etc.
- `[5.A.5]` — fifth item in Phase 5's UI shell lane.

**Two issues with the same `N.L` are sequenced.** **Two issues with the same `N` but different `L` can run in parallel.**

## Phases at a glance

| Phase | Theme | Lanes | Parallel work? | Total items |
|---|---|---|---|---|
| **1** | Foundations (regression safety) | 1 sequential chain | no | 2 |
| **2** | Safety & legal (auth, sandbox, ToS) | A, B, C | yes — 3-way parallel | 3 |
| **3** | Engine completeness | A (live), B (RT data), C (MCP), D (multi-asset), E (multi-strategy) | yes — 5-way parallel | 16 |
| **4** | Production readiness | A (dev env), B (observability), C (deploy), D (notifications), E (docs) | yes — 5-way parallel | 10 |
| **5** | Frontend polish | A (screens), B (cross-cutting UX) | yes — 2-way parallel | 20 |
| **6** | Growth | A (marketplace), B (compliance), C (REST/docs), D (portfolio extras) | yes — 4-way parallel | 13 |
| **7** | Research / nice-to-have | A (metrics depth), B (crypto), C (forex), D (options), E (DeFi), F (HFT) | yes (after Phase 3 ships) | 9 |

## Phase 1 — Foundations (sequential)

Locks today's correctness gains (PRs #188, #190, #192, #193) before anything else touches the engine.

| Tag | Issue | Status |
|---|---|---|
| `[1.1]` | **SEV-217** Backtest golden-file regression tests | **LANDED in PR #201** |
| `[1.2]` | **SEV-264** 80%+ coverage on core engine | open |

Move to Phase 2 when both are done. `[1.2]` blocks Phase 2 because without coverage gates, the auth + plugin sandbox work in Phase 2 can silently regress engine math.

## Phase 2 — Safety & legal (3-way parallel)

Three independent safety pre-requisites for any deploy beyond localhost. Pick whichever lane you can staff.

### Lane A — Auth + RBAC
| Tag | Issue |
|---|---|
| `[2.A.1]` | **SEV-233** Auth + RBAC per ADR-0002 — 5-day plan |

### Lane B — Sandboxing
| Tag | Issue |
|---|---|
| `[2.B.1]` | **SEV-267** Plugin sandbox with security isolation |

### Lane C — Legal
| Tag | Issue |
|---|---|
| `[2.C.1]` | **SEV-206** Risk disclaimers, EULA, ToS, legal-notice surfaces |

**All three must close before Phase 3 live-trading work ships to anything publicly reachable.** Lane B + Lane C can defer to mid-Phase-3 if they slow down progress; Lane A is the true gate.

## Phase 3 — Engine completeness (5-way parallel)

The core trade lifecycle. Five independent lanes, internal sequencing within each.

### Lane A — Live trading (sequential within lane)
| Tag | Issue |
|---|---|
| `[3.A.1]` | **SEV-258** Pluggable broker adapter system |
| `[3.A.2]` | **SEV-266** Alpaca live broker (first concrete adapter) |
| `[3.A.3]` | **SEV-269** Paper trading w/ live data feeds |

### Lane B — Real-time data
| Tag | Issue |
|---|---|
| `[3.B.1]` | **SEV-275** WebSocket API for portfolio updates |

### Lane C — MCP server (sequential within lane)
| Tag | Issue |
|---|---|
| `[3.C.1]` | **SEV-223** MCP server core (parent / scaffold) |
| `[3.C.2]` | **SEV-219** MCP market data tools |
| `[3.C.3]` | **SEV-220** MCP trading control tools |
| `[3.C.4]` | **SEV-221** MCP backtesting tools |
| `[3.C.5]` | **SEV-222** MCP strategy management tools |

### Lane D — Multi-asset foundations (sequential within lane)
| Tag | Issue |
|---|---|
| `[3.D.1]` | **SEV-239** Provider adapter system + registry |
| `[3.D.2]` | **SEV-218** Reference data system (symbol master) |
| `[3.D.3]` | **SEV-240** Abstract Instrument model |
| `[3.D.4]` | **SEV-259** Abstract Asset model |

### Lane E — Multi-strategy (sequential within lane)
| Tag | Issue |
|---|---|
| `[3.E.1]` | **SEV-261** Multi-strategy portfolio + signal aggregation |
| `[3.E.2]` | **SEV-274** Strategy evaluation/comparison engine |
| `[3.E.3]` | **SEV-198** A/B testing + shadow mode |

## Phase 4 — Production readiness (5-way parallel)

| Tag | Lane | Issue |
|---|---|---|
| `[4.A.1]` | A — Dev env | **SEV-260** Docker dev hot-reload |
| `[4.B.1]` | B — Observability | **SEV-251** Pluggable observability interfaces |
| `[4.B.2]` | B — Observability | **SEV-215** Structured logging + correlation IDs |
| `[4.B.3]` | B — Observability | **SEV-214** Grafana dashboards as code + alert runbooks |
| `[4.B.4]` | B — Observability | **SEV-213** SLOs + error budgets + burn-rate alerts |
| `[4.C.1]` | C — Deploy | **SEV-216** Blue/green + canary deploy strategy |
| `[4.C.2]` | C — Deploy | **SEV-232** Kubernetes Helm chart |
| `[4.D.1]` | D — Notifications | **SEV-253** Webhook notification system |
| `[4.D.2]` | D — Notifications | **SEV-247** User-configurable data retention |
| `[4.E.1]` | E — Docs | **SEV-241** Self-hosting deployment guide |

## Phase 5 — Frontend polish (2-way parallel)

### Lane A — UI screens (sequential within lane; SEV-429+SEV-439 first, then screens in any order)
| Tag | Issue | Status |
|---|---|---|
| `[5.A.1]` | **SEV-429** Layout shell + sidebar nav | files in PR #201 |
| `[5.A.2]` | **SEV-439** Reusable data viz components | files in PR #201 |
| `[5.A.3]` | **SEV-438** Real-time data layer / WebSocket hooks (depends on `[3.B.1]`) | open |
| `[5.A.4]` | **SEV-272** Dashboard with portfolio + equity | partial in PR #201 |
| `[5.A.5]` | **SEV-433** Positions & Orders screen | partial in PR #201 |
| `[5.A.6]` | **SEV-435** Risk Monitor screen | partial in PR #201 |
| `[5.A.7]` | **SEV-434** Strategy Runner screen | partial in PR #201 |
| `[5.A.8]` | **SEV-428** Backtest Studio screen | partial in PR #201 |
| `[5.A.9]` | **SEV-425** Strategy Lab screen | partial in PR #201 |
| `[5.A.10]` | **SEV-426** Marketplace screen | partial in PR #201 |
| `[5.A.11]` | **SEV-437** Plugin Dev Console | partial in PR #201 |
| `[5.A.12]` | **SEV-431** Settings screen | open |

### Lane B — Cross-cutting UX
| Tag | Issue |
|---|---|
| `[5.B.1]` | **SEV-208** Theming + dark-mode design tokens |
| `[5.B.2]` | **SEV-207** Empty/loading/error states |
| `[5.B.3]` | **SEV-211** First-run onboarding wizard |
| `[5.B.4]` | **SEV-209** Settings/preferences page |
| `[5.B.5]` | **SEV-210** In-app notification center (consumes `[4.D.1]`) |
| `[5.B.6]` | **SEV-197** Keyboard shortcuts + command palette |
| `[5.B.7]` | **SEV-212** WCAG 2.1 AA conformance |
| `[5.B.8]` | **SEV-246** i18n prep + locale framework |

## Phase 6 — Growth (4-way parallel)

| Tag | Lane | Issue |
|---|---|---|
| `[6.A.1]` | A — Marketplace | **SEV-234** Self-hosted strategy registry backend |
| `[6.A.2]` | A — Marketplace | **SEV-202** Monetization (Stripe Connect) |
| `[6.A.3]` | A — Marketplace | **SEV-262** nexus-sdk CLI tool |
| `[6.A.4]` | A — Marketplace | **SEV-201** Python + TypeScript SDK clients |
| `[6.B.1]` | B — Compliance | **SEV-205** Regulatory report generation |
| `[6.B.2]` | B — Compliance | **SEV-248** Tax export CSV/PDF |
| `[6.B.3]` | B — Compliance | **SEV-252** Tax jurisdiction engine + rule plugins |
| `[6.B.4]` | B — Compliance | **SEV-204** Runtime wash-sale enforcement |
| `[6.B.5]` | B — Compliance | **SEV-203** GDPR/CCPA DSR handling |
| `[6.C.1]` | C — REST/docs | **SEV-245** Complete REST API + OpenAPI |
| `[6.C.2]` | C — REST/docs | **SEV-200** Migration guides (Backtrader/QC/Zipline) |
| `[6.C.3]` | C — REST/docs | **SEV-199** CODE_OF_CONDUCT, templates, SECURITY, GOVERNANCE |
| `[6.D.1]` | D — Portfolio extras | **SEV-231** Tagging + cross-portfolio analytics |

## Phase 7 — Research / nice-to-have (defer, parallel)

Don't start until Phase 3 lanes A+D land. Each `7.X.*` lane is a separate research thread.

| Tag | Lane | Issue |
|---|---|---|
| `[7.A.1]` | A — Metrics depth | **SEV-225** 86-KPI metrics engine |
| `[7.A.2]` | A — Metrics depth | **SEV-224** 16-chart pro analytics dashboard |
| `[7.A.3]` | A — Metrics depth | **SEV-226** 33-component cost model |
| `[7.B.1]` | B — Crypto | **SEV-256** Crypto engine |
| `[7.B.2]` | B — Crypto | **SEV-244** Binance adapter |
| `[7.C.1]` | C — Forex | **SEV-229** Forex engine |
| `[7.D.1]` | D — Options | **SEV-236** Options engine + Greeks |
| `[7.E.1]` | E — DeFi | **SEV-235** DeFi integration |
| `[7.F.1]` | F — HFT | **SEV-242** HFT infrastructure |

## Cross-phase dependencies

A handful of items in later phases unlock from earlier ones — call out the wires:

- `[5.A.3]` (frontend WebSocket hook) **depends on** `[3.B.1]` (WebSocket API).
- `[5.B.5]` (in-app notification center) **depends on** `[4.D.1]` (webhook system).
- All of Phase 3 `[3.A.x]` (live trading) **must NOT ship publicly** before `[2.A.1]` (auth) is merged.
- `[6.A.2]` (Stripe Connect monetization) **depends on** `[6.A.1]` (marketplace backend).

## What's NOT in this list

- **Done / cancelled issues** are excluded — they kept their old titles.
- **Wedpilot pollution** (56 issues) was reassigned out of the nexus project on 2026-04-17.
- **Duplicate pairs** (13 issues) were closed pointing to keepers in the comment trail.
- **Phase-N orchestration meta-issues** (the agent workflow bookkeeping) were closed; they don't represent product work.

## Updating this document

Whenever a multica issue is created, renamed, or closed in the nexus project, update both the issue's title prefix and the corresponding row here. The mapping is small enough to keep in sync by hand for now; if the count grows past ~150 we should auto-generate from `multica issue list --output json`.
