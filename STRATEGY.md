# Nexus Trade Engine — Development Strategy

**Authoritative.** The engine follows this execution plan strictly. Phases run sequentially. Lanes within a phase run in parallel.

---

## Execution Method

Every issue is tagged `[N.L.k]`:
- **N** = Phase (1-7). Sequential. Phase N+1 starts only after Phase N gates close.
- **L** = Lane (A, B, C...). Parallel within a phase. Pick any lane to staff.
- **k** = Position within lane. Sequential. Lower numbers first.

**85 open issues. ~15 are duplicates (close first). 73 active issues mapped across 7 phases.**

## Phase 1 — Foundations (sequential)

Lock down regression safety before anything else touches the engine.

| Tag | Issue | Title | Status |
|-----|-------|-------|--------|
| `[1.1]` | SEV-217 | Backtest golden-file regression tests | LANDED |
| `[1.2]` | SEV-264 | 80%+ coverage on core engine | open |

**Gate:** Both must close. `[1.2]` blocks Phase 2 because without coverage gates, auth + sandbox work can silently regress engine math.

**Also address in Phase 1 (prerequisites from original GitHub issues):**
- #116 — CI/CD pipeline (lint, test, build) — blocks everything
- #19 — Alembic migrations with initial schema — data layer foundation
- #1 — Backtest loop engine — core functionality
- #4 — Tax lot tracking with FIFO/LIFO — core functionality
- #3 — Historical market data loading and caching — core functionality

## Phase 2 — Safety & Legal (3-way parallel)

Three independent safety pre-requisites for any deploy beyond localhost.

### Lane A — Auth + RBAC
| Tag | Issue | Title |
|-----|-------|-------|
| `[2.A.1]` | SEV-233 / #86 | Auth + RBAC per ADR-0002 |

### Lane B — Sandboxing
| Tag | Issue | Title |
|-----|-------|-------|
| `[2.B.1]` | SEV-267 | Plugin sandbox with security isolation |

### Lane C — Legal
| Tag | Issue | Title |
|-----|-------|-------|
| `[2.C.1]` | SEV-206 | Risk disclaimers, EULA, ToS, legal-notice surfaces |

**Gate:** All three must close before Phase 3 live-trading ships publicly. Lane A is the critical path.

## Phase 3 — Engine Completeness (5-way parallel)

The core trade lifecycle. Five independent lanes.

### Lane A — Live Trading (sequential)
| Tag | Issue |
|-----|-------|
| `[3.A.1]` | SEV-258 — Pluggable broker adapter system |
| `[3.A.2]` | SEV-266 — Alpaca live broker adapter |
| `[3.A.3]` | SEV-269 / #13 — Paper trading w/ live data feeds |

### Lane B — Real-Time Data
| Tag | Issue |
|-----|-------|
| `[3.B.1]` | SEV-275 — WebSocket API for portfolio updates |

### Lane C — MCP Server (sequential)
| Tag | Issue |
|-----|-------|
| `[3.C.1]` | SEV-223 / #99 — MCP server core (scaffold) |
| `[3.C.2]` | SEV-219 / #104 — MCP market data tools |
| `[3.C.3]` | SEV-220 / #103 — MCP trading control tools |
| `[3.C.4]` | SEV-221 / #102 — MCP backtesting tools |
| `[3.C.5]` | SEV-222 / #101 — MCP strategy management tools |

### Lane D — Multi-Asset (sequential)
| Tag | Issue |
|-----|-------|
| `[3.D.1]` | SEV-239 — Provider adapter system + registry |
| `[3.D.2]` | SEV-218 — Reference data system (symbol master) |
| `[3.D.3]` | SEV-240 — Abstract Instrument model |
| `[3.D.4]` | SEV-259 — Abstract Asset model |

### Lane E — Multi-Strategy (sequential)
| Tag | Issue |
|-----|-------|
| `[3.E.1]` | SEV-261 — Multi-strategy portfolio + signal aggregation |
| `[3.E.2]` | SEV-274 — Strategy evaluation/comparison engine |
| `[3.E.3]` | SEV-198 / #162 — A/B testing + shadow mode |

## Phase 4 — Production Readiness (5-way parallel)

| Tag | Lane | Issue |
|-----|------|-------|
| `[4.A.1]` | Dev env | SEV-260 — Docker dev hot-reload |
| `[4.B.1]` | Observability | SEV-251 — Pluggable observability interfaces |
| `[4.B.2]` | Observability | SEV-215 — Structured logging + correlation IDs |
| `[4.B.3]` | Observability | SEV-214 — Grafana dashboards as code |
| `[4.B.4]` | Observability | SEV-213 — SLOs + error budgets |
| `[4.C.1]` | Deploy | SEV-216 — Blue/green + canary deploy |
| `[4.C.2]` | Deploy | SEV-232 / #87 — Kubernetes Helm chart |
| `[4.D.1]` | Notifications | SEV-253 — Webhook notification system |
| `[4.D.2]` | Notifications | SEV-247 / #90 — Data retention policies |
| `[4.E.1]` | Docs | SEV-241 — Self-hosting deployment guide |

## Phase 5 — Frontend Polish (2-way parallel)

