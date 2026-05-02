# Project Governance

## Mission

Nexus Trade Engine ships an open, extensible algorithmic-trading platform
for retail and prosumer quants. Governance exists to keep contributions
flowing, decisions transparent, and the project usable by people who do
not have time to follow every commit.

## Roles

### Users
Anyone running the engine. No formal commitment.

### Contributors
Anyone whose pull request was merged. Recognized in the release notes.

### Maintainers
Have merge / release rights. They review PRs, triage issues, and shepherd
releases. Listed in `MAINTAINERS.md` (or the GitHub "maintainers" team)
once the role is formalized for this fork.

### Lead Maintainer
A single person who breaks ties on contested decisions and represents the
project externally. Currently the original author / fork owner. The role
rotates by mutual consent of active maintainers; rotation is announced in
a release note when it happens.

## Decision Making

We aim for **lazy consensus**: a maintainer proposes a change, waits 72
hours, and merges if no maintainer raises a substantive objection. For
larger changes (architecture, dependencies, breaking-change releases) we
prefer a written ADR (`docs/adr/`) and an explicit approval from at least
two maintainers.

When consensus cannot be reached, the lead maintainer decides. Their
decision is documented in the relevant ADR or PR.

## Becoming a Maintainer

Sustained, substantive contributions over several months — code review,
issue triage, releases, documentation — and a nomination from an existing
maintainer. The current maintainer team votes; a simple majority approves.

Maintainers can step down at any time and are marked `Emeritus` once they
do.

## Releases

We follow semantic versioning. Patch releases are cut on demand by any
maintainer; minor and major releases are announced one week in advance in
the Discussions area to gather final feedback. The release workflow
publishes container images, SDKs, and the changelog automatically.

## Code of Conduct

All participation is governed by [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
Reports of violations go through the channels in [`SECURITY.md`](SECURITY.md).

## Funding & Trademarks

Any external funding, sponsorships, or trademark filings are disclosed in
a public ADR before being executed. The project does not accept directed
funding that would constrain technical decisions.
