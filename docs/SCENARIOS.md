# The three scenarios

Each scenario is a requirement plus a playbook (`playbooks/<name>/`) holding the recorded
agent outputs, the simulated reviewers' decisions and any seeded fault. They build on each
other against one target directory: greenfield creates the service, brownfield extends it,
ambiguous hardens it. `sdlc demo` runs all three; the output of one such run is in
[`sample-runs/`](sample-runs/) (console transcript, audit logs, review packets, summaries).

Reproduce any of them yourself, with you as the reviewer:

```bash
sdlc run --scenario greenfield --target work/service --run-id g      # pauses at checkpoints
sdlc run --scenario brownfield --target work/service --run-id b      # needs the greenfield result
sdlc run --scenario ambiguous  --target work/service --run-id a      # needs the brownfield result
```

A scenario refuses to start against the wrong baseline (for example brownfield on an
empty directory) with an explanation instead of failing halfway.

| | Greenfield | Brownfield | Ambiguous |
|---|---|---|---|
| Requirement | Build the service | Add expiry and custom aliases | "More robust and safer, better insight into who is clicking" |
| Plan | 6 tasks, 4 waves | 4 tasks, 3 waves | 5 tasks, 3 waves (plan v2) |
| Human checkpoints | plan, release | plan, **change-control (migration)**, release | **clarification**, plan (**changes requested**), plan, change-control, release |
| Non-linear behaviour shown | retry after a transient fault | failed tests -> rollback -> targeted rework | clarification loop; upstream change -> re-plan |
| Result | 25 files, 65 tests | +3 / ~12 files, 127 tests | +7 / ~14 files, 201 tests |
| Service version | 1.0.0 | 1.1.0 | 1.2.0 |

Coverage of the generated service is 100% of lines after each scenario (gate: 85%).

---

## 1. Greenfield - build the service from nothing

**Requirement** ([requirement.md](../playbooks/greenfield/requirement.md)):
build a URL shortener with create, redirect, lookup, delete, basic analytics, and
production-grade reliability.

### Understanding

The analyst normalises it into five functional requirements (FR-1 create, FR-2 redirect,
FR-3 metadata, FR-4 delete, FR-5 analytics) and four non-functional ones (reliability,
security, observability, maintainability), each with testable acceptance criteria. The
request is specific enough that nothing is escalated; four low-risk assumptions are
recorded instead (unauthenticated behind a gateway, single instance, no deduplication,
"analytics" means counts). Out of scope is stated: auth, aliases, expiry, rate limiting.

### Decomposition

| Task | Wave | Depends on | Delivers |
|---|---|---|---|
| T1 Project skeleton and configuration | 1 | - | package, settings from environment, pytest config |
| T2 Short-code codec | 2 | T1 | random base62 codes, validity check |
| T3 Persistence layer | 2 | T1 | migrations, SQLite wrapper with transactions, repository |
| T6 Container packaging | 2 | T1 | Dockerfile (unprivileged, volume, health check) |
| T4 Link service | 3 | T2, T3 | validation, bounded collision retry, resolve + click, stats |
| T5 HTTP API and app factory | 4 | T4 | routes, RFC 7807 errors, request-id middleware, probes |

Every task carries the tests for its layer and the requirement ids it serves.

### Orchestration

```
codebase_scan ∥ requirements        both start immediately; the scan finds an empty codebase
architecture                        attempt 1: injected provider timeout -> retried after 0.05 s -> ok
planning                            plan v1 expanded into 6 task nodes
  HUMAN plan-signoff                approved
impl:T1
impl:T2 ∥ impl:T3 ∥ impl:T6         three tasks in parallel
impl:T4 -> impl:T5
implementation (join)
verification ∥ security ∥ docs      pytest in the sandbox (65 tests); policy scan; OpenAPI export
release_readiness                   checklist all green
  HUMAN release                     approved
promotion                           25 files copied to the target
```

No task needs change-control approval: the plan contains schema DDL, but there is no
existing data to damage, and the policy only treats changes to something that already
exists as high impact.

### Validation

- 65 tests, 100% coverage; every FR and NFR is referenced by at least one test.
- Security scan clean (parameterised SQL only, no unapproved imports, no secrets).
- API reference generated from the application's own OpenAPI schema.
- Evidence: [summary](sample-runs/greenfield/ENGINEERING_SUMMARY.md),
  [audit log](sample-runs/greenfield/audit.jsonl),
  [plan review packet](sample-runs/greenfield/approvals/A-001-plan-signoff.md).

