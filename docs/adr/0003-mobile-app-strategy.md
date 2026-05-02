# ADR-0003: Mobile experience strategy — PWA on top of the React frontend

- **Status**: Accepted
- **Date**: 2026-05-03
- **Deciders**: Lead maintainer
- **Tags**: frontend, mobile, ux, scope

## Context and Problem Statement

Nexus Trade Engine ships a React + Vite + Tailwind dashboard at
[`frontend/`](../../frontend/) for desktop browsers. Many of its
target users — prosumer quants, side-project algo traders, on-call
operators of a self-hosted deployment — explicitly want to *check*
the engine from a phone: glance at portfolio P&L, ack an alert,
trigger a backtest, read a runbook during an incident.

The dashboard is responsive in places but not designed mobile-first.
Three meaningfully different paths exist to "support mobile":

1. Make the existing web app fully responsive. No new platform.
2. Layer a Progressive Web App (PWA) on top of the responsive web
   app — installable, offline-capable, push-notification-capable.
3. Build a native app (React Native / Expo, or per-platform Swift +
   Kotlin), reusing the engine's REST/WebSocket API.

We need to commit to one direction so frontend work stops oscillating
between "make it responsive" and "what about a real app?".

## Decision Drivers

- **Reuse the existing investment.** The React + Vite + Tailwind
  frontend is real, growing, and not going anywhere. A solution that
  throws it away has to clear a high bar.
- **Maintainer-shaped team.** This is an open-source project with a
  small maintainer surface area. We will not commit to a second
  codebase that doubles platform-specific work.
- **Mobile workflows are mostly read-heavy.** Glance, alert, ack,
  light interaction. Heavy authoring (writing strategy code,
  reviewing 16-chart analytics, configuring complex backtests)
  happens on desktop and that is fine.
- **Push notifications matter.** A meaningful subset of mobile use is
  "tell me when something fired". The auth/MFA, webhook, and SLO
  alerts all benefit from push.
- **Operators self-host.** We cannot mandate that operators run an
  app store presence, push provider, or a code-signed binary.
- **Time-to-mobile is more valuable than polish.** Capturing the use
  cases above this quarter beats shipping a perfect native app a year
  from now.

## Considered Options

1. **Responsive web only** — push the existing dashboard to be fully
   responsive at 320 / 768 / 1024 / 1440. No installable app, no
   push, no offline read.
2. **PWA on top of responsive web** — add a Web App Manifest, a
   service worker for offline read + asset cache, Web Push for
   notifications, install prompts. Same codebase.
3. **Native — React Native / Expo** — second codebase that shares
   business logic via TypeScript but ships through the App Store and
   Play Store. Deeper platform integration than a PWA.
4. **Native — per-platform Swift + Kotlin** — the most polished
   option. Two new codebases, two App Store presences.

## Decision Outcome

Chosen option: **Option 2 — PWA on top of the responsive web app**,
because it captures the mobile use cases we actually care about
(glance, alert, ack, install on home screen) at the lowest possible
ongoing maintenance cost, reuses the existing frontend investment in
full, and does not bind the project to an app-store distribution
surface that operators of a self-hosted deployment cannot rely on.

We sequence the work in two phases so we can deliver value
incrementally:

- **Phase A — Responsive baseline.** Make every page in `frontend/`
  pass at 320 / 768 / 1024 viewports. Add hover/focus states for
  touch. Verify with Playwright at the breakpoints listed in
  the project's web testing rules. Tracked separately from this ADR
  because it benefits all users.
- **Phase B — PWA layer.** Web App Manifest with proper icons + theme
  color. Service worker that caches the shell + the `/api/v1/...`
  GETs that back the home dashboard for offline read. Web Push wired
  to the alert / webhook surface. Install prompt on supported
  browsers.

A native app (Option 3 / 4) is **deferred**, not rejected. The
trigger for revisiting is "we have a clear product-market signal that
PWA is leaving meaningful UX value on the table" — for example,
sustained complaints about Safari-on-iOS Web Push reliability, demand
for a Watch / widget surface, or App Store visibility itself becoming
a distribution requirement.

### Consequences

- **Positive**
  - Single codebase for desktop and mobile. Maintenance cost stays
    flat as we add features.
  - Reuse of existing React Query / auth / theming work — none of it
    rewrites.
  - Self-hosted operators get the mobile experience automatically; no
    app-store coordination.
  - Web Push lands on iOS 16.4+ and all modern Android, so push
    coverage is "good enough" today.
- **Negative**
  - We accept that a PWA still feels slightly less native than a real
    native app. Some users will notice.
  - Some platform-specific features (Watch, widgets, deep iOS share
    sheet integration) remain off the table.
  - We do not get an App Store presence for marketing visibility
    purposes.
- **Neutral**
  - The frontend will pick up a service worker. Service workers
    introduce their own failure modes (stale caches, registration
    loops). We accept this in exchange for offline read.

## Pros and Cons of the Options

### Option 1 — Responsive web only

- **Pros**
  - Cheapest. Pure CSS / layout work in the existing codebase.
  - Zero new platform surface, no service worker, no manifest.
- **Cons**
  - No install prompt, no home-screen icon — feels disposable.
  - No push notifications. Alerts have to be checked actively.
  - No offline read. Spotty network = blank page.

### Option 2 — PWA on top of responsive web (chosen)

- **Pros**
  - Single codebase. New mobile capabilities are all on the existing
    React stack.
  - Push notifications via the Web Push API. Works on iOS 16.4+ and
    every modern Android.
  - Installable to the home screen with a real icon and theme color.
    Bypasses browser chrome.
  - Offline read of cached pages — useful during flaky network or
    when the engine itself is unreachable.
  - Self-hosted operators don't need any new infrastructure.
- **Cons**
  - Service worker complexity (cache lifecycle, version skew).
  - Some platform integrations (Watch, widgets, deep share sheet)
    remain unavailable.
  - iOS web-push permission model is more conservative than Android.

### Option 3 — Native (React Native / Expo)

- **Pros**
  - Real app feel: native nav, gestures, system-integrated push.
  - Code sharing with the web app via TypeScript (with caveats —
    rendering layer differs).
  - App Store presence as a distribution channel.
- **Cons**
  - A *second* codebase to maintain. Even with shared TypeScript, the
    UI layer and platform glue are net-new.
  - App Store review delays every release.
  - Self-hosted operators need to ship signed builds — a non-starter
    for many.
  - Requires owning Apple + Google developer accounts and the
    associated annual cost.

### Option 4 — Native per-platform (Swift + Kotlin)

- **Pros**
  - Most polished UX possible.
  - Full access to platform-specific surfaces (WidgetKit, Live
    Activities, Wear OS Tiles).
- **Cons**
  - Two new codebases on languages most current contributors aren't
    using.
  - Highest ongoing maintenance cost by far.
  - Same App Store coupling as Option 3, doubled.
  - Way out of scope for the maintainer surface we have.

## Links

- Related issue: [#163](https://github.com/SevFle/nexus-trade-engine/issues/163)
- Related issues for the eventual implementation work:
  - Frontend accessibility / responsive baseline: #148, #149.
  - Real-time WebSocket surface (back-end side of push parity): #7.
- Supersedes: —
- Superseded by: —
- External references:
  - [Web Push on Safari (WebKit)](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
  - [PWA Builder](https://www.pwabuilder.com/) — useful for sanity
    checks on manifest + service-worker quality.
