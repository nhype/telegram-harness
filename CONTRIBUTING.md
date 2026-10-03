# Contributing

Issues and pull requests are welcome.

## Tests

```bash
python3 -m pytest tests harness/scripts/tests harness/tools/tests harness/plugins/herdr-control/tests
bash tests/test_install.sh --inner        # installer against stub CLIs (or without --inner: in docker)
shellcheck -x install.sh bin/harness lib/common.sh tests/*.sh
```

Slower, optional:

```bash
bin/harness test-plugins                  # plugin tests inside Hermes's runtime (they patch its internals)
bash tests/test_hermes_integration.sh     # real Hermes in a container (needs docker)
```

## Rules

- Keep the bridge and the registry dependency-free (Python stdlib, ≥ 3.11).
- Project-specific settings belong in `herdr-pipeline.json` or the skills' Project notes, never in code.
- Every behavior change comes with a test that fails without it.
- `tests/test_leak_guard.py` must pass: no secrets, no personal ids, no machine-specific paths in code.
