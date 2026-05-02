# Releasing

Nexus Trade Engine uses [release-please](https://github.com/googleapis/release-please)
to drive every public release. The process is fully automated as long as
contributors use [Conventional Commits](https://www.conventionalcommits.org/)
on `main`.

## Versioning

We follow [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** — incompatible API or operator-visible behaviour change.
- **MINOR** — backwards-compatible feature additions.
- **PATCH** — backwards-compatible bug fixes, perf, docs.

While the project is `0.x`, breaking changes bump the **minor** number, not
the major (this matches semver pre-1.0 conventions and is configured via
`bump-minor-pre-major: true` in `release-please-config.json`).

The single source of truth for the current version is
`.release-please-manifest.json`. Two downstream artifacts are kept in lock
step automatically:

- `pyproject.toml` (`project.version`)
- `frontend/package.json` (`version`)

## Conventional Commits

The PR title (and the squash-merge commit) becomes the release-note entry.
Use one of:

| Type        | Section                  | Bumps version? |
|-------------|--------------------------|----------------|
| `feat`      | Features                 | Yes (minor)    |
| `fix`       | Bug Fixes                | Yes (patch)    |
| `perf`      | Performance Improvements | Yes (patch)    |
| `refactor`  | Code Refactoring         | Yes (patch)    |
| `docs`      | Documentation            | Yes (patch)    |
| `revert`    | Reverts                  | Yes (patch)    |
| `test`      | Tests                    | No (hidden)    |
| `build`     | Build System             | No (hidden)    |
| `ci`        | Continuous Integration   | No (hidden)    |
| `chore`     | Miscellaneous Chores     | No (hidden)    |

Add `!` after the type (e.g. `feat!: drop /v0/foo`) **or** include
`BREAKING CHANGE:` in the commit body to force a major bump (or, while we are
0.x, a minor bump).

Examples:

```
feat(webhooks): add Telegram template
fix(auth): reject expired MFA challenge tokens
docs(community): add CoC, security, governance, contributing
feat!: replace /api/v1/strategy with /api/v2/strategy
```

## How a Release Happens

1. PRs land on `main` with conventional-commit titles.
2. The **Release Please** workflow (`.github/workflows/release-please.yml`)
   runs on every push to `main`. It opens (or updates) a single rolling
   "release PR" that bumps the version, regenerates `CHANGELOG.md`, and
   updates the version files listed above.
3. Maintainers review the release PR. The diff is exactly the changelog
   delta and version bumps — nothing else.
4. Merging the release PR triggers release-please again, which:
   - Tags the commit (`vX.Y.Z`).
   - Creates a GitHub Release with the changelog excerpt.
   - Triggers any downstream workflows that listen on `release: published`
     (for example, container image and SBOM publishing — see issue #141).

No manual `git tag` or `gh release create` is required. Hand-rolled tags
will desynchronise the manifest and should be avoided.

## Cutting an Out-of-Cycle Patch

If a security or critical fix needs to land on a prior minor:

1. Create a `release-X.Y` branch from the tag.
2. Cherry-pick the fix.
3. Bump the version in `pyproject.toml`, `frontend/package.json`, and
   `.release-please-manifest.json` manually on that branch.
4. Tag and push: `git tag vX.Y.Z+1 && git push origin vX.Y.Z+1`.
5. Open a GitHub Release manually for the tag. Forward-port the fix to
   `main` so the next normal release picks it up.

This is intentionally awkward — it should be rare. See `SECURITY.md` for the
disclosure timeline that may force this path.

## First Release Checklist

Before `v0.1.0` (or whatever the first tagged release is) ships:

- [ ] `pyproject.toml`, `frontend/package.json`, and
      `.release-please-manifest.json` agree on the starting version.
- [ ] `CHANGELOG.md` exists and is committed (release-please refuses to
      operate without one).
- [ ] At least one conventional-commit landed on `main` since the last
      version, otherwise no release PR will open.

## Troubleshooting

- **No release PR appearing.** Either every commit on `main` since the last
  release was a hidden type (`chore`, `ci`, `build`, `test`), or the commit
  titles aren't conventional. Check the Actions tab for the most recent
  `release-please` run.
- **Wrong version bump.** Add or amend a commit with the correct prefix and
  push to `main` — the release PR rewrites itself.
- **Manifest drifted from `pyproject.toml`.** Edit
  `.release-please-manifest.json` to match what is actually deployed and
  push. The release PR will regenerate from that anchor.
