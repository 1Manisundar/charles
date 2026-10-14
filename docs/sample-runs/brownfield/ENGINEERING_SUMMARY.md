# Engineering summary: Expiring links and custom aliases

- Run: `brownfield` (brownfield scenario, provider `scripted`)
- Status: **succeeded**
- Started 2026-10-14 00:32:38, last update 2026-10-14 00:32:44
- Target: `demo/sample/service` (promoted)

## 1. Requirement and interpretation

Raw requirement as received:

> Marketing wants two additions to the existing URL shortener. First, campaign links should stop working after a set amount of time. Second, they want to choose the short code themselves (for example /spring-sale) instead of getting a random one. Links that already exist must keep working exactly as they do today.

**Normalised problem.** Extend the existing link-creation API with two optional inputs - a lifetime after which the link stops redirecting, and a caller-chosen short code - without changing the behaviour of existing links or of clients that do not use the new inputs.

| ID | Requirement | Priority | Acceptance criteria |
|---|---|---|---|
| FR-10 | A client can give a link a lifetime when creating it. | must | POST /api/v1/links accepts optional expires_in_seconds between 60 and 31536000 (365 days)<br>The response and the metadata endpoint expose the resulting expires_at<br>A value outside the range or of the wrong type returns 422 |
| FR-11 | An expired link stops redirecting. | must | From expires_at onwards (inclusive) GET /{code} returns 410 Gone<br>Refused visits are not counted as clicks<br>Metadata and statistics of an expired link remain available |
| FR-12 | A client can choose the short code. | must | POST /api/v1/links accepts optional alias of 3-32 letters, digits, '-' or '_'<br>The alias is used verbatim as the code and the response marks the link as custom |
| FR-13 | An alias cannot collide with another link or with the service's own routes. | must | An alias that is already in use returns 409 and never replaces the existing link<br>A malformed alias or a reserved name (api, docs, healthz, readyz, ...) returns 422<br>The code of a deleted link cannot be claimed as an alias |
| NFR-10 | Backward compatibility: nothing that works today changes. | must | Requests without the new fields produce the same result as before<br>Existing links keep redirecting and never expire<br>The existing test-suite passes; any test that changes is changed deliberately and listed |
| NFR-11 | Safe schema change: the database upgrade is automatic, additive and atomic. | must | A populated v1 database upgrades in place with no data loss<br>A failing migration leaves the previous schema untouched<br>The previous application release still runs against the upgraded schema |

**Assumptions and resolved ambiguities**

| ID | Source | Statement |
|---|---|---|
| A10 | analyst | 'After a set amount of time' means a lifetime relative to creation, supplied in seconds; an absolute timestamp input is not needed. |
| A11 | analyst | Aliases live in the same namespace as generated codes and are case-sensitive, like generated codes. |
| A12 | analyst | Expiry is not retroactive: links that exist today never expire and cannot be given a lifetime in this change. |

**Out of scope:** Changing the lifetime or alias of an existing link; Deleting expired links or their analytics; Per-user ownership of aliases

## 2. Plan and rationale

The change threads two optional inputs through every existing layer without adding new ones: the schema gains two nullable/defaulted columns, the repository reads and writes them, the service validates them and enforces expiry lazily at redirect time, and the API contract exposes them. The redirect path gains one in-memory comparison and no extra query.

**Design decisions**

| ID | Decision | Alternatives rejected | Consequences |
|---|---|---|---|
| ADR-6 | **Relative lifetime in, absolute expiry stored.** Accept expires_in_seconds, compute expires_at once at creation from the server clock and store that. | Accept an absolute timestamp (exposes clients to clock skew and time-zone mistakes); Store the duration and add it on every read (work on the hot path for no benefit) | One comparison per redirect; the stored value is unambiguous. Changing a link's lifetime later needs a new endpoint. |
| ADR-7 | **Lazy expiry, inclusive of the expiry instant.** Check expiry when the link is resolved; a link is gone when now >= expires_at. No background job. | A reaper job that deactivates expired links (needs scheduling and still races with requests); Filtering in SQL (hides the link from metadata and statistics too) | Exact, race-free behaviour with no moving parts. Expired rows remain until a retention policy exists. |
| ADR-8 | **Aliases share the code namespace.** Store the alias in links.code. The primary key gives uniqueness atomically; a conflict is reported as 409 and never retried or substituted. | A separate alias table (second lookup on every redirect, two uniqueness rules to keep consistent); Check-then-insert (race between two requests for the same alias) | No new query on the redirect path. The existing code syntax check must admit '-' and '_' and three-character codes, which changes one existing test on purpose. |
| ADR-9 | **Reserved names instead of a separate URL prefix.** Refuse a case-insensitive list of reserved names, and keep service routes registered before the catch-all redirect route. | Move redirects under /r/{code} (breaks every existing short link) | Existing links keep their URLs. New top-level routes must be added to the reserved list. |
| ADR-10 | **Additive migration only.** Migration 2 only adds a nullable column and a defaulted column; no rewrite, no backfill. | Rebuild the table with the new shape (long lock, irreversible, no benefit) | Instant on any table size and safe to roll the application back: release 1.0 reads rows by column name and ignores the new columns. |

