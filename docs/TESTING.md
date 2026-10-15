# Testing approach, limitations and trade-offs

## Testing approach

There are two test-suites because there are two products.

### The orchestrator (`tests/`, 121 tests, ~25 s)

| Layer | File | What it proves | How |
|---|---|---|---|
| Foundations | `test_foundations.py` | Audit chain detects edits, deletions and reordering and is safe under concurrent writers; the sandbox refuses every path escape; apply/rollback is exact and atomic; promotion can be undone; each policy scanner fires on a violation and stays quiet on safe code; graph validation; plan gates; codebase analysis and blast radius | Pure unit tests on real files in a temp directory |
| Engine behaviour | `test_engine.py` | Parallel execution and joins; lineage; bounded retry with backoff; feedback loop; fallback; scope, sandbox and secret violations; pause/resume at checkpoints; rejection; role enforcement; change-control before a high-impact task while other work continues; change request -> re-plan with a task diff; revalidation without recomputation; targeted rework and its bound; safe-stop/resume; budgets; change freeze; target drift; crash recovery; faults fire once across a resume | The **real** graph, gates, policy, workspace and audit log. Only the model (a programmable in-memory provider) and the two agents that shell out (pytest runner, OpenAPI export) are replaced by stubs with the same contract |
| Providers | `test_providers.py` | Playbook replay semantics; both live adapters (Anthropic, OpenRouter) request schema-constrained output and map every failure (rate limit, daily quota, 5xx, bad key, prose, schema mismatch, truncation) onto retry / feedback / fail | A fake SDK client; an `httpx` mock transport |
| End to end | `test_scenarios.py` | The three scenarios through the unmodified pipeline, the greenfield one driven through the CLI with real deferred checkpoints; the specific recovery in each scenario; audit integrity; fleet metrics; release rollback; baseline guards; **the committed `service/` equals the pipeline's output** | Nothing stubbed: change sets are applied, the generated suite is executed by pytest in the sandbox, the app is imported to export OpenAPI |

Principles behind the choices:

- **Test recovery paths on purpose.** Retry, fallback, rollback, rework, safe-stop and
  crash recovery only run when something goes wrong, so each has a test that makes it go
  wrong, and the scenarios seed faults declared as data and labelled in the audit log.
- **Concurrency is asserted, not assumed.** The parallelism test uses a barrier that only
  opens if two tasks are executing at the same moment; ordering tests use audit sequence
  numbers.
- **Time and randomness are injected** (sleep function, clocks, code factory, key factory),
  so nothing sleeps and nothing is flaky by construction.
- **The harness found real bugs**, which is the point of it: a race where a parallel
  read-only stage listed a file that was mid-rename, and a relative-path error when running
  from the repository root. Both have regression tests.

### The generated service (`service/tests/`, 201 tests, ~3 s, 100% line coverage)

The pipeline's verification stage runs this suite on every run and gates on the result,
coverage (>= 85%) and requirement traceability (every functional requirement id must be
named by a test). Tests are layered like the code: codec and configuration (pure),
repository and migrations (real SQLite files, including an upgrade from a populated v1
database and a migration that fails halfway), service rules (injected clock), HTTP API
(ASGI test client), plus rate limiting, destination vetting and a privacy test that reads
the database back.

Additionally verified by hand while building: the service was started with uvicorn and
driven over real HTTP through every endpoint and error path (create, redirect, stats,
alias + TTL, 409, unsafe-destination 422, 429 with Retry-After, 410 after delete, 404).

### Live runs

The greenfield requirement was run three times against a real model:
`nvidia/nemotron-3-super-120b-a12b:free` through OpenRouter, whose free tier allows 50
requests a day. The checkpoints were decided by the coding assistant acting for the user
during the trial, and every record says so. No human reviewer took part. The pipeline's own
summaries of the two complete runs are in
[sample-runs/live-openrouter/](sample-runs/live-openrouter/).

| Run | Outcome | What happened |
|---|---|---|
| live1 | Stopped by the operator | Requirements output failed schema validation twice: the provider's schema conversion had removed a field named `title`. |
| live2 | **Failed safely** after 2 rework rounds; target untouched | Clarification, then plan sign-off sent back: the design contradicted the answers, because answers sent with `approve` had been dropped. Re-plan v2 (9 tasks became 4). One task hit `max_tokens` twice. Safe-stop on the 900 s active-time budget, resumed with an audited extension. Verification failed 11 of 32 tests; two rework rounds did not converge, because each call opened a new connection to an in-memory SQLite database. 32 node attempts, first-pass success 0.82, 8 rollbacks, MTTR 315 s. |
| live3 | **Stopped** on its invocation cap (23 of 22); target untouched | Clarification, then three plan sign-offs that caught real design errors: SQLAlchemy is not on the dependency allow-list; SQLite has no `SELECT ... FOR UPDATE`; a wall-clock timing test was planned for the unit suite. The policy gate caught syntax errors in two generated files and rolled them back. The final routes module failed at import time inside FastAPI, so no test module could be collected. Rework began, and the cap stopped the run. First-pass success 0.55, 2 re-plans, 8 rollbacks, MTTR 107 s. |

**What this shows.**
- No live run reached release. The free model could not produce a working multi-module
  service within the rework bound.
- The control plane behaved as designed against unscripted output:
  - every gate that fired caught a genuine error;
  - nothing failing reached the target;
  - each stop was graceful and resumable;
  - both audit chains verify.
- A stronger model (`--provider anthropic`, or a paid OpenRouter model) has not been
  measured.

