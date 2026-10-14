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

## Operations

- `GET /healthz` - liveness; does not touch the database.
- `GET /readyz` - readiness; `503` when the database is unreachable.
- Schema migrations are forward-only and applied automatically at start-up
  (`urlshortener/migrations.py`).

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
  codec.py        short-code generation
  config.py       settings from the environment
tests/            unit and API tests
```

## Test

```bash
python -m pytest --cov=urlshortener
```
