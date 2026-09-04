# Brains Quality Gates

These gates define reproducible checks for the supported public product. They record
commands and healthy outcomes, not dated observations or candidate-specific proof.

## Fast checks

Run the smallest relevant tests while developing, then run:

```console
python scripts/check_docs.py
python scripts/check_traceability.py
```

A healthy result exits zero and reports no documentation or traceability drift. The full
repository gate owns generated core-surface validation with a fresh distribution build.

## Full repository gate

```console
python scripts/run_quality_gates.py
```

The runner owns the authoritative CI order. A healthy result means formatting, static
analysis, unit and integration tests, frontend checks, distribution checks, and the
generated core-surface manifest all pass. Do not copy a dated pass count into docs.

## Isolation

Tests that can alter a service, database, state directory, client configuration, or
listener must use disposable synthetic state. Run those tests in an isolated container
or disposable VM. Never mount an operator's Brains state or client configuration and
never bind the default Brains host ports.

Network access may be enabled while fetching build dependencies. The test runtime should
use no host mounts or published ports unless the test validates a synthetic boundary.

## Native lifecycle probes

Platform service-manager behavior cannot be established by a Linux container. Use the
repository's native lifecycle workflow on each supported operating system. The workflow
must exercise the real installer and service manager, bind the installed artifact to the
candidate package manifest, and emit machine-readable preparation, verification, and
cleanup records.

Preparation is not completion. A reboot-dependent check remains incomplete until a
post-reboot verification record is bound to the same journey and machine-observed boot
transition. Cleanup must restore the captured pre-test state and fail closed when that
restoration cannot be proven.

## Acceptance

Acceptance tests map stable `F*`, `B*`, `J*`, `O*`, and `AC-*` identifiers to observable
user outcomes. Passing lower-level tests does not replace an end-to-end acceptance check
when a change crosses CLI, MCP, browser, persistence, or service boundaries.

## Documentation

Public documentation states current supported behavior, target contracts, and unresolved
evidence gaps separately. Record a command and its healthy result instead of a manual
verification timestamp, commit hash, test count, screenshot pack, or delivery diary.

The core and frozen backlogs contain actions only. Completed work is removed; Git history
retains its disposition.