---

## 2. Brownfield - extend the existing service

**Requirement** ([requirement.md](../playbooks/brownfield/requirement.md)):
campaign links should stop working after a set time; marketing wants to choose the short
code; existing links must keep working exactly as today.

### Understanding the codebase first

The codebase analyst builds a structural map with `ast` - import graph, HTTP routes,
tables - before the architect runs. The architect names the modules it will touch; two
things then happen that do not depend on the model being right:

- an exit gate checks every named module **exists** in that map (a hallucinated path is
  rejected and fed back);
- the blast radius is **computed** from the import graph. The design lists seven source
  modules; the impact report adds what depends on them - `urlshortener/main.py`,
  the affected routes, and the existing tests that exercise them (including those that
  reach the app only through `conftest.py` fixtures).

The spec adds FR-10..FR-13 and two non-functional requirements that matter most in a
brownfield change: NFR-10 backward compatibility and NFR-11 a safe, additive, atomic
schema change. Requirement ids continue the existing numbering so traceability stays
unambiguous in a codebase that already contains FR-1..FR-5.

### Decomposition

| Task | Wave | Depends on | Delivers |
|---|---|---|---|
| T1 Schema migration 2 *(migration)* | 1 | - | `links.expires_at`, `links.is_custom`; upgrade-from-v1 and failed-migration tests |
| T2 Alias validation | 1 | - | alias syntax, reserved names; one existing test updated on purpose |
| T3 Expiry and aliases in repository and service | 2 | T1, T2 | TTL bounds, atomic alias reservation, expiry check |
| T4 API contract | 3 | T3 | optional request fields, new response fields, 409/422 mapping |

### Orchestration

```
codebase_scan ∥ requirements -> architecture -> planning
  HUMAN plan-signoff                 approved
impl:T1  HUMAN change-control:T1     migration against existing data + protected path: must be
                                     approved BEFORE it runs
impl:T2                              runs meanwhile - an independent branch is not held up
impl:T1                              runs once the DBA approves
impl:T3                              first attempt carries a seeded off-by-one (now > expires_at)
impl:T4 -> implementation (join)
verification ∥ security ∥ docs
verification                         exit gate tests_pass FAILS: 3 of 127 tests
  REWORK round 1/2                   failure output names files owned by T3 and T4 -> only those re-open
  rollback                           documentation, T4, T3 undone newest-first; T1 and T2 untouched
  security_review, implementation    marked stale
impl:T3 (with the failing test output as feedback) -> impl:T4 -> join
verification ∥ security ∥ docs       all re-run; 127 tests pass
  HUMAN release                      approved
promotion                            3 files added, 12 modified
```

The seeded defect is declared in the playbook and appears in the audit log as
`fault.injected`, so the drill cannot be mistaken for a real failure. Everything after it
is the engine's ordinary behaviour: the failing report is kept as `rejected:test_report`,
the approved migration task is **not** re-executed, and recovery time (first failure of
`verification` to its next success) is reported as MTTR.

### Validation

- The regression net is the existing suite: 65 earlier tests keep passing, one edited
  deliberately and called out in the design.
- The migration is tested against a populated v1 database and against a script that fails
  halfway (nothing is applied).
- The expiry boundary has a test one microsecond before and exactly at the instant - the
  test that caught the seeded defect.
- Evidence: [summary](sample-runs/brownfield/ENGINEERING_SUMMARY.md),
  [change-control packet](sample-runs/brownfield/approvals/A-002-change-control-T1.md).

---

## 3. Ambiguous - a vague request

**Requirement** ([requirement.md](../playbooks/ambiguous/requirement.md)):
"The link service has been getting hammered lately and we had a scare with some dodgy
links last week. Can you make it more robust and safer? Also, product would like better
insight into who is clicking."

### Understanding: identify the ambiguity, do not guess

The analyst's first output keeps the requirements deliberately loose and raises three
questions, each with concrete options, a recommendation and why it matters:

