# URL Shortener - API reference

Version 1.2.0. Generated from the application's OpenAPI schema (`openapi.json`); do not edit by hand.

## Endpoints

### `GET /`

Service index

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | `ServiceInfo` |

### `POST /api/v1/links`

Create a short link

Request body: `CreateLinkRequest`

| Status | Meaning | Body |
|---|---|---|
| 201 | Successful Response | `LinkResponse` |
| 409 | The requested alias is already taken | `Problem` |
| 422 | The request was rejected by validation | `Problem` |
| 429 | Rate limit exceeded; retry after the Retry-After delay | `Problem` |
| 503 | No free short code could be allocated | `Problem` |

### `DELETE /api/v1/links/{code}`

Delete (deactivate) a link

| Parameter | In | Required | Type |
|---|---|---|---|
| `code` | path | yes | string |

| Status | Meaning | Body |
|---|---|---|
| 204 | Successful Response |  |
| 404 | No link with this code | `Problem` |
| 422 | Validation Error | `HTTPValidationError` |

### `GET /api/v1/links/{code}`

Get a link's metadata

| Parameter | In | Required | Type |
|---|---|---|---|
| `code` | path | yes | string |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | `LinkResponse` |
| 404 | No link with this code | `Problem` |
| 422 | Validation Error | `HTTPValidationError` |

### `GET /api/v1/links/{code}/stats`

Get a link's click analytics

| Parameter | In | Required | Type |
|---|---|---|---|
| `code` | path | yes | string |

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | `StatsResponse` |
| 404 | No link with this code | `Problem` |
| 422 | Validation Error | `HTTPValidationError` |

### `GET /healthz`

Liveness probe

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | `HealthResponse` |

### `GET /readyz`

Readiness probe

| Status | Meaning | Body |
|---|---|---|
| 200 | Successful Response | `HealthResponse` |
| 503 | The database is not reachable | `Problem` |

### `GET /{code}`

Follow a short link

| Parameter | In | Required | Type |
|---|---|---|---|
| `code` | path | yes | string |

| Status | Meaning | Body |
|---|---|---|
| 307 | Successful Response |  |
| 404 | No link with this code | `Problem` |
| 410 | The link was deleted or has expired | `Problem` |
| 422 | Validation Error | `HTTPValidationError` |
| 429 | Rate limit exceeded; retry after the Retry-After delay | `Problem` |

## Schemas

### `CreateLinkRequest`

| Field | Type | Required | Description |
|---|---|---|---|
| `alias` | string | null | no | Custom short code: 3-32 letters, digits, '-' or '_'. Omit for a random code. |
| `expires_in_seconds` | integer | null | no | Lifetime in seconds (60 to 31536000). Omit for a link that never expires. |
| `url` | string | yes | Absolute http(s) URL to shorten. |

### `DailyClicks`

| Field | Type | Required | Description |
|---|---|---|---|
| `clicks` | integer | yes | Redirects served on that day. |
| `date` | string | yes | Calendar day (UTC), YYYY-MM-DD. |
| `unique_visitors` | integer | no | Distinct visitors that day, counted with anonymous identifiers that change daily. |

### `HTTPValidationError`

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | ValidationError[] | no |  |

### `HealthResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | integer | null | no | Applied database schema version. |
| `status` | string | yes | 'ok' when the process is alive, 'ready' when it can serve traffic. |

### `LinkResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `code` | string | yes | Short code identifying the link. |
| `created_at` | string | yes | Creation time (UTC). |
| `expires_at` | string | null | no | When the link stops redirecting; null if never. |
| `is_active` | boolean | yes | False once the link has been deleted. |
| `is_custom` | boolean | no | True if the code was chosen by the caller. |
| `long_url` | string | yes | Destination URL. |
| `short_url` | string | yes | Public URL that redirects to long_url. |

### `Problem`

Error body (RFC 7807, served as application/problem+json).

| Field | Type | Required | Description |
|---|---|---|---|
| `detail` | string | yes | Explanation specific to this occurrence. |
| `status` | integer | yes | HTTP status code. |
| `title` | string | yes | Short, stable summary of the problem. |
| `type` | string | no | URI identifying the problem type. |

### `ReferrerCount`

| Field | Type | Required | Description |
|---|---|---|---|
| `clicks` | integer | yes | Redirects that arrived from that host. |
| `host` | string | yes | Host of the referring page. |

### `ServiceInfo`

| Field | Type | Required | Description |
|---|---|---|---|
| `docs` | string | yes | Path of the interactive API documentation. |
| `health` | string | yes | Path of the liveness probe. |
| `openapi` | string | yes | Path of the OpenAPI schema. |
| `service` | string | yes | Name of the service. |
| `version` | string | yes | Running version. |

### `StatsResponse`

| Field | Type | Required | Description |
|---|---|---|---|
| `clicks_by_day` | DailyClicks[] | yes | Daily redirect counts for the recent window. |
| `code` | string | yes | Short code the statistics belong to. |
| `last_clicked_at` | string | null | yes | Time of the most recent redirect, if any. |
| `top_referrers` | ReferrerCount[] | no | Most frequent referring hosts since the link was created. |
| `total_clicks` | integer | yes | Redirects served since the link was created. |

### `ValidationError`

| Field | Type | Required | Description |
|---|---|---|---|
| `ctx` | object | no |  |
| `input` |  | no |  |
| `loc` | string | integer[] | yes |  |
| `msg` | string | yes |  |
| `type` | string | yes |  |
