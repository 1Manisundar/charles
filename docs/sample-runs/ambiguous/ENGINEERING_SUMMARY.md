# Engineering summary: Abuse protection and privacy-preserving analytics

- Run: `ambiguous` (ambiguous scenario, provider `scripted`)
- Status: **succeeded**
- Started 2026-10-14 00:32:44, last update 2026-10-14 00:32:49
- Target: `demo/sample/service` (promoted)

## 1. Requirement and interpretation

Raw requirement as received:

> The link service has been getting hammered lately and we had a scare with some dodgy links last week. Can you make it more robust and safer? Also, product would like better insight into who is clicking.

**Normalised problem.** Harden the existing service in three confirmed ways: throttle each client (strictly for link creation, loosely for redirects), refuse destinations that are internal, disguised, self-referencing or blocklisted, and extend statistics with unique-visitor and top-referrer aggregates while storing nothing that identifies a visitor.

| ID | Requirement | Priority | Acceptance criteria |
|---|---|---|---|
| FR-20 | Link creation is rate limited per client. | must | More than the configured number of creations per minute from one client returns 429 with a Retry-After header and a problem document<br>The limit is configurable and can be disabled; clients have independent budgets<br>X-Forwarded-For identifies the client only when the deployment declares a trusted proxy |
| FR-21 | Redirects are rate limited per client with a separate, looser budget. | should | Exceeding the redirect budget returns 429 and the refused visit is not counted as a click<br>Health probes and read-only API calls are never limited |
| FR-22 | Unsafe destinations are refused when a link is created. | must | Loopback, private, link-local and otherwise non-public addresses are refused in every spelling browsers accept<br>Internal host names, URLs with embedded credentials and URLs pointing back at the service are refused<br>Domains on a configurable blocklist, and their subdomains, are refused<br>Refusals return 422 with a reason; ordinary public URLs are unaffected |
| FR-23 | Statistics report unique visitors and top referrers. | should | The per-day breakdown includes the number of distinct visitors that day<br>Statistics list the most frequent referring hosts |
| NFR-20 | Privacy: no personal data about visitors is persisted or logged. | must | No IP address or user agent is stored or written to logs<br>Visitor identifiers cannot be reversed to an address and cannot be linked across days<br>Only the host of a referrer is stored, never its path or query string |
| NFR-21 | Bounded resources: protections use constant time and bounded memory. | must | Rate-limit state never exceeds a fixed number of tracked clients<br>Per-request overhead does not grow with the number of clients or clicks |
| NFR-22 | Backward compatibility with existing links, clients and data. | must | Existing response fields are unchanged; new fields are additive<br>Existing links and previously recorded clicks keep working and still aggregate<br>Safe defaults: limits on, forwarded headers untrusted, empty blocklist |

**Assumptions and resolved ambiguities**

| ID | Source | Statement |
|---|---|---|
| A20 | analyst | The three concerns are independent and ship together as one hardening release. |
| A21 | human | Q1 answered: 'hammered' means abusive clients, not organic growth. Rate-limit per client; creation is the abuse vector, redirects get a much looser limit. No caching or scale-out in this change. |
| A22 | human | Q2 answered: rule-based blocking only. No external reputation service this quarter; a configurable blocklist covers known bad domains. |
| A23 | human | Q3 answered: aggregates only. Product needs unique visitors and top referrers; storing IP addresses, user agents or location is explicitly rejected. |
| A24 | analyst | Rate limits are enforced per instance; the service currently runs as a single instance. |

**Out of scope:** Authentication and per-user quotas; Retroactive vetting of links that already exist; Redirect caching and horizontal scale-out; External URL-reputation checks; Visitor location

## 2. Plan and rationale

Three independent additions at the edges of the existing layers. A token-bucket limiter is attached to the create and redirect routes as a dependency. A destination check joins the existing URL validation in the service. Click recording gains an anonymous daily visitor identifier, produced by a dedicated privacy module, so statistics can count unique visitors and rank referrers without holding personal data.

**Design decisions**

