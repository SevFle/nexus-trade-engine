# ADR-0006: Password hashing (bcrypt) and MFA secret at rest (Fernet)

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: Lead maintainer + 1 reviewer
- **Tags**: auth, security, crypto, mfa

## Context and Problem Statement

The engine stores two kinds of secret material in the database:

1. **Verifier material** — passwords (local auth provider) and
   long-lived API keys. The database only ever needs to *check* these,
   never recover the plaintext. The right shape is a slow,
   salt-embedded, deliberately expensive hash.
2. **Recoverable secrets at rest** — TOTP seeds for MFA (we need the
   plaintext at verification time to compute the expected code) and
   the master key material for the engine's encrypted secret store
   ([`engine/core/secrets.py`](../../engine/core/secrets.py)). The
   database must store these, but a read-only DB leak (SQL dump,
   backup tape,Snapshot) must not be enough to recover them.

Two ADRs already cover adjacent decisions: ADR-0002 picked the
`AuthBackend` / `RBAC` architecture, and ADR-0001 named Argon2id as
the *intended* password hash. This ADR locks in the *actual* choices
that shipped (bcrypt for verifiers, Fernet for recoverable secrets)
and the reasoning behind them, so future contributors don't relitigate
the conversation every PR.

## Decision Drivers

- **Defense against a DB-only leak.** A read-only snapshot of
  `users.hashed_password`, `users.mfa_secret_encrypted`,
  `api_keys.key_hash`, or `secrets.ciphertext` must not give an
  attacker an authentication path.
- **Latency budget.** A login attempt goes through password verify
  *and* (if MFA is on) TOTP verify. We can spend ~250 ms hashing on
  registration / verify, not the multi-second cost of an aggressive
  Argon2id profile.
- **Implementation surface.** Both choices should be one import from
  the Python cryptography ecosystem, with no hand-rolled primitives.
- **Rotation story.** Recoverable secrets need master-key rotation
  (operator rekeys the engine, every ciphertext is migrated). The
  primitive we pick has to support that without rewriting rows by
  hand.
- **Auditability.** Both choices should be conventional enough that
  an outside auditor recognises them on sight, not bespoke.

## Considered Options

### For verifier material (passwords, API keys, backup codes)

1. **bcrypt with cost factor 12** — Blowfish-based EksBlowfish, the
   historical Python ecosystem default (`bcrypt>=4`). Cost 12 ≈
   ~250 ms on commodity hardware today.
2. **Argon2id (libargon2 via `argon2-cffi`)** — PHC winner. Resists
   GPU/ASIC cracking better than bcrypt; recommended by OWASP.
3. **scrypt** — older KDF, resists GPU attacks, but parameter space
   is harder to reason about and the Python bindings are less
   maintained than Argon2's.
4. **PBKDF2-HMAC-SHA-256 (high iterations)** — stdlib-only; safe but
   not GPU-hard. Acceptable fallback, not first choice.

### For recoverable secrets at rest (TOTP seeds, master key material)

1. **Fernet (`cryptography.fernet`)** — AES-128-CBC + HMAC-SHA-256,
   authenticated, key rotation via `MultiFernet`. One-line encrypt /
   decrypt.
2. **AES-GCM via `cryptography.hazmat`** — same primitives, but you
   have to manage nonces, AAD, and tag concatenation yourself.
3. **NaCl SecretBox (`pynacl`)** — XSalsa20-Poly1305. Excellent
   primitive but adds a C dependency and a second crypto library for
   one feature.
4. **Application-level envelope encryption with KMS** — right answer
   at scale, but we have no KMS today. Tracked as a follow-up.

## Decision Outcome

Chosen options:

- **bcrypt with cost factor 12** for `users.hashed_password`,
  `api_keys.key_hash`, and `users.mfa_backup_codes`. Implemented in
  [`engine/api/auth/local.py`](../../engine/api/auth/local.py),
  [`engine/api/auth/api_keys.py`](../../engine/api/auth/api_keys.py),
  and
  [`engine/api/auth/mfa_service.py`](../../engine/api/auth/mfa_service.py).
- **Fernet (`cryptography.fernet.Fernet`) keyed by
  `settings.mfa_encryption_key`** for the TOTP secret at rest, and
  **`MultiFernet`** for the application-level secret store in
  [`engine/core/secrets.py`](../../engine/core/secrets.py) so the
  operator can rotate the master key without rewriting every row in
  one transaction.

Cost factor 12 is a deliberate midpoint: it costs an attacker roughly
the same to brute-force a 12-character password today as a 16-character
password hashed at cost 10. We will bump the cost factor in a follow-up
migration as hardware moves; bcrypt's `gensalt(rounds=N)` makes that a
row-by-row rewrite on next login, not a schema change.

For Fernet, the key is loaded from `settings.mfa_encryption_key`
(MFA) and `settings.secrets_master_key` (secret store), both
url-safe-base64 32-byte Fernet keys. Key rotation is exposed in
[`engine/core/secrets.py`](../../engine/core/secrets.py) via
`SecretsService.rotate_master_key` followed by `reencrypt_all`; the
`MultiFernet` accepts the previous key for decrypt during the
migration window.

