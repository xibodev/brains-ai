## Outcome

<!-- What user or operator outcome changes? -->

## Contract mapping

- Feature/backend IDs (`F*` / `B*`):
- Personas and journeys (`P*` / `J*`):
- Acceptance criteria (`AC-*`):
- Core backlog item, if applicable:

## Current and target behavior

<!-- Separate observed current behavior from the intended contract. -->

## Implementation and recovery

<!-- Summarize changed UI/API/control/data surfaces and failure/recovery behavior. -->

## Validation

<!-- List exact commands and results. Redact credentials, personal data, private paths, and host details. -->

- [ ] Documentation and traceability checks
- [ ] Relevant lint, format, type, and Python tests
- [ ] Browser typecheck/build/bundle comparison, if applicable
- [ ] Browser journeys, if applicable
- [ ] Migration and recovery checks, if applicable

## Review checklist

- [ ] The change stays within the advertised core or explicitly updates the product contract.
- [ ] Protected routes and scoped data keep their authentication and authorization checks.
- [ ] Human-governed actions do not gain an execution bypass.
- [ ] Persistent changes include migration, failure, and recovery behavior.
- [ ] Frontend calls match server routes and changed browser source includes the rebuilt bundle.
- [ ] Secrets, personal identifiers, private configuration, and runtime state are absent.
- [ ] Documentation describes current behavior, targets, and evidence gaps separately.