| ID | Decision | Alternatives rejected | Consequences |
|---|---|---|---|
| ADR-11 | **In-process token bucket.** Keep one token bucket per client address in process memory, with a hard cap on tracked clients (least recently seen evicted). Separate limiters for creation and redirects. | A shared store such as Redis (correct across instances, but new infrastructure for a single instance); Fixed-window counters (allow a double burst at window edges) | No new dependency and O(1) per request. Limits are per instance; moving to a shared store is required before scaling out. |
| ADR-12 | **Forwarded client address only from a declared trusted proxy.** Use the socket address by default. Only when URLSHORTENER_TRUST_FORWARDED_FOR is set, use the last entry of X-Forwarded-For - the address the trusted proxy itself observed. | Always honour the first X-Forwarded-For entry (first draft; trivially spoofable); A configurable list of trusted proxy networks (more flexible; not needed for one proxy) | Safe by default. Deployments behind a proxy must set one flag; deployments behind several chained proxies are not supported yet. |
| ADR-13 | **Rule-based destination vetting inside URL validation.** Reject credentials in the URL, non-public addresses in every spelling browsers accept, internal host names, the service's own host and blocklisted domains. No DNS resolution. | Resolve the host and check the resulting address (slow, racy against DNS changes, and the service never fetches the URL anyway); External reputation service (rejected by the product owner) | Deterministic and dependency-free. A public name that resolves to a private address is not caught; documented as a limitation. |
| ADR-14 | **Unique visitors by a keyed hash under a daily-rotating in-memory key.** Identify a visitor as HMAC-SHA256(key, ip + user agent), where the key is random, held only in process memory and replaced every UTC day. Store the truncated digest; never store or log the key, the address or the user agent. | Plain SHA-256 of address and user agent (first draft; reversible by enumeration); A persistent secret salt (still links a visitor across days and becomes a secret to manage); Cookies (changes the redirect response and needs consent) | Identifiers cannot be reversed, linked across days or recomputed after a restart. Unique counts are per day only, and a restart or a second instance over-counts slightly; accepted in exchange for holding no personal data. |
| ADR-15 | **Referrer reduced to host.** Store only the referring host from now on; aggregate older full-URL rows by host when reading. | Keep storing the truncated full URL (retains personal data for no product value) | Top-referrer statistics work for old and new rows. The full referrer of new clicks is no longer available, which changes one existing test on purpose. |

**Task graph** (plan version 2; tasks in the same wave ran in parallel). Settings first, because everything reads them. The limiter, the destination check and the privacy-preserving analytics are independent of each other and run in parallel. A final task wires all three into the service, the API and the application factory. Compared with the first plan, the analytics task now delivers a dedicated privacy module, and the settings and wiring tasks carry the trusted-proxy rule.

| Task | Wave | Kind | Depends on | Requirements | Writes |
|---|---|---|---|---|---|
| T1 Hardening settings | 1 | config | - | FR-20, FR-21, FR-22, NFR-22 | urlshortener/config.py<br>tests/test_config.py |
| T2 Token-bucket rate limiter | 2 | code | T1 | FR-20, FR-21, NFR-21 | urlshortener/ratelimit.py<br>tests/test_ratelimit.py |
| T3 Destination vetting | 2 | code | T1 | FR-22 | urlshortener/safety.py<br>tests/test_safety.py |
| T4 Privacy-preserving visitor analytics | 2 | migration | T1 | FR-23, NFR-20, NFR-21, NFR-22 | urlshortener/privacy.py<br>urlshortener/migrations.py<br>urlshortener/repository.py<br>tests/test_privacy.py |
| T5 Wire limits, vetting and analytics into the service and API | 3 | code | T2, T3, T4 | FR-20, FR-21, FR-22, FR-23, NFR-20, NFR-22 | urlshortener/service.py<br>urlshortener/schemas.py<br>urlshortener/api.py<br>urlshortener/main.py<br>urlshortener/__init__.py<br>tests/test_service.py<br>tests/test_api_hardening.py |

