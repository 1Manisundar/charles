# Engineering summary: URL shortener service v1

- Run: `greenfield` (greenfield scenario, provider `scripted`)
- Status: **succeeded**
- Started 2026-10-14 00:32:35, last update 2026-10-14 00:32:38
- Target: `demo/sample/service` (promoted)

## 1. Requirement and interpretation

Raw requirement as received:

> Build a URL shortener service from scratch. A client submits a long URL and gets a short link back; opening the short link redirects to the original URL. Links can be looked up and removed. We need basic analytics on how often each link is used, and the service has to be reliable enough to run in production: health checks, sensible error handling, and data that survives a restart.

**Normalised problem.** A stateless HTTP API backed by a persistent store that creates short links, redirects visitors to the original URL, exposes link metadata and click analytics, and is operable in production (probes, structured errors, durable data).

| ID | Requirement | Priority | Acceptance criteria |
|---|---|---|---|
| FR-1 | A client can create a short link for an http(s) URL. | must | POST /api/v1/links with a valid URL returns 201 with code, short_url, long_url and created_at<br>The code is 7 base62 characters and unique<br>A missing, malformed, non-http(s) or over-long URL returns 422 with a problem document |
| FR-2 | Opening a short link redirects to the original URL. | must | GET /{code} returns 307 with the long URL in Location<br>An unknown code returns 404 |
| FR-3 | A client can look up a link's metadata. | must | GET /api/v1/links/{code} returns the stored link<br>An unknown code returns 404 |
| FR-4 | A client can remove a link. | must | DELETE /api/v1/links/{code} returns 204 and is idempotent<br>A removed link answers 410 on redirect and keeps its metadata and analytics |
| FR-5 | The service reports how often each link is used. | must | Every successful redirect is recorded with its time and referrer<br>GET /api/v1/links/{code}/stats returns total clicks, the last click time and a per-day breakdown |
| NFR-1 | Reliability: the service is operable in production. | must | Liveness and readiness probes exist; readiness fails when the database is unreachable<br>Data survives a process restart<br>No request path returns an unhandled 500 for bad input; code collisions are retried a bounded number of times |
| NFR-2 | Security: untrusted input cannot alter queries, logs or stored data shape. | must | All SQL uses bound parameters<br>URLs, codes and headers are validated or bounded before use<br>No secrets in the codebase |
| NFR-3 | Observability: requests can be correlated across logs. | should | Every response carries an X-Request-ID; a safe caller-supplied id is propagated<br>Each request is logged with method, path, status and duration |
| NFR-4 | Maintainability: the code is layered, configurable and tested. | should | HTTP, business rules and persistence are separate modules<br>Configuration comes from environment variables with working defaults<br>Line coverage is at least 85 percent and the OpenAPI schema is published |

**Assumptions and resolved ambiguities**

| ID | Source | Statement |
|---|---|---|
| A1 | analyst | The API is unauthenticated in v1; it is deployed behind a trusted gateway. |
| A2 | analyst | A single instance with an embedded database is sufficient for the prototype's load. |
| A3 | analyst | Shortening the same URL twice yields two independent links. |
| A4 | analyst | 'Basic analytics' means click counts over time, not visitor identity. |

**Out of scope:** Authentication and per-user link ownership; Custom aliases and link expiry; Rate limiting and abuse protection; A web UI

## 2. Plan and rationale

A layered FastAPI application: an HTTP layer that only translates requests and errors, a service layer that owns the business rules, and a repository that owns all SQL against an embedded SQLite database. The application is built by a factory so tests and deployments inject their own settings.

**Design decisions**