**Task graph** (plan version 1; tasks in the same wave ran in parallel). Change the layers in dependency order. The schema migration and the alias validation are independent and run in parallel; the repository and service build on both; the API contract comes last. Each task extends the tests of the layer it touches, and the existing suite is the regression net.

| Task | Wave | Kind | Depends on | Requirements | Writes |
|---|---|---|---|---|---|
| T1 Schema migration 2: expiry and custom-code columns | 1 | migration | - | NFR-10, NFR-11 | urlshortener/migrations.py<br>tests/test_migrations.py |
| T2 Alias validation | 1 | code | - | FR-12, FR-13 | urlshortener/codec.py<br>tests/test_codec.py |
| T3 Expiry and aliases in repository and service | 2 | code | T1, T2 | FR-10, FR-11, FR-12, FR-13, NFR-10 | urlshortener/repository.py<br>urlshortener/service.py<br>tests/test_expiry_alias.py |
| T4 API contract for expiry and aliases | 3 | code | T3 | FR-10, FR-11, FR-12, FR-13, NFR-10 | urlshortener/schemas.py<br>urlshortener/api.py<br>urlshortener/__init__.py<br>tests/test_api_expiry_alias.py |

**Test strategy.** Regression first: the existing 65 tests must stay green, with one deliberate edit (three-character codes become valid). New behaviour is tested per layer: the migration against a populated v1 database and against a failing script, alias syntax in the codec, expiry boundaries and alias conflicts in the service with an injected clock, and status codes through the API. The expiry boundary gets a test one microsecond before and exactly at the instant.

## 3. Codebase impact (computed from the import graph)

| Aspect | Result |
|---|---|
| Modules changed | tests/test_codec.py, urlshortener/__init__.py, urlshortener/api.py, urlshortener/codec.py, urlshortener/migrations.py, urlshortener/repository.py, urlshortener/schemas.py, urlshortener/service.py |
| Transitive dependents | urlshortener/db.py, urlshortener/main.py |
| Existing tests exercising them | tests/conftest.py, tests/test_api.py, tests/test_codec.py, tests/test_config.py, tests/test_ops.py, tests/test_repository.py, tests/test_service.py |
| Routes affected | DELETE /api/v1/links/{code}, GET /, GET /api/v1/links/{code}, GET /api/v1/links/{code}/stats, GET /healthz, GET /readyz, GET /{code}, POST /api/v1/links |

## 4. Artifacts

| Change | Files |
|---|---|
| added | tests/test_api_expiry_alias.py<br>tests/test_expiry_alias.py<br>tests/test_migrations.py |
| modified | tests/test_codec.py<br>urlshortener/__init__.py<br>urlshortener/api.py<br>urlshortener/codec.py<br>urlshortener/migrations.py<br>urlshortener/repository.py<br>urlshortener/schemas.py<br>urlshortener/service.py |
| deleted | - |

| Artifact | Versions | Produced by | Hash |
|---|---|---|---|
| change_summary | 2 | implementation | 7aecf8833e34 |
| changeset:T1 | 1 | impl:T1 | 07344e3f5f69 |
| changeset:T2 | 1 | impl:T2 | d76dba87f477 |
| changeset:T3 | 2 | impl:T3 | 5c2122a25bd5 |
| changeset:T4 | 1 | impl:T4 | cd3f84bd8bbb |
| codebase_map | 1 | codebase_scan | 1a8f4f915f04 |
| design | 1 | architecture | d83bff296205 |
| docs_change | 1 | documentation | 2cfd44286d6e |
| docs_report | 1 | documentation | 4e528f90e445 |
| impact_report | 1 | architecture | 89041f6d90bf |
| plan | 1 | planning | 70819d181c5d |
| promotion | 1 | promotion | c9740e6ed44a |
| rejected:test_report | 1 | verification | a2e85a7d19c0 |
| rejected:traceability | 1 | verification | b583eba19bfa |
| release_report | 1 | release_readiness | 122d22e4efde |
| requirement_spec | 1 | requirements | b8b5bee49163 |
| security_report | 1 | security_review | 979b159bf845 |
| test_report | 1 | verification | d93942f9a6e3 |
| traceability | 1 | verification | b583eba19bfa |