### Consequences

- **Positive**
  - bcrypt is well-known to any auditor. No surprises in a security
    review.
  - Fernet's `MultiFernet` gives us free decrypt-with-old /
    encrypt-with-new during rotation. No bespoke key-version field.
  - Both libraries are pure wrappers around OpenSSL / libffi — no
    hand-rolled crypto, no exotic C dependencies.
- **Negative**
  - bcrypt is GPU-friendly compared to Argon2id. We accept the
    trade-off because the latency budget is real and because rate
    limiting on the login endpoint (see ADR-0005's Valkey-backed
    limiter) raises the cost of online attacks far more than any KDF
    can raise the cost of offline attacks.
  - Fernet is AES-128, not AES-256. We're comfortable with the
    128-bit security margin; if AES-128 falls, this project has
    bigger problems than MFA seeds.
  - The MFA and secret-store keys live in environment variables. A
    host with the env var and a DB dump still recovers the plaintext.
    KMS-backed envelope encryption is the eventual answer and is
    tracked as a follow-up.
- **Neutral**
  - Cost factor 12 will need bumping as CPUs improve. We track that
    in the [auth-mfa runbook](../operations/runbooks/auth-mfa.md).

## Pros and Cons of the Options

### bcrypt (chosen, for verifiers)

- **Pros**
  - Universally supported; the Python `bcrypt` package is mature.
  - Cost factor is per-hash — migrating to a higher cost happens
    transparently on next login.
  - Salt is embedded in the hash; no separate column.
- **Cons**
  - GPU-friendly relative to Argon2id.
  - 72-byte password truncation. We mitigate by pre-hashing long
    inputs with SHA-256 before bcrypt-hashing the digest if/when we
    see passwords exceed that ceiling. (Not done today because we
    reject > 1 KiB at the request boundary.)

### Argon2id

- **Pros**
  - PHC winner; memory-hard.
  - OWASP-recommended.
- **Cons**
  - Roughly 2-5× the latency per verify at the memory levels that
    matter for GPU resistance.
  - Less universally recognised by auditors in the Python ecosystem,
    though this is changing.
  - Migration path *off* bcrypt is harder than migration path *onto*
    Argon2id; ship bcrypt first, swap when we have a benchmark.

### scrypt / PBKDF2

- **Pros**
  - PBKDF2 is stdlib-only.
- **Cons**
  - scrypt's parameter space is a footgun.
  - PBKDF2 is GPU-friendly; not worth the trade against bcrypt today.

### Fernet (chosen, for recoverable secrets)

- **Pros**
  - Authenticated encryption by default. Tampered ciphertext is
    rejected.
  - `MultiFernet` ships rotation primitives.
  - One-line encrypt/decrypt — nothing to get wrong at the call site.
- **Cons**
  - AES-128-CBC + HMAC rather than AEAD. Two-pass but well within
    budget at this scale.
  - Key is a single 32-byte value; no key-splitting built in.

### AES-GCM (raw)

- **Pros**
  - Modern AEAD; one pass.
- **Cons**
  - Nonce management is the caller's problem; nonce reuse breaks the
    scheme.
  - More code at every call site — easy to get wrong.

### NaCl SecretBox

- **Pros**
  - Excellent primitive; hard to misuse.
- **Cons**
  - Adds `pynacl` and a libsodium dependency for one feature.
  - No `MultiFernet`-style rotation helper; we'd write it.

### Envelope encryption with KMS

- **Pros**
  - Right answer at scale; per-record data-encryption-keys, KMS-backed
    master.
- **Cons**
  - No KMS in this project today. Operators of a self-hosted engine
    shouldn't need a cloud KMS to run MFA. Tracked as a follow-up.

## Links

- Related code:
  - [`engine/api/auth/local.py`](../../engine/api/auth/local.py) —
    `bcrypt.hashpw` / `checkpw` for passwords.
  - [`engine/api/auth/api_keys.py`](../../engine/api/auth/api_keys.py)
    — bcrypt for the API-key `key_hash`.
  - [`engine/api/auth/mfa_service.py`](../../engine/api/auth/mfa_service.py)
    — Fernet for the TOTP secret at rest; bcrypt for backup codes.
  - [`engine/core/secrets.py`](../../engine/core/secrets.py) —
    `MultiFernet`-based secret store with master-key rotation.
  - [`engine/config.py`](../../engine/config.py) — `mfa_encryption_key`
    and `secrets_master_key` settings.
- Related ADRs:
  - [ADR-0001 — Scaffold technology choices](0001-scaffold-tech-choices.md)
    (originally named Argon2id; this ADR records the bcrypt choice
    that actually shipped).
  - [ADR-0002 — Auth & RBAC model](0002-auth-rbac.md).
- Related runbook:
  [`docs/operations/runbooks/auth-mfa.md`](../operations/runbooks/auth-mfa.md).
- Supersedes: —
- Superseded by: —
- External references:
  - [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)
  - [Fernet spec](https://github.com/fernet/spec)
  - [bcrypt cost factor recommendations](https://www.usenix.org/conference/usenixsecurity19/presentation/pedicini)