| ID | Decision | Alternatives rejected | Consequences |
|---|---|---|---|
| ADR-1 | **FastAPI on SQLite behind a repository seam.** Use FastAPI with the standard-library sqlite3 driver. Only repository.py and db.py know SQL. | PostgreSQL from day one (needs infrastructure to run the prototype); An ORM (more dependencies and indirection than six queries justify) | Zero-setup local runs and tests; moving to a client/server database replaces two modules. Write throughput is limited to one writer. |
| ADR-2 | **Random base62 codes with database-enforced uniqueness.** Generate 7 random base62 characters from the OS CSPRNG; rely on the primary key for uniqueness and retry a collision up to 5 times. | Encode an auto-increment id (enumerable, reveals volume); Hash of the URL (forces deduplication, collisions on truncation) | No coordination between instances is needed. Collision probability stays negligible until billions of links; exhaustion surfaces as a retryable 503. |
| ADR-3 | **307 redirects that are never cached.** Answer GET /{code} with 307 and Cache-Control: no-store. | 301 permanent redirect (cached by browsers, undercounts clicks and makes deletion ineffective) | Accurate counts and immediate effect of deletion, at the cost of one extra round trip per visit. |
| ADR-4 | **Soft delete.** DELETE marks the link inactive; redirects then answer 410 Gone. | Hard delete (loses analytics, lets a code be reissued to a different destination) | Codes are never reused, which prevents old links from silently pointing somewhere new. |
| ADR-5 | **Forward-only migrations applied at start-up.** Keep an ordered list of migrations; apply the pending ones transactionally on start and record them in schema_migrations. | A migration framework (unneeded dependency for a single-file schema); CREATE TABLE IF NOT EXISTS (cannot express later changes) | A new instance and an upgraded instance converge on the same schema. Rolling back a release requires migrations to be backward compatible. |

**Task graph** (plan version 1; tasks in the same wave ran in parallel). Build bottom-up along the dependency direction: configuration first, then the codec and the persistence layer in parallel, then the service that combines them, then the HTTP layer. Packaging is independent of the application code and runs alongside.

| Task | Wave | Kind | Depends on | Requirements | Writes |
|---|---|---|---|---|---|
| T1 Project skeleton and configuration | 1 | config | - | NFR-4 | urlshortener/__init__.py<br>urlshortener/config.py<br>requirements.txt<br>pytest.ini<br>tests/test_config.py |
| T2 Short-code codec | 2 | code | T1 | FR-1, NFR-2 | urlshortener/codec.py<br>tests/test_codec.py |
| T3 Persistence layer | 2 | code | T1 | FR-3, FR-4, FR-5, NFR-1, NFR-2 | urlshortener/migrations.py<br>urlshortener/db.py<br>urlshortener/repository.py<br>tests/test_repository.py |
| T4 Link service | 3 | code | T2, T3 | FR-1, FR-2, FR-3, FR-4, FR-5, NFR-1, NFR-2 | urlshortener/service.py<br>tests/test_service.py |
| T5 HTTP API and application factory | 4 | code | T4 | FR-1, FR-2, FR-3, FR-4, FR-5, NFR-1, NFR-3, NFR-4 | urlshortener/schemas.py<br>urlshortener/api.py<br>urlshortener/main.py<br>tests/conftest.py<br>tests/test_api.py<br>tests/test_ops.py |
| T6 Container packaging | 2 | config | T1 | NFR-1 | Dockerfile<br>.dockerignore |

**Test strategy.** Each task ships with the tests for its layer: pure unit tests for the codec and configuration, repository tests against a real temporary SQLite file, service tests with an injected clock and code factory, and API tests through the ASGI test client. Test docstrings name the requirement ids they cover so traceability can be checked mechanically.

## 3. Codebase impact

Greenfield: no existing modules were affected.

## 4. Artifacts

| Change | Files |
|---|---|
| added | .dockerignore<br>Dockerfile<br>pytest.ini<br>requirements.txt<br>tests/conftest.py<br>tests/test_api.py<br>tests/test_codec.py<br>tests/test_config.py<br>tests/test_ops.py<br>tests/test_repository.py<br>tests/test_service.py<br>urlshortener/__init__.py<br>urlshortener/api.py<br>urlshortener/codec.py<br>urlshortener/config.py<br>urlshortener/db.py<br>urlshortener/main.py<br>urlshortener/migrations.py<br>urlshortener/repository.py<br>urlshortener/schemas.py<br>urlshortener/service.py |
| modified | - |
| deleted | - |

