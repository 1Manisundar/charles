Node: T1 Schema migration 2: expiry and custom-code columns
Why a human is needed: High-impact change: schema migration against a codebase with existing data; modifies protected path urlshortener/migrations.py.

Task T1: Schema migration 2: expiry and custom-code columns
Append an additive migration adding links.expires_at (nullable) and links.is_custom (default 0). Test the upgrade of a populated v1 database and the atomicity of a failing migration.

Files:
  - urlshortener/migrations.py
  - tests/test_migrations.py

Rollback plan: Application rollback is sufficient: redeploy 1.0.0. It reads rows by column name and ignores expires_at and is_custom, so the upgraded schema needs no down-migration. Links created with a lifetime would stop expiring and aliases keep working as ordinary codes. In this pipeline the change stays in the sandbox until release approval, and `sdlc rollback` restores the target directory.
