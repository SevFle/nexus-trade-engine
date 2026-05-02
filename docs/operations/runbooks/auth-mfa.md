# Runbook — Auth & MFA

**Alerts**: `AuthMFAFastBurn`, `AuthMFAMediumBurn`, `AuthMFASlowBurn`,
`AuthMFABudgetExhaustion`

**SLO**: 99.9% non-failure auth + MFA-verify outcomes over 28 days
([slos.md](../slos.md#critical-user-journeys)).

## What this means

Legitimate users cannot reliably log in. Auth is one nine higher than
general API availability because login is a binary cliff: a 1% failure
rate locks ≈ 1% of users out completely. **Page severity at every burn
rate.** Treat as a security-adjacent incident: do not investigate
quickly *and* sloppily.

## First 60 seconds

1. Open the **Nexus / SLO overview** dashboard. Check the "Auth & MFA"
   panel. Confirm the failure ratio is real, not a single noisy probe.
2. Try logging in with a known-good test account. If it works, the
   alert may be firing on a specific user cohort — not all users.
3. If it fails, capture the response body (redact sensitive fields)
   and the request id. Move to "Common causes → MFA verification
   broken".

## Triage

- **Is the failure on password verification or on MFA verification?**
  - `nexus_auth_attempts_total{outcome="failure"}` is split by neither
    today; look at engine logs for the request id to disambiguate.
  - The relevant call-sites are
    [`engine/api/routes/auth.py`](../../../engine/api/routes/auth.py)
    (password / login) and
    [`engine/api/auth/mfa_service.py`](../../../engine/api/auth/mfa_service.py)
    (`verify_login_code`, `verify_challenge`).
- **Spike correlated with a deploy?** Cross-reference the spike with
  release-please tag history. A botched MFA-encryption-key roll is the
  most damaging cause and is documented under "Common causes".
- **Spike correlated with a brute-force attempt?** Look at
  `nexus_auth_attempts_total{outcome="failure"}` partitioned by source
  IP if your edge proxy attaches it. A spike that doesn't move
  `outcome="success"` means the SLO is firing on probably-malicious
  traffic — the SLO definition deliberately includes failed attempts,
  so this is "working as designed" but the on-call still needs to
  decide whether to rate-limit harder.

## Common causes

- **MFA encryption key drift** — the running engine's
  `MFA_ENCRYPTION_KEY` does not match the one used when the user's
  secret was encrypted. Symptom: every MFA verify returns "failure"
  for users with `mfa_enabled = true`. Fix: restore the previous key
  from the secrets vault, see
  [`backup-and-recovery.md → Secrets`](../backup-and-recovery.md#secrets-and-keys).
  **Do not** rotate the key under load — it must be a coordinated
  re-encryption.
- **MFA challenge token expired** — challenge TTL is `mfa_challenge_ttl_seconds`
  (default 300 s). If users are slow to enter codes, this will look like
  failures. Consider raising the TTL if the SLO violation correlates
  with operator-set TTL.
- **Clock skew** — TOTP relies on synchronized clocks. NTP drift on
  hosts > ±1 step causes legitimate codes to be rejected. Verify with
  `chronyc tracking` or equivalent.
- **DB unavailable for the `users` table read path** — login fails to
  even fetch the user record. Treat as a DB incident
  ([`backup-and-recovery.md`](../backup-and-recovery.md)) and remediate
  there first.
- **Brute-force / credential-stuffing campaign** — failures dominated by
  one ASN / set of IPs. Engage the rate-limit / WAF surface; do not
  relax the SLO.

## Escalation

Auth failures touch security: page the security on-call alongside the
service on-call. Time-to-page should be **immediate** for the fast-burn
alert.

## Post-incident

- Capture how many user accounts were affected and for how long.
- If MFA secrets had to be re-encrypted, document the rotation in the
  on-call log and confirm the new key is in the secrets vault with
  cross-region replication.
- If the cause was a brute-force attempt, file a security advisory only
  if user accounts were actually compromised.
