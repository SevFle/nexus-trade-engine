# MFA API

Mounted at `/api/v1/auth/mfa`. Implementation:
`engine/api/routes/mfa.py`. Service logic:
`engine/api/auth/mfa_service.py`.

TOTP-based MFA. TOTP secrets are Fernet-encrypted at rest with
`NEXUS_MFA_ENCRYPTION_KEY` (empty disables enrollment; existing
enrolled users can still verify). Backup codes are bcrypt-hashed.

## POST /enroll

Begin enrollment. Returns the shared secret + an `otpauth://` URI
suitable for QR-code rendering.

**Auth** — required. The caller must not already have MFA enabled.

**Response** `EnrollResponse`:
```json
{
  "secret": "JBSWY3DPEHPK3PXP",
  "otpauth_uri": "otpauth://totp/Nexus:alice@example.com?secret=...&issuer=Nexus"
}
```

**Errors** — `409` if MFA already enabled; `503` if the Fernet key is
not configured.

## POST /enroll/confirm

Confirm enrollment by supplying a valid 6-digit code generated from
the secret. On success the secret is persisted (encrypted) and ten
one-time backup codes are returned.

**Auth** — required.

**Request body** `ConfirmRequest`:
```json
{ "secret": "JBSWY3DPEHPK3PXP", "code": "123456" }
```

**Response** `ConfirmResponse`:
```json
{ "backup_codes": ["11223344", "22114433", ...] }
```

The plaintext backup codes are returned exactly once. Lost codes are
unrecoverable — regenerate them via `/backup-codes/regen`.

## POST /verify

Complete an MFA-challenged login. The `challenge_token` comes from
`POST /api/v1/auth/login` when the user has MFA enabled.

**Auth** — none (the challenge token proves identity).

**Request body** `VerifyRequest`:
```json
{ "challenge_token": "<short-lived jwt>", "code": "123456" }
```

The `code` is either a TOTP code from the authenticator app or a
backup code (8-character alphanumeric). Backup codes are single-use;
a fresh batch is persisted if one is consumed.

**Response** — `TokenResponse` (access + refresh), same shape as
`POST /auth/login`.

**Errors** — `400` if MFA not enabled, `401` on bad code or expired
challenge, `500` if the Fernet key is misconfigured.

## POST /disable

Disable MFA. Requires re-authentication with both password and a
current TOTP/backup code.

**Auth** — required (interactive session).

**Request body** `DisableRequest`:
```json
{ "password": "...", "code": "123456" }
```

**Response** — `200 OK` `{"status": "disabled"}`.

**Errors** — `400` if MFA not enabled or the user has no password
(federated users cannot disable via password — they must re-enroll or
contact an admin); `401` on bad password or code.

## POST /backup-codes/regen

Generate a fresh batch of backup codes after verifying identity with a
current TOTP code (or one of the remaining backup codes).

**Auth** — required.

**Request body** `RegenBackupCodesRequest`:
```json
{ "code": "123456" }
```

**Response** `ConfirmResponse` — same as `/enroll/confirm`. Old backup
codes are invalidated atomically.

## Operational notes

- The Fernet key must be 32 url-safe base64 bytes. Generate one with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Challenge tokens live for `NEXUS_MFA_CHALLENGE_TTL_SECONDS` (default
  300s).
- Backup-code count is `NEXUS_MFA_BACKUP_CODES_COUNT` (default 10).
- MFA disable for federated-only accounts is intentionally not
  supported by the API surface — the route requires a password to
  prove interactive ownership. Operators who need to reset MFA for a
  federated user must do so in the database.