| Artifact | Versions | Produced by | Hash |
|---|---|---|---|
| change_summary | 1 | implementation | c627e127625a |
| changeset:T1 | 1 | impl:T1 | 3755babd5e30 |
| changeset:T2 | 1 | impl:T2 | 55bc0736364a |
| changeset:T3 | 1 | impl:T3 | 20fdf17c640b |
| changeset:T4 | 1 | impl:T4 | dcc86083fafd |
| changeset:T5 | 1 | impl:T5 | 983a9fd306ae |
| changeset:T6 | 1 | impl:T6 | 0adaa4575f05 |
| codebase_map | 1 | codebase_scan | 5cd6ae398747 |
| design | 1 | architecture | c616fb82a33c |
| docs_change | 1 | documentation | 6eb3bf546566 |
| docs_report | 1 | documentation | 4e528f90e445 |
| impact_report | 1 | architecture | a6b48662437f |
| plan | 1 | planning | d2e3ce3fd576 |
| promotion | 1 | promotion | 71f914886885 |
| release_report | 1 | release_readiness | ec25b2c23be1 |
| requirement_spec | 1 | requirements | d8cf55474810 |
| security_report | 1 | security_review | 35730b23b7d0 |
| test_report | 1 | verification | 7c7c70749125 |
| traceability | 1 | verification | a6455176ffd9 |

## 5. Validation

- Tests: **65 run, 0 failed, 0 errors** (executed in the sandbox with pytest)
- Coverage: **100.0%**
- Security and compliance scan: 21 files, findings {'high': 0, 'low': 0, 'medium': 0}
- Gates: 54 evaluated, 0 failed and recovered

**Requirement traceability**

| Requirement | Tests |
|---|---|
| FR-1 | tests/test_api.py::test_create_link<br>tests/test_api.py::test_create_link_rejects_bad_input<br>tests/test_codec.py::test_generated_code_has_requested_length_and_alphabet<br>tests/test_codec.py::test_generated_codes_do_not_repeat_in_practice<br>tests/test_repository.py::test_duplicate_code_is_reported<br>tests/test_service.py::test_create_returns_a_resolvable_link<br>tests/test_service.py::test_unusable_urls_are_rejected<br>tests/test_service.py::test_identical_urls_get_distinct_codes |
| FR-2 | tests/test_api.py::test_follow_link_redirects<br>tests/test_api.py::test_unknown_code_is_404<br>tests/test_service.py::test_create_returns_a_resolvable_link<br>tests/test_service.py::test_unknown_or_malformed_codes_are_not_found |
| FR-3 | tests/test_api.py::test_unknown_code_is_404<br>tests/test_api.py::test_get_link_metadata<br>tests/test_repository.py::test_insert_then_get_round_trips<br>tests/test_service.py::test_unknown_or_malformed_codes_are_not_found |
| FR-4 | tests/test_api.py::test_delete_link<br>tests/test_repository.py::test_deactivate_is_a_soft_delete<br>tests/test_service.py::test_deleted_links_are_gone_but_still_described |
| FR-5 | tests/test_api.py::test_stats_reflect_redirects<br>tests/test_api.py::test_stats_of_unused_link<br>tests/test_repository.py::test_click_analytics<br>tests/test_service.py::test_stats_count_clicks_per_day_within_the_window<br>tests/test_service.py::test_stats_for_a_link_without_clicks |
| NFR-1 | tests/test_api.py::test_create_link_rejects_bad_input<br>tests/test_ops.py::test_liveness<br>tests/test_ops.py::test_readiness_reports_schema_version<br>tests/test_ops.py::test_readiness_fails_when_database_is_down<br>tests/test_ops.py::test_unexpected_errors_do_not_leak_internals<br>tests/test_ops.py::test_code_exhaustion_is_a_retryable_503<br>tests/test_ops.py::test_database_is_closed_on_shutdown<br>tests/test_repository.py::test_migrate_is_idempotent<br>tests/test_repository.py::test_data_survives_a_restart<br>tests/test_repository.py::test_failed_transaction_rolls_back<br>tests/test_repository.py::test_ping_reports_a_closed_database<br>tests/test_service.py::test_code_collisions_are_retried<br>tests/test_service.py::test_retry_budget_is_bounded |
| NFR-2 | tests/test_codec.py::test_malformed_codes_are_invalid<br>tests/test_ops.py::test_unsafe_request_id_is_replaced<br>tests/test_ops.py::test_unexpected_errors_do_not_leak_internals<br>tests/test_repository.py::test_hostile_input_is_stored_as_data_not_executed<br>tests/test_service.py::test_unusable_urls_are_rejected<br>tests/test_service.py::test_oversized_referrers_are_truncated |
| NFR-3 | tests/test_ops.py::test_request_id_is_generated<br>tests/test_ops.py::test_safe_request_id_is_echoed<br>tests/test_ops.py::test_unsafe_request_id_is_replaced |
| NFR-4 | tests/test_config.py::test_defaults_are_usable_without_any_environment<br>tests/test_config.py::test_environment_overrides_defaults<br>tests/test_ops.py::test_service_is_self_describing |

