Node: T4 Privacy-preserving visitor analytics
Why a human is needed: High-impact change: schema migration against a codebase with existing data; modifies protected path urlshortener/migrations.py.

Task T4: Privacy-preserving visitor analytics
A privacy module producing visitor identifiers as an HMAC under a random in-memory key that rotates daily, and reducing referrers to their host. Migration 3 adds clicks.visitor_hash for that opaque identifier; the repository stores it and aggregates unique visitors per day and clicks per referrer.

Files:
  - urlshortener/privacy.py
  - urlshortener/migrations.py
  - urlshortener/repository.py
  - tests/test_privacy.py

Rollback plan: Redeploy 1.1.0: it ignores clicks.visitor_hash and the new settings. Limits and destination checks disappear with the code; no data needs repair. In this pipeline nothing reaches the target before release approval, and `sdlc rollback` restores it afterwards.
