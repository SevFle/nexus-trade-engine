# Grafana dashboards

Dashboards-as-code for Nexus Trade Engine. All dashboards target the
recording rules and SLI metrics defined in
[`docs/operations/slos.md`](../../docs/operations/slos.md) and
[`observability/prometheus/slo-rules.yaml`](../prometheus/slo-rules.yaml).

| File | Purpose |
|------|---------|
| `dashboards/slo-overview.json`     | Single-pane SLO health (1h burn rates, multi-window trends, alert list). Start here. |
| `dashboards/api-traffic.json`      | HTTP RED metrics: RPS by route, error rate by route, p50/p95/p99 latency. |
| `dashboards/webhook-pipeline.json` | Webhook dispatcher operations: terminal outcomes, delivered ratio, failure trend. |

Each dashboard ships a `DS_PROMETHEUS` template variable so it works
regardless of how the operator names their Prometheus datasource.

## Provisioning

The two YAML files in `provisioning/` are example
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/)
configs:

- `provisioning/dashboards.yaml` — points Grafana at a directory of
  dashboard JSON. The example mounts dashboards under
  `/var/lib/grafana/dashboards/nexus`.
- `provisioning/datasources.yaml` — example Prometheus datasource. Edit
  the `url`, auth fields, and version to match your deployment.

A typical Docker Compose / Kubernetes mount looks like:

```yaml
# docker compose
services:
  grafana:
    image: grafana/grafana:latest
    volumes:
      - ./observability/grafana/provisioning:/etc/grafana/provisioning
      - ./observability/grafana/dashboards:/var/lib/grafana/dashboards/nexus
```

## Editing

- Dashboards are committed in their exported JSON form. After editing in
  Grafana, click **Share → JSON** and paste the result back into the
  matching file. Keep `id: null` and `version: 1` (Grafana ignores them
  on import).
- Don't hard-code datasource UIDs — always use `${DS_PROMETHEUS}` so the
  dashboard is portable.
- New dashboards should reference recording rules with the
  `nexus:slo:<journey>:<kind>:rate<window>` shape rather than raw
  `nexus_*_total` counters where possible. Recording rules are cheaper at
  render time and centralize the SLI math.

## Validating

GitHub renders the JSON on hover, but to confirm the schema parses run:

```bash
python3 -c "import json, glob; [json.load(open(p)) for p in glob.glob('observability/grafana/dashboards/*.json')]"
```

For a deeper check, import each file into a throwaway Grafana instance
and verify panels render without query errors against an empty
Prometheus.

## Helm packaging

Once a Helm chart exists for Nexus Trade Engine the dashboard JSON should
be shipped as ConfigMaps mounted into the Grafana deployment. That
packaging is intentionally out of scope until the chart itself lands.