**Test strategy.** Each building block is unit-tested in isolation with injected time and keys; the wiring task adds API tests for the observable behaviour (429, 422, new statistics fields) and one test that reads the click table back and asserts that no address, user agent or referrer path was stored. The existing 127 tests are the regression net, with one deliberate change: the referrer test now expects the host only.

## 3. Codebase impact (computed from the import graph)

| Aspect | Result |
|---|---|
| Modules changed | tests/test_config.py, tests/test_service.py, urlshortener/__init__.py, urlshortener/api.py, urlshortener/config.py, urlshortener/main.py, urlshortener/migrations.py, urlshortener/repository.py, urlshortener/schemas.py, urlshortener/service.py |
| Transitive dependents | urlshortener/db.py |
| Existing tests exercising them | tests/conftest.py, tests/test_api.py, tests/test_api_expiry_alias.py, tests/test_codec.py, tests/test_config.py, tests/test_expiry_alias.py, tests/test_migrations.py, tests/test_ops.py, tests/test_repository.py, tests/test_service.py |
| Routes affected | DELETE /api/v1/links/{code}, GET /, GET /api/v1/links/{code}, GET /api/v1/links/{code}/stats, GET /healthz, GET /readyz, GET /{code}, POST /api/v1/links |

## 4. Artifacts

| Change | Files |
|---|---|
| added | tests/test_api_hardening.py<br>tests/test_privacy.py<br>tests/test_ratelimit.py<br>tests/test_safety.py<br>urlshortener/privacy.py<br>urlshortener/ratelimit.py<br>urlshortener/safety.py |
| modified | tests/test_config.py<br>tests/test_service.py<br>urlshortener/__init__.py<br>urlshortener/api.py<br>urlshortener/config.py<br>urlshortener/main.py<br>urlshortener/migrations.py<br>urlshortener/repository.py<br>urlshortener/schemas.py<br>urlshortener/service.py |
| deleted | - |

| Artifact | Versions | Produced by | Hash |
|---|---|---|---|
| change_summary | 1 | implementation | 0e1ea5bf50af |
| changeset:T1 | 1 | impl:T1 | 9fbcb2d7175e |
| changeset:T2 | 1 | impl:T2 | 5bda511d6ce6 |
| changeset:T3 | 1 | impl:T3 | 11b46b8fb1ea |
| changeset:T4 | 1 | impl:T4 | 047917e3748c |
| changeset:T5 | 1 | impl:T5 | a7e6e1e7ceba |
| codebase_map | 1 | codebase_scan | 40265ddc19c1 |
| design | 2 | architecture | abfc9cd92eed |
| docs_change | 1 | documentation | bc39cfb50181 |
| docs_report | 1 | documentation | 4e528f90e445 |
| impact_report | 1 | architecture | c0abd2ced54f |
| plan | 2 | planning | 97335ec738e1 |
| promotion | 1 | promotion | 766308ce20fe |
| release_report | 1 | release_readiness | d97af3d0d6ae |
| requirement_spec | 2 | requirements | fc89ae5e83b1 |
| security_report | 1 | security_review | f1c18c23a56a |
| test_report | 1 | verification | 8376e9d45ece |
| traceability | 1 | verification | 7bae09f2c026 |

## 5. Validation

- Tests: **201 run, 0 failed, 0 errors** (executed in the sandbox with pytest)
- Coverage: **100.0%**
- Security and compliance scan: 35 files, findings {'high': 0, 'low': 0, 'medium': 0}
- Gates: 64 evaluated, 0 failed and recovered

**Requirement traceability**

