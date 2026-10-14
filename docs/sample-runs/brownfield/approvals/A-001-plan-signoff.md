Node: Task decomposition
Why a human is needed: The design and plan must be approved before any code is changed.

Design: The change threads two optional inputs through every existing layer without adding new ones: the schema gains two nullable/defaulted columns, the repository reads and writes them, the service validates them and enforces expiry lazily at redirect time, and the API contract exposes them. The redirect path gains one in-memory comparison and no extra query.

Decisions:
  - ADR-6 Relative lifetime in, absolute expiry stored: Accept expires_in_seconds, compute expires_at once at creation from the server clock and store that.
  - ADR-7 Lazy expiry, inclusive of the expiry instant: Check expiry when the link is resolved; a link is gone when now >= expires_at. No background job.
  - ADR-8 Aliases share the code namespace: Store the alias in links.code. The primary key gives uniqueness atomically; a conflict is reported as 409 and never retried or substituted.
  - ADR-9 Reserved names instead of a separate URL prefix: Refuse a case-insensitive list of reserved names, and keep service routes registered before the catch-all redirect route.
  - ADR-10 Additive migration only: Migration 2 only adds a nullable column and a defaulted column; no rewrite, no backfill.

Data model changes:
  - links: ADD COLUMN expires_at TEXT NULL; ADD COLUMN is_custom INTEGER NOT NULL DEFAULT 0 [MIGRATION]

Risks:
  - R10 (low/high) The schema migration damages or loses existing links. -> Additive DDL only, applied in one transaction, tested against a populated v1 database and against a failing migration; requires change-control approval before the task runs.
  - R11 (medium/medium) An off-by-one at the expiry boundary lets a link live too long or die too early. -> The rule is a single function with tests one microsecond before and exactly at the expiry instant.
  - R12 (medium/medium) An alias shadows a service route or squats on a brand name. -> Reserved-name list and route ordering; ownership and moderation of aliases need authentication and are out of scope.
  - R13 (low/low) Widening the code syntax changes behaviour for existing clients. -> Only lookups of previously malformed codes change (still 404, now after a database miss); covered by the updated codec test.

Existing modules impacted:
  - tests/test_codec.py
  - urlshortener/__init__.py
  - urlshortener/api.py
  - urlshortener/codec.py
  - urlshortener/migrations.py
  - urlshortener/repository.py
  - urlshortener/schemas.py
  - urlshortener/service.py

Tasks:
  - T1 [migration] Schema migration 2: expiry and custom-code columns (after: -) writes ['urlshortener/migrations.py', 'tests/test_migrations.py']
  - T2 [code] Alias validation (after: -) writes ['urlshortener/codec.py', 'tests/test_codec.py']
  - T3 [code] Expiry and aliases in repository and service (after: T1, T2) writes ['urlshortener/repository.py', 'urlshortener/service.py', 'tests/test_expiry_alias.py']
  - T4 [code] API contract for expiry and aliases (after: T3) writes ['urlshortener/schemas.py', 'urlshortener/api.py', 'urlshortener/__init__.py', 'tests/test_api_expiry_alias.py']

Rollback plan: Application rollback is sufficient: redeploy 1.0.0. It reads rows by column name and ignores expires_at and is_custom, so the upgraded schema needs no down-migration. Links created with a lifetime would stop expiring and aliases keep working as ordinary codes. In this pipeline the change stays in the sandbox until release approval, and `sdlc rollback` restores the target directory.
