# Changelog

## 1.2.0 - Abuse protection and privacy-preserving analytics

- Per-client rate limits on link creation and on redirects (`429` + `Retry-After`).
- Destinations that are internal, disguised, self-referencing, credential-bearing
  or on the configured blocklist are refused with `422`.
- Statistics add daily `unique_visitors` and `top_referrers`.
- Privacy: visitors are counted with a keyed identifier that rotates daily and is
  never persisted in reversible form; referrers are stored as host only.
  **Behaviour change:** the full referrer URL is no longer stored.
- Schema migration 3 (additive: `clicks.visitor_hash`).

## 1.1.0 - Expiring links and custom aliases

- `POST /api/v1/links` accepts `expires_in_seconds`; an expired link answers `410 Gone`.
- `POST /api/v1/links` accepts `alias`; `409` if taken, `422` if malformed or reserved.
- Link responses include `expires_at` and `is_custom`.
- Schema migration 2 (additive: `links.expires_at`, `links.is_custom`) is applied
  automatically at start-up. Existing links and existing clients are unaffected.

## 1.0.0 - Initial release

- Create short links (`POST /api/v1/links`) and follow them (`GET /{code}`, 307).
- Link metadata, soft delete (`410 Gone` afterwards) and click analytics
  (total, last click, per-day breakdown).
- SQLite persistence with forward-only migrations applied at start-up.
- Liveness and readiness probes, RFC 7807 error bodies, request-id propagation.
