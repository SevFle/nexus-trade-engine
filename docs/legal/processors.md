# Sub-processor inventory

Operators of Nexus Trade Engine are responsible for keeping their own
sub-processor inventory current. This file is the **template** for
that inventory and the canonical list of categories every privacy
policy needs to reference.

> Update this file in your fork — do not point your customers at the
> upstream repo. The upstream cannot warrant who you have engaged.

## What goes here

For every external service that processes user data:

- **Service** — vendor name and the specific product.
- **Role** — controller / processor / sub-processor.
- **Purpose** — *why* the data goes there (storage, market data,
  notifications, observability, etc.).
- **Data categories** — which fields it touches.
- **Region(s)** — physical processing location and any cross-region
  transfers (relevant for GDPR Chapter V).
- **DPA** — link to the vendor's data-processing addendum.

## Categories every operator should review

Even if you delete every row below, ask whether you're using:

| Category | Examples |
|----------|----------|
| Hosting & compute | AWS / GCP / Azure / Hetzner / on-prem |
| Database & storage | RDS / Cloud SQL / Crunchy / Neon / Supabase / S3 / GCS |
| Cache / queue | ElastiCache / Memorystore / Upstash |
| Email | SES / Postmark / SendGrid / Mailgun |
| Notifications | Slack / Discord / Telegram / Twilio (SMS) |
| Market data | Polygon / IEX / Alpaca / Binance / Tradier |
| Brokers | Alpaca / IBKR / Binance / Kraken / Oanda |
| Auth IdPs | Google / GitHub / Auth0 / Okta / Keycloak |
| Observability | Sentry / Datadog / Grafana Cloud / New Relic |
| Container registry | GHCR (default) / ECR / GAR |
| CDN / WAF | Cloudflare / Fastly |
| Backup storage | S3 with versioning / B2 / GCS — see [`backup-and-recovery.md`](../operations/backup-and-recovery.md) |

## Template

```markdown
### <Service name>

- **Role:** <controller | processor | sub-processor>
- **Purpose:** <one-sentence why>
- **Data categories:** <e.g., user email, IP address, request metadata>
- **Region:** <e.g., eu-central-1 / us-east-1>
- **DPA:** <URL>
- **Notes:** <special arrangements, expiry, contact for incidents>
```

## Inventory (fill in)

> **This is the part you have to fill in for your deployment.**

### <None — fill in before going live>

The upstream repository ships with no sub-processors. Your privacy
policy must replace this section with the real list before you accept
EU or California user data.

## Related

- [`SECURITY.md`](../../SECURITY.md) — vulnerability disclosure.
- [`docs/operations/backup-and-recovery.md`](../operations/backup-and-recovery.md)
  — your backup destination is itself a sub-processor.
- gh#157 — GDPR/CCPA DSR handling, which this inventory underpins.
