# Architecture

Nexus Trade Engine is an open, extensible algorithmic-trading platform.
This directory captures the *current* shape of the system — what
components exist, how they fit together, and where to add new code.

For *why* we made specific structural choices, see
[`docs/adr/`](../adr/README.md).

## Reading order

1. **[overview.md](overview.md)** — high-level component map, request
   lifecycle, key dependencies. Start here if you've never opened the
   repo before.
2. **[database.md](database.md)** — table inventory, migration policy,
   data ownership.
3. **[plugins.md](plugins.md)** — plugin SDK and the registry that
   loads strategies / data providers / execution backends at runtime.
4. **[`docs/operations/`](../operations/)** — how the running system is
   monitored, backed up, and recovered. Lives next to the runbooks
   that on-call uses.
5. **[`docs/adr/`](../adr/README.md)** — architecture decision records.
   Read these before proposing a change that contradicts them.

## Existing diagrams

The two `.jsx` files in this directory render as interactive React
components in our docs site:

- `trading-framework-architecture.jsx` — top-level engine + frontend +
  worker + queues.
- `plugin-sdk-architecture.jsx` — plugin lifecycle, sandboxing, and
  the registry.

The flat-markdown architecture docs (`overview.md`, `database.md`,
`plugins.md`) are the source of truth; the diagrams are a presentation
layer on top.

## Conventions

- Markdown files in this directory describe the **current** state.
  When you change the code, change the doc in the same PR.
- Forward-looking design lives in an ADR, not in the architecture
  docs. If you find yourself writing "we will" or "soon we will",
  it's an ADR.
- Each component doc should answer four questions:
  1. What does it do?
  2. What are its inputs and outputs?
  3. What does it depend on?
  4. Where does the code live?