| Requirement | Tests |
|---|---|
| FR-20 | tests/test_api_hardening.py::test_link_creation_is_rate_limited<br>tests/test_api_hardening.py::test_limits_are_per_client_behind_a_trusted_proxy<br>tests/test_api_hardening.py::test_forwarded_header_is_ignored_unless_trusted<br>tests/test_api_hardening.py::test_limiter_can_be_disabled<br>tests/test_config.py::test_hardening_settings_from_environment<br>tests/test_ratelimit.py::test_burst_up_to_the_limit_then_refused<br>tests/test_ratelimit.py::test_budget_refills_over_time<br>tests/test_ratelimit.py::test_refused_requests_do_not_extend_the_penalty<br>tests/test_ratelimit.py::test_clients_have_independent_budgets<br>tests/test_ratelimit.py::test_zero_disables_the_limiter<br>tests/test_ratelimit.py::test_rate_limited_error_rounds_up_to_whole_seconds |
| FR-21 | tests/test_api_hardening.py::test_redirects_have_their_own_budget |
| FR-22 | tests/test_api_hardening.py::test_unsafe_destinations_are_422<br>tests/test_config.py::test_hardening_settings_from_environment<br>tests/test_safety.py::test_public_destinations_pass<br>tests/test_safety.py::test_internal_destinations_are_refused<br>tests/test_safety.py::test_embedded_credentials_are_refused<br>tests/test_safety.py::test_links_back_to_the_service_are_refused<br>tests/test_safety.py::test_blocked_domains_cover_subdomains<br>tests/test_safety.py::test_blocklist_matches_whole_labels_only<br>tests/test_service.py::test_unsafe_destinations_are_invalid_urls |
| FR-23 | tests/test_api_hardening.py::test_stats_report_unique_visitors_and_top_referrers<br>tests/test_privacy.py::test_same_visitor_same_day_gets_the_same_id<br>tests/test_privacy.py::test_repository_counts_unique_visitors_and_referrers<br>tests/test_service.py::test_referrers_recorded_by_older_releases_are_folded_into_hosts |
| NFR-20 | tests/test_api_hardening.py::test_forwarded_header_is_ignored_unless_trusted<br>tests/test_api_hardening.py::test_no_personal_data_reaches_the_database<br>tests/test_privacy.py::test_ids_cannot_be_linked_across_days<br>tests/test_privacy.py::test_ids_cannot_be_recomputed_by_another_process<br>tests/test_privacy.py::test_id_reveals_nothing_about_its_input<br>tests/test_privacy.py::test_referrer_is_reduced_to_its_host<br>tests/test_service.py::test_only_the_referrer_host_is_stored |
| NFR-21 | tests/test_privacy.py::test_key_is_requested_once_per_day<br>tests/test_ratelimit.py::test_memory_is_bounded |
| NFR-22 | tests/test_api_hardening.py::test_public_destinations_are_still_accepted<br>tests/test_api_hardening.py::test_existing_stats_fields_are_unchanged<br>tests/test_config.py::test_hardening_defaults_are_safe<br>tests/test_safety.py::test_public_destinations_pass<br>tests/test_service.py::test_referrers_recorded_by_older_releases_are_folded_into_hosts |

**Release checklist**

| Check | Result | Evidence |
|---|---|---|
| All planned tasks implemented | pass | 5/5 tasks |
| Test suite passes | pass | 201 tests, 0 failures, 0 errors |
| Coverage at least 85.0% | pass | 100.0% |
| Every functional requirement has a test | pass | untested: [] |
| No blocking security or compliance findings | pass | {'high': 0, 'low': 0, 'medium': 0} |
| API schema exported and reference generated | pass | 8 operations |
| Plan signed off by a human | pass | approved checkpoints: ['change-control:T4', 'plan-signoff'] |
| Audit trail intact | pass | 167 records, chain intact |

## 6. Risks, trade-offs and rollback

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| R20 Rate limits throttle legitimate users who share an address (offices, mobile carriers). | medium | medium | Generous redirect default, configurable limits, clear 429 with Retry-After. |
| R21 The destination rules reject legitimate links. | low | medium | Rules target only non-public and disguised destinations; tested against ordinary public URLs. |
| R22 Schema migration on the click log. | low | high | Additive nullable column; change-control approval before the task runs; covered by the existing upgrade tests. |
| R23 Limiter memory grows with the number of distinct clients. | medium | medium | Hard cap on tracked clients with least-recently-seen eviction. |
| R24 A misconfigured proxy flag lets clients spoof their address, or collapses all clients into one bucket. | medium | medium | Untrusted by default; only the proxy-appended entry is used; behaviour documented with the setting. |
| R25 Visitor analytics are mistaken for personal data in an audit. | low | high | Keyed, rotating, memory-only identifier; a test asserts that no address, user agent or referrer path reaches the database. |