| | Question | Options | Why it matters |
|---|---|---|---|
| Q1 | What does "hammered" mean we should do? | rate-limit clients / cache redirects / scale out | three different projects; only one is a bounded code change |
| Q2 | What makes a link "dodgy"? | rule-based blocking / reputation service / moderation queue | an external service adds a vendor and a failure mode to link creation |
| Q3 | What must "who is clicking" reveal? | aggregates only / store IP and user agent / location | storing addresses makes the click log personal data |

Because open questions exist, the `clarification` checkpoint fires. The product owner
answers (rate-limit, rules plus a blocklist, **aggregates only - no IP addresses**); the
answers go back to the analyst as feedback, and the second spec has no open questions,
records each answer as an assumption with source `human`, and tightens the acceptance
criteria. Design does not start until then.

### Decomposition (plan v2)

| Task | Wave | Depends on | Delivers |
|---|---|---|---|
| T1 Hardening settings | 1 | - | limits, proxy trust (off by default), blocklist |
| T2 Token-bucket rate limiter | 2 | T1 | per-client buckets with bounded memory |
| T3 Destination vetting | 2 | T1 | refuses internal, disguised, self-referencing and blocked URLs |
| T4 Privacy-preserving analytics *(migration)* | 2 | T1 | keyed daily visitor ids, referrer reduced to host, `clicks.visitor_hash` |
| T5 Wiring | 3 | T2, T3, T4 | route dependencies, 429/422 mapping, extended statistics |

### Orchestration: the plan changes after review

```
requirements                         spec v1 with Q1..Q3
  HUMAN clarification                answers supplied -> requirements re-opened with them
requirements                         spec v2, no open questions
architecture                         design v1
planning                             plan v1 (5 tasks)
  HUMAN plan-signoff                 CHANGES REQUESTED, target: architecture
                                       (1) SHA-256 of an IP address is reversible by enumeration -
                                           still personal data; use a keyed hash, key in memory, rotated daily
                                       (2) trusting X-Forwarded-For unconditionally lets clients dodge the limiter
architecture                         re-opened with the comment -> design v2 (ADR-12 and ADR-14 revised)
planning                             stale -> inputs changed -> re-run -> plan v2
                                       task graph diff: changed T1, T4, T5; kept T2, T3
  HUMAN plan-signoff                 raised again (the approved content changed) -> approved
impl:T1 -> impl:T2 ∥ impl:T3 ∥ impl:T4 (HUMAN change-control:T4) -> impl:T5 -> join
verification ∥ security ∥ docs -> HUMAN release -> promotion
```

Two things are worth noticing. The reviewer caught something no scanner would: the first
design satisfied the letter of "do not store IP addresses" while keeping reversible
identifiers. And the engine treated the fix as an upstream change - it re-ran only what
depended on the design, recomputed the task graph, recorded the diff, and required a new
sign-off because approvals are bound to the content that was approved.

### Validation

- A test reads the click table back after a request with a known address, user agent and
  referrer, and asserts none of them is stored.
- Rate-limit behaviour is tested with injected time; destination vetting against internal
  addresses in every spelling browsers accept (decimal, octal, hex, IPv6).
- Backward compatibility: new response fields are additive, safe defaults, referrers
  recorded by earlier releases still aggregate.
- Evidence: [summary](sample-runs/ambiguous/ENGINEERING_SUMMARY.md),
  [clarification packet](sample-runs/ambiguous/approvals/A-001-clarification.md),
  [first plan packet](sample-runs/ambiguous/approvals/A-002-plan-signoff.md).

---

## Resilience drill

Faults can be injected into any scenario from the command line, and budgets tightened:

```bash
sdlc run --scenario greenfield --target work/drill --run-id drill --approvals scripted \
         --fault documentation:permanent --max-invocations 8
#   architecture   [drill] injected transient fault -> retried
#                  SAFE-STOP: agent invocation budget exhausted (8/8)       exit code 4, state kept
sdlc resume drill --approvals scripted --max-invocations 40
#   documentation  [drill] injected permanent fault
#   documentation  primary agent 'doc_writer' failed -> FALLBACK to 'doc_template'
#   documentation  SUCCEEDED via fallback agent
#   promotion      PROMOTED to target
```

Other controls to try: `sdlc stop <run>` while a run is in progress (safe-stop),
`--freeze` (a change freeze blocks promotion), `sdlc approve ... --decision reject`,
editing a file in the target between plan approval and release (promotion is refused
because the baseline drifted), `sdlc rollback <run> --by <you>` after a release.