## 5. Validation

- Tests: **127 run, 0 failed, 0 errors** (executed in the sandbox with pytest)
- Coverage: **100.0%**
- Security and compliance scan: 28 files, findings {'high': 0, 'low': 0, 'medium': 0}
- Gates: 68 evaluated, 1 failed and recovered

**Requirement traceability**

| Requirement | Tests |
|---|---|
| FR-10 | tests/test_api_expiry_alias.py::test_create_with_expiry<br>tests/test_api_expiry_alias.py::test_out_of_range_expiry_is_422<br>tests/test_api_expiry_alias.py::test_wrongly_typed_fields_are_422<br>tests/test_expiry_alias.py::test_ttl_sets_an_absolute_expiry<br>tests/test_expiry_alias.py::test_ttl_limits_are_inclusive<br>tests/test_expiry_alias.py::test_ttl_outside_the_limits_is_rejected<br>tests/test_expiry_alias.py::test_alias_can_be_combined_with_expiry |
| FR-11 | tests/test_api_expiry_alias.py::test_expired_link_answers_410_but_stays_inspectable<br>tests/test_expiry_alias.py::test_link_redirects_until_just_before_expiry<br>tests/test_expiry_alias.py::test_link_is_gone_at_the_exact_expiry_instant<br>tests/test_expiry_alias.py::test_expired_link_keeps_metadata_and_stats |
| FR-12 | tests/test_api_expiry_alias.py::test_create_with_alias<br>tests/test_api_expiry_alias.py::test_wrongly_typed_fields_are_422<br>tests/test_codec.py::test_well_formed_aliases_are_accepted<br>tests/test_expiry_alias.py::test_alias_becomes_the_code<br>tests/test_expiry_alias.py::test_alias_can_be_combined_with_expiry |
| FR-13 | tests/test_api_expiry_alias.py::test_taken_alias_is_409<br>tests/test_api_expiry_alias.py::test_invalid_alias_is_422<br>tests/test_api_expiry_alias.py::test_reserved_routes_are_not_shadowed<br>tests/test_codec.py::test_malformed_aliases_are_rejected<br>tests/test_codec.py::test_reserved_names_are_rejected<br>tests/test_expiry_alias.py::test_taken_alias_is_refused_not_substituted<br>tests/test_expiry_alias.py::test_alias_of_a_deleted_link_stays_taken<br>tests/test_expiry_alias.py::test_malformed_or_reserved_alias_is_rejected |
| NFR-10 | tests/test_api_expiry_alias.py::test_requests_without_new_fields_are_unchanged<br>tests/test_expiry_alias.py::test_links_without_ttl_never_expire<br>tests/test_expiry_alias.py::test_invalid_url_wins_over_alias<br>tests/test_migrations.py::test_upgrade_from_v1_preserves_existing_data |
| NFR-11 | tests/test_migrations.py::test_migration_versions_are_contiguous<br>tests/test_migrations.py::test_upgrade_from_v1_preserves_existing_data<br>tests/test_migrations.py::test_failed_migration_changes_nothing |

**Release checklist**

| Check | Result | Evidence |
|---|---|---|
| All planned tasks implemented | pass | 4/4 tasks |
| Test suite passes | pass | 127 tests, 0 failures, 0 errors |
| Coverage at least 85.0% | pass | 100.0% |
| Every functional requirement has a test | pass | untested: [] |
| No blocking security or compliance findings | pass | {'high': 0, 'low': 0, 'medium': 0} |
| API schema exported and reference generated | pass | 8 operations |
| Plan signed off by a human | pass | approved checkpoints: ['change-control:T1', 'plan-signoff'] |
| Audit trail intact | pass | 171 records, chain intact |

**Gate failures during the run** (each was recovered or stopped the run)

| Node | Gate | Reason |
|---|---|---|
| verification | tests_pass | 3 of 127 tests failed: tests/test_api_expiry_alias.py::test_expired_link_answers_410_but_stays_inspectable: assert 307 == 410  +  where 307 = <Response [307 Temporary Redirect]>.status_code client = <starlette.testclient.TestClient object a |