**Rollback plan.** Redeploy 1.1.0: it ignores clicks.visitor_hash and the new settings. Limits and destination checks disappear with the code; no data needs repair. In this pipeline nothing reaches the target before release approval, and `sdlc rollback` restores it afterwards.

**Known limitations**

- Rate limits and unique-visitor keys are per instance and reset on restart
- Destination vetting does not resolve DNS
- Unique-visitor counts are per day and treat several people behind one address and browser as one visitor
- Only a single trusted proxy is supported for X-Forwarded-For

## 7. Governance trail

| Checkpoint | Node | Required role | Outcome | By | Comment |
|---|---|---|---|---|---|
| clarification | requirements | product-owner | changes_requested | alex.product-owner (simulated) | Answers below. Keep it to code changes we can ship this sprint. |
| plan-signoff | planning | tech-lead | changes_requested | priya.tech-lead (simulated) | Two blockers. (1) ADR-14: SHA-256 of an IP address is not anonymous - the IPv4 space can be enumerated, so this is still personal data and contradicts the product owner's answer to Q3. Use a keyed hash with a key that lives only in memory and rotates daily. (2) ADR-12: trusting X-Forwarded-For unconditionally lets any client pick its own identity and walk around the limiter. Trust it only when a proxy is explicitly configured, and then only the entry the proxy appended. |
| plan-signoff | planning | tech-lead | approved | priya.tech-lead (simulated) | Both points addressed in ADR-12 and ADR-14; plan updated accordingly. Approved. |
| change-control:T4 | impl:T4 | change-advisory | approved | dana.dba (simulated) | Migration 3 adds one nullable column holding an opaque value. Approved to run. |
| release | release_readiness | release-manager | approved | sam.release-manager (simulated) | Checklist green; privacy test confirms nothing identifying is stored. Promote. |

**Recovery and re-planning events**

| Seq | Event | Node | Detail |
|---|---|---|---|
| 15 | node.reset | requirements | reason=changes requested at 'clarification' |
| 50 | plan.expanded | planning | added=['impl:T1', 'impl:T2', 'impl:T3', 'impl:T4', 'impl:T5']; version=1 |
| 55 | node.reset | architecture | reason=changes requested at 'plan-signoff' |
| 78 | plan.expanded | planning | changed=['impl:T1', 'impl:T4', 'impl:T5']; kept=['impl:T2', 'impl:T3']; version=2 |

- Decisions recorded: 22 (see `state.json` -> `decisions`, or `sdlc lineage`)
- Audit log: 183 records, chain intact

## 8. Reliability metrics

| Metric | Value |
|---|---|
| status | succeeded |
| end_to_end_latency_s | 4.295 |
| active_latency_s | 4.295 |
| human_wait_s | 0.027 |
| nodes_succeeded | 15 |
| node_attempts | 18 |
| first_pass_success_rate | 0.867 |
| retries | 0 |
| retry_frequency | 0.0 |
| fallbacks | 0 |
| rollbacks | 0 |
| rework_rounds | 0 |
| replans | 1 |
| invalidations | 3 |
| incidents | 0 |
| incidents_recovered | 0 |
| mttr_s | n/a |
| gates_evaluated | 64 |
| gates_failed | 0 |
| approvals | 5 |
| policy_violations | 0 |
| faults_injected | 0 |

| Stage | Agent time (s) |
|---|---|
| design | 0.033 |
| requirements | 0.005 |
| planning | 0.001 |
| implementation | 0.186 |
| security | 0.037 |
| documentation | 0.5 |
| testing | 3.841 |
| release | 0.04 |
