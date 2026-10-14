# URL Shortener

A small HTTP service that turns long URLs into short links, redirects visitors,
and reports how often each link is used.

## Run

```bash
pip install -r requirements.txt
python -m uvicorn urlshortener.main:create_app --factory --port 8000
```

or with Docker:

```bash
docker build -t urlshortener .
docker run -p 8000:8000 -v urlshortener-data:/data urlshortener
```

## Use

```bash
# create a short link
curl -s -X POST localhost:8000/api/v1/links \
     -H 'content-type: application/json' \
     -d '{"url": "https://example.com/a/very/long/path"}'
# {"code":"q3ZxA1b","short_url":"http://localhost:8000/q3ZxA1b",...}

curl -i localhost:8000/q3ZxA1b                      # 307 redirect to the long URL
curl -s localhost:8000/api/v1/links/q3ZxA1b         # metadata
curl -s localhost:8000/api/v1/links/q3ZxA1b/stats   # click analytics
curl -X DELETE localhost:8000/api/v1/links/q3ZxA1b  # deactivate; the link now answers 410

# choose the code yourself and let the link expire after a day
curl -s -X POST localhost:8000/api/v1/links \
     -H 'content-type: application/json' \
     -d '{"url": "https://example.com/spring", "alias": "spring-sale", "expires_in_seconds": 86400}'
```

The full contract is in [docs/API.md](docs/API.md) and `openapi.json` (both
generated from the code). Interactive docs are served at `/docs`; the root path
`/` returns a small index that points there.

## Behaviour worth knowing

- **Redirects are `307` with `Cache-Control: no-store`**, so browsers revisit the
  service and every click is counted.
- **Codes are 7 random base62 characters** (about 3.5 trillion possibilities),
  not sequential ids, so links cannot be enumerated.
- **Deleting is a soft delete.** The link stops redirecting (`410 Gone`) but its
  metadata and analytics remain.
- **Links can expire.** `expires_in_seconds` (60 seconds to 365 days) sets a
  lifetime; from the expiry instant on the link answers `410 Gone`, while its
  metadata and analytics stay available. Links created without it never expire.
- **Codes can be chosen.** `alias` (3-32 letters, digits, `-` or `_`) is used as
  the code. A taken alias is `409 Conflict`; reserved names such as `api` or
  `docs` are refused. Aliases are case-sensitive and, like every code, are never
  reused - not even after the link is deleted.
- **Requests are rate limited per client.** Link creation and redirects have
  separate per-minute budgets; beyond them the service answers `429` with a
  `Retry-After` header. Probes and read-only API calls are not limited.
- **Unsafe destinations are refused.** A URL that points at a loopback, private
  or link-local address (in any spelling), at an internal host name, at this
  service itself, at a blocked domain, or that carries `user@` credentials is
  rejected with `422`. The check is syntactic and does not resolve DNS.
- **Analytics hold no personal data.** Unique visitors are counted with an
  identifier derived from address and user agent under a random key that lives
  only in memory and changes every day, so it cannot be reversed or linked across
  days. Only the host of the referrer is stored. Addresses are never logged.
- **Errors are RFC 7807 problem documents** (`application/problem+json`).
- **Every response carries `X-Request-ID`**; a well-formed id sent by the caller
  is propagated, anything else is replaced.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `URLSHORTENER_DB_PATH` | `urlshortener.db` | SQLite database file |
| `URLSHORTENER_BASE_URL` | `http://localhost:8000` | Public origin used to build `short_url` |
| `URLSHORTENER_CODE_LENGTH` | `7` | Length of generated codes |
| `URLSHORTENER_MAX_URL_LENGTH` | `2048` | Longest URL accepted |
| `URLSHORTENER_STATS_DAYS` | `30` | Window of the per-day click breakdown |
| `URLSHORTENER_RATE_LIMIT_CREATE_PER_MINUTE` | `30` | Link creations per client per minute; `0` disables |
| `URLSHORTENER_RATE_LIMIT_REDIRECT_PER_MINUTE` | `600` | Redirects per client per minute; `0` disables |
| `URLSHORTENER_TRUST_FORWARDED_FOR` | `false` | Set to `true` only behind exactly one trusted proxy that sets `X-Forwarded-For` |
| `URLSHORTENER_BLOCKED_DOMAINS` | *(empty)* | Comma-separated domains (and their subdomains) that may not be shortened |

## Operations

- `GET /healthz` - liveness; does not touch the database.
- `GET /readyz` - readiness; `503` when the database is unreachable.
- Schema migrations are forward-only and applied automatically at start-up
  (`urlshortener/migrations.py`).
- Rate-limit state and the visitor-identifier key are held in process memory.
  With several instances each one enforces its own limits and counts its own
  visitors; move both to a shared store before scaling out.

## Layout

```
urlshortener/
  main.py         application factory, request-id middleware, health probes
  api.py          routes and error mapping
  schemas.py      request/response models (the API contract)
  service.py      business rules (no HTTP, no SQL)
  repository.py   all SQL, always parameterised
  db.py           SQLite connection, transactions, migration runner
  migrations.py   versioned schema
  codec.py        short-code generation and alias rules
  ratelimit.py    token-bucket limiter
  safety.py       destination vetting
  privacy.py      anonymous visitor identifiers, referrer reduction
  config.py       settings from the environment
tests/            unit and API tests
```

## Test

```bash
python -m pytest --cov=urlshortener
```