### Lane A — UI Screens (sequential)
| Tag | Issue |
|-----|-------|
| `[5.A.1]` | SEV-429 — Layout shell + sidebar nav |
| `[5.A.2]` | SEV-439 — Reusable data viz components |
| `[5.A.3]` | SEV-438 — Real-time data layer / WebSocket hooks |
| `[5.A.4]` | SEV-272 / #10 — Dashboard with portfolio + equity |
| `[5.A.5]` | SEV-433 — Positions & Orders screen |
| `[5.A.6]` | SEV-435 / #98 — Risk Monitor screen |
| `[5.A.7]` | SEV-434 / #12 — Strategy Runner / Backtest Studio |
| `[5.A.8]` | SEV-428 — Backtest Studio screen |
| `[5.A.9]` | SEV-425 / #11 — Strategy Lab screen |
| `[5.A.10]` | SEV-426 / #14 — Marketplace screen |
| `[5.A.11]` | SEV-437 — Plugin Dev Console |
| `[5.A.12]` | SEV-431 — Settings screen |

### Lane B — Cross-Cutting UX
| Tag | Issue |
|-----|-------|
| `[5.B.1]` | SEV-208 / #152 — Theming + dark-mode design tokens |
| `[5.B.2]` | SEV-207 — Empty/loading/error states |
| `[5.B.3]` | SEV-211 / #149 — First-run onboarding wizard |
| `[5.B.4]` | SEV-209 / #151 — Settings/preferences page |
| `[5.B.5]` | SEV-210 / #150 — In-app notification center |
| `[5.B.6]` | SEV-197 / #164 — Keyboard shortcuts + command palette |
| `[5.B.7]` | SEV-212 / #148 — WCAG 2.1 AA conformance |
| `[5.B.8]` | SEV-246 / #93 — i18n prep + locale framework |

## Phase 6 — Growth (4-way parallel)

| Tag | Lane | Issue |
|-----|------|-------|
| `[6.A.1]` | Marketplace | SEV-234 / #85 — Self-hosted strategy registry |
| `[6.A.2]` | Marketplace | SEV-202 / #158 — Monetization (Stripe Connect) |
| `[6.A.3]` | Marketplace | SEV-262 / #20 — nexus-sdk CLI tool |
| `[6.A.4]` | Marketplace | SEV-201 / #159 — Python + TypeScript SDK clients |
| `[6.B.1]` | Compliance | SEV-205 — Regulatory report generation |
| `[6.B.2]` | Compliance | SEV-248 / #105 — Tax export CSV/PDF |
| `[6.B.3]` | Compliance | SEV-252 — Tax jurisdiction engine |
| `[6.B.4]` | Compliance | SEV-204 — Runtime wash-sale enforcement |
| `[6.B.5]` | Compliance | SEV-203 / #157 — GDPR/CCPA DSR handling |
| `[6.C.1]` | REST/docs | SEV-245 / #17 — Complete REST API + OpenAPI |
| `[6.C.2]` | REST/docs | SEV-200 / #160 — Migration guides |
| `[6.C.3]` | REST/docs | SEV-199 — CODE_OF_CONDUCT, templates |
| `[6.D.1]` | Portfolio | SEV-231 / #89 — Tagging + cross-portfolio analytics |

## Phase 7 — Research / Nice-to-Have (defer)

Don't start until Phase 3 lanes A+D land.

| Tag | Lane | Issue |
|-----|------|-------|
| `[7.A.1]` | Metrics | SEV-225 / #97 — 86-KPI metrics engine |
| `[7.A.2]` | Metrics | SEV-224 / #98 — 16-chart analytics dashboard |
| `[7.A.3]` | Metrics | SEV-226 — 33-component cost model |
| `[7.B.1]` | Crypto | SEV-256 / #84 — Crypto engine |
| `[7.B.2]` | Crypto | SEV-244 — Binance adapter |
| `[7.C.1]` | Forex | SEV-229 / #92 — Forex engine |
| `[7.D.1]` | Options | SEV-236 / #83 — Options engine + Greeks |
| `[7.E.1]` | DeFi | SEV-235 — DeFi integration |
| `[7.F.1]` | HFT | SEV-242 / #91 — HFT infrastructure |

## Cross-Phase Dependencies

- `[5.A.3]` (frontend WebSocket) **depends on** `[3.B.1]` (WebSocket API)
- `[5.B.5]` (notification center) **depends on** `[4.D.1]` (webhook system)
- All `[3.A.x]` (live trading) **must NOT ship publicly** before `[2.A.1]` (auth)
- `[6.A.2]` (Stripe monetization) **depends on** `[6.A.1]` (marketplace backend)
- `[7.x]` lanes **depend on** Phase 3 lanes A+D

## Duplicate Issues to Close

Close these with a comment referencing the canonical issue: #42, #35, #29, #28, #27, #26, #74, #73, #72, #67, #66, #65, #75.

## Priority for Autonomous Engine

1. **Phase 1 first** — CI/CD (#116), migrations (#19), coverage (SEV-264), core engine (#1, #4, #3)
2. **Phase 2 second** — Auth (#86), sandbox, legal
3. **Phase 3 third** — 5 parallel lanes (live trading, real-time data, MCP, multi-asset, multi-strategy)
4. **Phase 4-5** — Production readiness and frontend
5. **Phase 6-7** — Growth and research