## 6. Risks, trade-offs and rollback

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| R10 The schema migration damages or loses existing links. | low | high | Additive DDL only, applied in one transaction, tested against a populated v1 database and against a failing migration; requires change-control approval before the task runs. |
| R11 An off-by-one at the expiry boundary lets a link live too long or die too early. | medium | medium | The rule is a single function with tests one microsecond before and exactly at the expiry instant. |
| R12 An alias shadows a service route or squats on a brand name. | medium | medium | Reserved-name list and route ordering; ownership and moderation of aliases need authentication and are out of scope. |
| R13 Widening the code syntax changes behaviour for existing clients. | low | low | Only lookups of previously malformed codes change (still 404, now after a database miss); covered by the updated codec test. |

**Rollback plan.** Application rollback is sufficient: redeploy 1.0.0. It reads rows by column name and ignores expires_at and is_custom, so the upgraded schema needs no down-migration. Links created with a lifetime would stop expiring and aliases keep working as ordinary codes. In this pipeline the change stays in the sandbox until release approval, and `sdlc rollback` restores the target directory.

**Known limitations**

- Aliases are case-sensitive: 'Spring' and 'spring' are different links
- Expired links are never purged
- A link's lifetime and alias cannot be changed after creation

## 7. Governance trail

| Checkpoint | Node | Required role | Outcome | By | Comment |
|---|---|---|---|---|---|
| plan-signoff | planning | tech-lead | approved | priya.tech-lead (simulated) | Additive migration and lazy expiry are the right call. Approved. |
| change-control:T1 | impl:T1 | change-advisory | approved | dana.dba (simulated) | Reviewed migration 2: additive, transactional, app-only rollback. Approved to run. |
| release | release_readiness | release-manager | approved | sam.release-manager (simulated) | Regression suite green after one rework round; migration tested on v1 data. Promote. |

**Recovery and re-planning events**

| Seq | Event | Node | Detail |
|---|---|---|---|
| 38 | plan.expanded | planning | added=['impl:T1', 'impl:T2', 'impl:T3', 'impl:T4']; version=1 |
| 69 | fault.injected | impl:T3 | invocation=0; kind=corrupt_change; path=urlshortener/service.py; replace=now > link.expires_at; search=now >= link.expires_at |
| 112 | node.attempt_failed | verification | error=tests_pass: 3 of 127 tests failed: tests/test_api_expiry_alias.py::test_expired_link_answers_410_but_stays_inspectable: assert 307 == 410  +  where 307 = <Respo; attempt=1; kind=gate; rework=True; will_retry=False |
| 115 | rework.triggered | verification | blame=Blamed the tasks whose files appear in the failure output.; limit=2; round=1; targets=['impl:T3', 'impl:T4'] |
| 117 | workspace.rolled_back | documentation | reason=rework round 1 requested by 'verification' |
| 118 | workspace.rolled_back | impl:T4 | reason=rework round 1 requested by 'verification' |
| 119 | workspace.rolled_back | impl:T3 | reason=rework round 1 requested by 'verification' |
| 122 | node.reset | impl:T3 | reason=rework round 1 requested by 'verification' |
| 123 | node.reset | impl:T4 | reason=rework round 1 requested by 'verification' |

- Decisions recorded: 12 (see `state.json` -> `decisions`, or `sdlc lineage`)
- Audit log: 187 records, chain intact

## 8. Reliability metrics

| Metric | Value |
|---|---|
| status | succeeded |
| end_to_end_latency_s | 6.366 |
| active_latency_s | 6.366 |
| human_wait_s | 0.019 |
| nodes_succeeded | 14 |
| node_attempts | 20 |
| first_pass_success_rate | 0.929 |
| retries | 0 |
| retry_frequency | 0.0 |
| fallbacks | 0 |
| rollbacks | 3 |
| rework_rounds | 1 |
| replans | 0 |
| invalidations | 4 |
| incidents | 1 |
| incidents_recovered | 1 |
| mttr_s | 3.04 |
| gates_evaluated | 68 |
| gates_failed | 1 |
| approvals | 3 |
| policy_violations | 0 |
| faults_injected | 1 |

| Stage | Agent time (s) |
|---|---|
| design | 0.027 |
| requirements | 0.029 |
| planning | 0.001 |
| implementation | 0.208 |
| security | 0.053 |
| documentation | 0.906 |
| testing | 2.88 |
| release | 0.037 |
