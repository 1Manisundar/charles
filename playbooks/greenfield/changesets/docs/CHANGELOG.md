# Changelog

## 1.0.0 - Initial release

- Create short links (`POST /api/v1/links`) and follow them (`GET /{code}`, 307).
- Link metadata, soft delete (`410 Gone` afterwards) and click analytics
  (total, last click, per-day breakdown).
- SQLite persistence with forward-only migrations applied at start-up.
- Liveness and readiness probes, RFC 7807 error bodies, request-id propagation.
