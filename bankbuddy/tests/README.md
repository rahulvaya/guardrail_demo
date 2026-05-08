# tests/

Test layout for BankBuddy.

```text
tests/
  unit/         # per-module unit tests (no I/O)
  integration/  # spin services up via compose; hit real endpoints
  contract/     # verify each concrete provider satisfies its ABC
```

Real test code arrives in Phase 1f. Phase 1a only fixes the layout.

## Conventions

- `pytest` + `pytest-asyncio`.
- Each service has its own `conftest.py` under its test folder when needed.
- Contract tests import only `bankbuddy_shared.interfaces` and the provider under test - never service-internal code.
