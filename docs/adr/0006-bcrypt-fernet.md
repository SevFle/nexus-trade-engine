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
   database must store these, but a *database-scoped* leak (SQL
   injection that exfiltrates table rows, a stolen DB backup, a read
   replica) must not be enough to recover them — because the
   decryption key is held *outside* the database. This is scoped to a
   SQL-injection-level DB leak, not a broad "read-only leak": an
   attacker who also reaches the application host / env var still
   recovers plaintext (see [Key Management](#key-management)).

Two ADRs already cover adjacent decisions: ADR-0002 picked the
`AuthBackend` / `RBAC` architecture, and ADR-0001 named Argon2id as
the *intended* password hash. This ADR locks in the *actual* choices
that shipped (bcrypt for verifiers, Fernet for recoverable secrets)
and the reasoning behind them, so future contributors don't relitigate
the conversation every PR.

## Decision Drivers

- **Defense against a DB-scoped leak.** A database-scoped leak (SQL
  injection that exfiltrates table rows, a stolen DB backup, a read
  replica) hands an attacker ciphertext only — the decryption key
  lives outside the database — so `users.mfa_secret_encrypted` and
  `secrets.ciphertext` must not yield plaintext, and
  `users.hashed_password` / `api_keys.key_hash` must not be
  crackable in useful time. The boundary is *DB-scoped*, not "any
  read-only leak"; an attacker who also reaches the env var / host
  still wins (see [Key Management](#key-management)).
- **Latency budget.** A login attempt runs password verify *and* (if
  MFA is on) TOTP verify, so we cap hashing at ~250 ms per attempt.
  This does **not** distinguish bcrypt from Argon2id: an
  OWASP-baseline Argon2id profile (time cost `t=2`, memory
  `m=64 MiB`, parallelism `p=1`) measures ~100–250 ms on commodity
  hardware — the same band as bcrypt cost factor 12 (~250 ms). Only
  *aggressive* Argon2id (e.g. `m=1 GiB`) pushes into the multi-second
  range, so latency alone is not a reason to prefer bcrypt; the
  decision turns on the drivers below.
- **Operational simplicity (reason to retain bcrypt).** bcrypt's cost
  factor is per-hash and transparently upgrades on the next login via
  `bcrypt.gensalt(rounds=N)`, so ratcheting cost upward is a lazy,
  row-by-row migration with no schema change. Combined with the
  login rate limiter (ADR-0005), which raises the cost of *online*
  attacks far more than any KDF raises *offline* attack cost, bcrypt
  is the cheapest safe default to operate today.
- **bcrypt 72-byte input ceiling.** bcrypt silently truncates to the
  first 72 bytes of a password, so any passphrase longer than that
  collapses to a shared prefix. This is a real driver, not a
  footnote: it weakens distinctness for long passphrases, and
  Argon2id has no such ceiling. We accept it because inputs are
  rejected at > 1 KiB at the request boundary and we pre-hash with
  SHA-256 before bcrypt if/when long passphrases appear (see
  Consequences).
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
    trade-off not on latency grounds (a tuned Argon2id profile is
    comparably fast — see Decision Drivers) but on operational ones:
    per-hash cost-factor upgrades and the login rate limiter
    (ADR-0005's Valkey-backed limiter), which raises the cost of
    online attacks far more than any KDF raises the cost of offline
    attacks.
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

## Key Management

This section makes the Fernet threat boundary and rotation procedure
explicit so operators don't have to reverse-engineer them from code.

### Where the keys live

- **MFA key** — `settings.mfa_encryption_key`, a url-safe-base64
  32-byte Fernet key. Loaded from the `MFA_ENCRYPTION_KEY`
  environment variable; an empty value disables MFA enrollment (see
  [`engine/config.py`](../../engine/config.py) and
  [`engine/api/auth/mfa_service.py`](../../engine/api/auth/mfa_service.py)).
- **Secret-store master key** — `settings.secrets_master_key`, same
  encoding. Loaded from `SECRETS_MASTER_KEY` and wrapped in a
  `MasterKey` (current + optional previous) in
  [`engine/core/secrets.py`](../../engine/core/secrets.py).
- **Production target.** Env-var injection is the default so a
  self-hosted operator can run MFA without a cloud account. For
  multi-tenant / hosted deployments the intended target is
  KMS-backed envelope encryption: the Fernet key becomes a
  data-encryption-key unwrapped at boot from a KMS (AWS KMS / GCP
  KMS / Vault Transit), so the long-lived key never sits in a `.env`
  file or container image. Tracked as a follow-up.

### Rotation procedure

The `MultiFernet` inside `SecretsService` is what makes rotation safe:

1. Generate a new 32-byte Fernet key with
   `engine.core.secrets.generate_master_key()`.
2. Promote it to current and demote the old current to `previous` via
   `SecretsService.rotate_master_key(new_current=…)`.
   `rotate_master_key` refuses to run if a `previous` key is already
   installed, so an unfinished migration cannot be silently orphaned.
3. Migrate ciphertext with `await SecretsService.reencrypt_all()`.
   This is two-phase: it decrypts *every* record first and aborts
   *before any write* if any record fails under either key, then
   re-encrypts under the new current and returns the count migrated.
4. Once a full run completes without raising, drop the previous key
   with `drop_previous_key()`. Do **not** drop `previous` earlier —
   a crash mid-flush leaves a mix of old/new ciphertext, and the
   previous key is the only thing that can still read the old ones.
5. The MFA key (`mfa_encryption_key`) rotates the same way at the row
   level: re-encrypt every `users.mfa_secret_encrypted` value with
   the new Fernet key while both old and new are mounted.

### Threat boundary (corrected)

Fernet defends against a **database-scoped** leak — an attacker who
exfiltrates database rows (SQL injection, a dumped `users` table, a
stolen DB backup) gets ciphertext only, and the key lives outside the
database, so plaintext is not recoverable from the dump alone.

It does **not** defend against a broader compromise in which the
attacker also obtains the key:

- Application-host compromise (read access to the process env or
  `/proc/<pid>/environ`).
- A leaked `.env` file, a baked-in container-image secret, or a
  config/secret-store read that exposes `MFA_ENCRYPTION_KEY` /
  `SECRETS_MASTER_KEY` alongside the DB dump.

In that combined scenario a DB dump + the env var is enough to
recover every MFA seed and secret-store value (already noted under
Negative Consequences). Scoping the claim to "SQL-injection-level DB
leak" rather than the looser "read-only DB leak" keeps the protection
honest; KMS-backed envelope encryption is the follow-up that narrows
the boundary further (the key never touches host disk).

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
