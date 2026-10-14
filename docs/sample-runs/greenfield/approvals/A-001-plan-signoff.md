Node: Task decomposition
Why a human is needed: The design and plan must be approved before any code is changed.

Design: A layered FastAPI application: an HTTP layer that only translates requests and errors, a service layer that owns the business rules, and a repository that owns all SQL against an embedded SQLite database. The application is built by a factory so tests and deployments inject their own settings.

Decisions:
  - ADR-1 FastAPI on SQLite behind a repository seam: Use FastAPI with the standard-library sqlite3 driver. Only repository.py and db.py know SQL.
  - ADR-2 Random base62 codes with database-enforced uniqueness: Generate 7 random base62 characters from the OS CSPRNG; rely on the primary key for uniqueness and retry a collision up to 5 times.
  - ADR-3 307 redirects that are never cached: Answer GET /{code} with 307 and Cache-Control: no-store.
  - ADR-4 Soft delete: DELETE marks the link inactive; redirects then answer 410 Gone.
  - ADR-5 Forward-only migrations applied at start-up: Keep an ordered list of migrations; apply the pending ones transactionally on start and record them in schema_migrations.

Data model changes:
  - links: New table: code (PK), long_url, created_at, is_active [MIGRATION]
  - clicks: New table: id, code (FK), clicked_at, referrer; index on (code, clicked_at) [MIGRATION]
  - schema_migrations: New table recording applied migration versions [MIGRATION]

Risks:
  - R1 (medium/medium) SQLite allows a single writer; redirect bursts queue behind click inserts. -> WAL mode and a busy timeout; the repository seam allows moving to a server database or buffering clicks.
  - R2 (medium/high) The service can be used to disguise malicious destinations (open redirect by design). -> Only http(s) URLs are accepted in v1; destination vetting and rate limiting are explicitly out of scope and tracked as follow-up.
  - R3 (high/low) The clicks table grows without bound. -> Indexed by (code, clicked_at); the stats window is bounded. Retention or roll-ups are a follow-up.
  - R4 (high/medium) Unauthenticated API allows anyone to create or delete links. -> Assumption A1: deployed behind a trusted gateway. Authentication is out of scope for v1.

Tasks:
  - T1 [config] Project skeleton and configuration (after: -) writes ['urlshortener/__init__.py', 'urlshortener/config.py', 'requirements.txt', 'pytest.ini', 'tests/test_config.py']
  - T2 [code] Short-code codec (after: T1) writes ['urlshortener/codec.py', 'tests/test_codec.py']
  - T3 [code] Persistence layer (after: T1) writes ['urlshortener/migrations.py', 'urlshortener/db.py', 'urlshortener/repository.py', 'tests/test_repository.py']
  - T4 [code] Link service (after: T2, T3) writes ['urlshortener/service.py', 'tests/test_service.py']
  - T5 [code] HTTP API and application factory (after: T4) writes ['urlshortener/schemas.py', 'urlshortener/api.py', 'urlshortener/main.py', 'tests/conftest.py', 'tests/test_api.py', 'tests/test_ops.py']
  - T6 [config] Container packaging (after: T1) writes ['Dockerfile', '.dockerignore']

Rollback plan: Greenfield: nothing exists to regress. The change is only promoted to the target directory after release approval, and `sdlc rollback` removes the promoted files again.
