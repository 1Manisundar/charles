Node: Release readiness
Why a human is needed: Promoting the change onto the target codebase is a high-impact action.

Release checklist:
  - [x] All planned tasks implemented - 5/5 tasks
  - [x] Test suite passes - 201 tests, 0 failures, 0 errors
  - [x] Coverage at least 85.0% - 100.0%
  - [x] Every functional requirement has a test - untested: []
  - [x] No blocking security or compliance findings - {'high': 0, 'low': 0, 'medium': 0}
  - [x] API schema exported and reference generated - 8 operations
  - [x] Plan signed off by a human - approved checkpoints: ['change-control:T4', 'plan-signoff']
  - [x] Audit trail intact - 167 records, chain intact

Files that will change in the target:
  - added: tests/test_api_hardening.py
  - added: tests/test_privacy.py
  - added: tests/test_ratelimit.py
  - added: tests/test_safety.py
  - added: urlshortener/privacy.py
  - added: urlshortener/ratelimit.py
  - added: urlshortener/safety.py
  - modified: CHANGELOG.md
  - modified: README.md
  - modified: docs/API.md
  - modified: openapi.json
  - modified: tests/test_config.py
  - modified: tests/test_service.py
  - modified: urlshortener/__init__.py
  - modified: urlshortener/api.py
  - modified: urlshortener/config.py
  - modified: urlshortener/main.py
  - modified: urlshortener/migrations.py
  - modified: urlshortener/repository.py
  - modified: urlshortener/schemas.py
  - modified: urlshortener/service.py