**Defects the live runs found**, which the scripted runs could not:

| # | Defect | Fix | Regression test |
|---|---|---|---|
| 1 | Strict-schema conversion stripped a *field* called `title` along with the `title` keyword | Keep `properties` keys | `test_openrouter_strict_schema_keeps_fields_named_like_keywords` |
| 2 | `sdlc approve --decision approve --answer ...` silently dropped the answers | Refuse `--answer` unless `--decision changes`; the pause message shows the right command | `test_answers_are_refused_with_a_decision_that_would_drop_them` |
| 3 | A run that safe-stopped on time could not be given more time | `--max-wall-clock` on `run` and `resume`, audited as `budget.extended` | `test_resume_can_extend_budgets_and_the_extension_is_audited` |
| 4 | Rework blame searched feedback after it was cut to budget, so later failing files were missed | Blame searches the full output; the gate lists every failing test first | `test_blame_sees_every_failure_even_when_feedback_is_cut_to_budget` |
| 5 | A module that failed to import was reported as `.py::tests.test_db`, so blame could not attribute it | Collection errors are named by file | `test_junit_failures_name_the_test_file_even_for_collection_errors` |
| 6 | 16k `max_tokens` truncated a reasoning model's output | 32k default | Configuration; confirmed in live3, which had no truncation |
| 7 | The conventions never said what `URLSHORTENER_DB_PATH=":memory:"` implies | The convention now states it | Prompt content; live3's design adopted it (ADR-3) |
| 8 | The implementer never saw the design's decisions | ADRs are part of its context | Prompt content |

## Limitations

Stated so they can be weighed, not discovered.

**Live model path**
- The OpenRouter provider has run against a real free model, and none of those runs
  reached release (see [Live runs](#live-runs)). The Anthropic provider has still only run
  against a fake client. Prompts were tuned only by the eight fixes above. Output quality
  with a stronger model is unmeasured.
- Free models are slow, taking 30 s to 4 min per call, and are limited to 50 requests a day.
  A live run costs 15–30 calls, so the default budgets are sized for scripted runs and need
  raising for live ones.
- In live mode the implementer makes one schema-constrained call per task with the
  relevant files as context. It is not a tool-using agent that explores the repository,
  and it does not run tests itself; feedback arrives through gates and rework rounds.
- There is no token or cost budget, only invocation, rework and wall-clock budgets.

**Demo content**
- The scripted playbooks are curated, recorded outputs. They show how the control plane
  behaves, not how good unassisted generation would be. Reviewers are simulated in
  `sdlc demo` and labelled as such in every record.

**Governance**
- Roles are asserted, not authenticated: `sdlc approve --by --role` trusts the caller.
  A real deployment needs identity (SSO), signed decisions and separation-of-duties rules.
- The audit log is tamper-*evident*, not tamper-proof: someone with write access could
  rewrite the whole chain. Anchoring the head hash externally (or WORM storage) closes that.
- Change control knows about paths and task kinds, not about calendars, tickets or CAB systems.

**Security scanning**
- The scanners are AST and regex heuristics for a few high-value rules. They are a
  guardrail, not a replacement for SAST, dependency audit or secret-scanning services.
  The whole-workspace scan and the documentation stage run in parallel, so a file written
  by that stage is covered by its own change-level scan rather than by the workspace scan.

**Engine**
- One process per run; state is a JSON snapshot. No distributed workers, no concurrent
  runs against the same target (the drift check turns that into a refused promotion rather
  than a corrupted one).
- Agents share the orchestrator's process and interpreter; isolation is the sandbox
  directory and path checks, not a container. The generated tests run as a subprocess with
  the orchestrator's privileges.
- Rework blame is a heuristic (task-owned file paths in the failure output). When it
  cannot attribute a failure it re-opens every task, which is safe but slow.
- A human change request re-runs everything downstream of its target unless the result is
  provably unchanged; there is no partial re-planning inside a task.
- Windows, macOS and Linux should all work (paths and line endings are normalised) but
  only Windows was exercised.

**Service**
- SQLite with one serialised connection: correct, simple, single-writer. Rate limits and
  the visitor-id key are per process. Both are documented with the seam to replace them.
- No authentication or per-user ownership of links. Destination vetting is syntactic and
  does not resolve DNS.
- The generated Dockerfile was not built here (the Docker daemon was not running).

## Trade-offs

| Chose | Over | Because | Gave up |
|---|---|---|---|
| Deterministic replay as the default | Requiring an API key | Reviewable, repeatable evidence of the orchestration; CI-friendly | A demonstration of raw model capability; the live runs are a separate, smaller record |
| Stubs for the two subprocess agents in engine tests | Running pytest in every engine test | 30+ behaviours tested in seconds; the real agents are covered end to end | A small gap between stub and real contract, closed by `test_scenarios.py` |
| Fail-closed gates (a crashing gate fails the node) | Fail-open | A guardrail that can be skipped by breaking it is not one | A gate bug stops a run |
| Stop instead of fail for budgets, freeze and drift | Treating them as errors | They are governance outcomes, resumable once resolved, and should not pollute failure metrics | Two terminal-ish states to explain |
| Rollback by before-images | Git | No assumptions about the target being a repository; exact per-node undo | No history, no merge |
| Reserving documentation paths for the docs stage | Letting tasks write docs | Makes parallel writes disjoint and the API reference single-sourced | A task cannot ship its own doc change |
| 100% coverage in the generated service | Stopping at the 85% gate | Small codebase; uncovered lines were all cheap to test | Nothing meaningful |
