<!--
Thanks for opening a PR! Please fill in each section below — empty
sections will slow down review.
-->

## Summary

<!-- 1–3 bullets: what this PR does and why. -->

-
-

## Linked Issues

<!-- Use "Closes #123" / "Fixes #123" so the issue auto-closes on merge. -->

Closes #

## Changes

<!-- Notable code, schema, config, or behavioural changes. -->

-

## Test Plan

<!--
Markdown checklist of how reviewers (or CI) will verify this works.
Include manual steps for anything CI doesn't cover.
-->

- [ ] Unit tests added / updated
- [ ] Integration tests added / updated (if DB or HTTP boundary changed)
- [ ] `make test` passes locally
- [ ] `make lint` passes locally
- [ ] Manual verification: <describe>

## Database / Migrations

<!-- Delete this section if no schema change. -->

- [ ] Added an Alembic revision in `engine/db/migrations/versions/`
- [ ] Migration is reversible OR the down-migration is intentionally a no-op
- [ ] Tested against an existing dev database (no destructive surprises)

## Breaking Changes

<!--
List any API, config, env var, or behaviour change that would break an
existing operator. If none, write "None".
-->

None

## Follow-ups

<!--
Anything intentionally deferred. Link to a tracking issue when possible.
-->

-

## Checklist

- [ ] PR title follows conventional commits (`feat:`, `fix:`, `docs:`, ...)
- [ ] Self-reviewed the diff
- [ ] No secrets, credentials, or PII in the diff
- [ ] Updated relevant docs (README, ADR, in-code docstrings)
