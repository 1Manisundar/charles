Node: Task decomposition
Why a human is needed: The design and plan must be approved before any code is changed.

Design: Three independent additions at the edges of the existing layers. A token-bucket limiter is attached to the create and redirect routes as a dependency. A destination check joins the existing URL validation in the service. Click recording gains a visitor identifier so statistics can count unique visitors and rank referrers.

Decisions:
  - ADR-11 In-process token bucket: Keep one token bucket per client address in process memory, with a hard cap on tracked clients (least recently seen evicted). Separate limiters for creation and redirects.
  - ADR-12 Client address from X-Forwarded-For: Use the first address in X-Forwarded-For when the header is present, otherwise the socket address.
  - ADR-13 Rule-based destination vetting inside URL validation: Reject credentials in the URL, non-public addresses in every spelling browsers accept, internal host names, the service's own host and blocklisted domains. No DNS resolution.
  - ADR-14 Unique visitors by hashing address and user agent: Store SHA-256(ip + user agent) with each click and count distinct hashes per day.
  - ADR-15 Referrer reduced to host: Store only the referring host from now on; aggregate older full-URL rows by host when reading.

Data model changes:
  - clicks: ADD COLUMN visitor_hash TEXT NULL - SHA-256 of the visitor's IP address and user agent [MIGRATION]

Risks:
  - R20 (medium/medium) Rate limits throttle legitimate users who share an address (offices, mobile carriers). -> Generous redirect default, configurable limits, clear 429 with Retry-After.
  - R21 (low/medium) The destination rules reject legitimate links. -> Rules target only non-public and disguised destinations; tested against ordinary public URLs.
  - R22 (low/high) Schema migration on the click log. -> Additive nullable column; change-control approval before the task runs; covered by the existing upgrade tests.
  - R23 (medium/medium) Limiter memory grows with the number of distinct clients. -> Hard cap on tracked clients with least-recently-seen eviction.

Existing modules impacted:
  - tests/test_config.py
  - tests/test_service.py
  - urlshortener/__init__.py
  - urlshortener/api.py
  - urlshortener/config.py
  - urlshortener/main.py
  - urlshortener/migrations.py
  - urlshortener/repository.py
  - urlshortener/schemas.py
  - urlshortener/service.py

Tasks:
  - T1 [config] Hardening settings (after: -) writes ['urlshortener/config.py', 'tests/test_config.py']
  - T2 [code] Token-bucket rate limiter (after: T1) writes ['urlshortener/ratelimit.py', 'tests/test_ratelimit.py']
  - T3 [code] Destination vetting (after: T1) writes ['urlshortener/safety.py', 'tests/test_safety.py']
  - T4 [migration] Visitor analytics storage (after: T1) writes ['urlshortener/migrations.py', 'urlshortener/repository.py', 'tests/test_analytics_repository.py']
  - T5 [code] Wire limits, vetting and analytics into the service and API (after: T2, T3, T4) writes ['urlshortener/service.py', 'urlshortener/schemas.py', 'urlshortener/api.py', 'urlshortener/main.py', 'urlshortener/__init__.py', 'tests/test_service.py', 'tests/test_api_hardening.py']

Rollback plan: Redeploy 1.1.0: it ignores clicks.visitor_hash and the new settings. Limits and destination checks disappear with the code; no data needs repair. In this pipeline nothing reaches the target before release approval, and `sdlc rollback` restores it afterwards.