**Release checklist**

| Check | Result | Evidence |
|---|---|---|
| All planned tasks implemented | pass | 6/6 tasks |
| Test suite passes | pass | 65 tests, 0 failures, 0 errors |
| Coverage at least 85.0% | pass | 100.0% |
| Every functional requirement has a test | pass | untested: [] |
| No blocking security or compliance findings | pass | {'high': 0, 'low': 0, 'medium': 0} |
| API schema exported and reference generated | pass | 8 operations |
| Plan signed off by a human | pass | approved checkpoints: ['plan-signoff'] |
| Audit trail intact | pass | 134 records, chain intact |

## 6. Risks, trade-offs and rollback

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| R1 SQLite allows a single writer; redirect bursts queue behind click inserts. | medium | medium | WAL mode and a busy timeout; the repository seam allows moving to a server database or buffering clicks. |
| R2 The service can be used to disguise malicious destinations (open redirect by design). | medium | high | Only http(s) URLs are accepted in v1; destination vetting and rate limiting are explicitly out of scope and tracked as follow-up. |
| R3 The clicks table grows without bound. | high | low | Indexed by (code, clicked_at); the stats window is bounded. Retention or roll-ups are a follow-up. |
| R4 Unauthenticated API allows anyone to create or delete links. | high | medium | Assumption A1: deployed behind a trusted gateway. Authentication is out of scope for v1. |

**Rollback plan.** Greenfield: nothing exists to regress. The change is only promoted to the target directory after release approval, and `sdlc rollback` removes the promoted files again.

**Known limitations**

- Single-writer SQLite: suitable for a prototype or a single instance, not for horizontal scaling
- No authentication, rate limiting or destination vetting in v1
- Analytics are computed on read; no retention policy for click rows

## 7. Governance trail

| Checkpoint | Node | Required role | Outcome | By | Comment |
|---|---|---|---|---|---|
| plan-signoff | planning | tech-lead | approved | priya.tech-lead (simulated) | Layering and migration approach are sound. Approved for implementation. |
| release | release_readiness | release-manager | approved | sam.release-manager (simulated) | Checklist green, coverage above threshold. Promote. |

**Recovery and re-planning events**

| Seq | Event | Node | Detail |
|---|---|---|---|
| 19 | fault.injected | architecture | kind=transient; times=1 |
| 20 | node.attempt_failed | architecture | error=injected fault: simulated provider timeout; attempt=1; kind=transient; retry_in_s=0.05; will_retry=True |
| 42 | plan.expanded | planning | added=['impl:T1', 'impl:T2', 'impl:T3', 'impl:T4', 'impl:T5', 'impl:T6']; version=1 |

- Decisions recorded: 11 (see `state.json` -> `decisions`, or `sdlc lineage`)
- Audit log: 150 records, chain intact

## 8. Reliability metrics

| Metric | Value |
|---|---|
| status | succeeded |
| end_to_end_latency_s | 2.582 |
| active_latency_s | 2.582 |
| human_wait_s | 0.004 |
| nodes_succeeded | 16 |
| node_attempts | 17 |
| first_pass_success_rate | 0.938 |
| retries | 1 |
| retry_frequency | 0.059 |
| fallbacks | 0 |
| rollbacks | 0 |
| rework_rounds | 0 |
| replans | 0 |
| invalidations | 0 |
| incidents | 1 |
| incidents_recovered | 1 |
| mttr_s | 0.054 |
| gates_evaluated | 54 |
| gates_failed | 0 |
| approvals | 2 |
| policy_violations | 0 |
| faults_injected | 1 |

| Stage | Agent time (s) |
|---|---|
| design | 0.052 |
| requirements | 0.004 |
| planning | 0.002 |
| implementation | 0.158 |
| security | 0.026 |
| documentation | 0.524 |
| testing | 2.187 |
| release | 0.038 |
