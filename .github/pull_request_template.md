## Summary

Describe the problem and the scope of this change.

## Contracts And Risk

List the affected user workflow, data semantics, security/privacy boundaries,
extension points, cross-layer consumers, compatibility requirements, and public
claims. Use `N/A` only with a short explanation.

## Regression Evidence

Describe the counterexample that failed before the fix and passed afterward.
Name the real user path exercised and distinguish live, deterministic,
synthetic, mocked, and unverified evidence.

## Verification Checklist

- [ ] I ran `python scripts/smoke_test.py`.
- [ ] I ran `python -m pytest -q`.
- [ ] I ran `pnpm --dir ui test && pnpm --dir ui build` when UI code or assets changed.
- [ ] I added or updated tests for behavioral changes.
- [ ] I proved the regression test fails when the corrected behavior is removed.
- [ ] I exercised applicable error, boundary, unknown-extension, isolation, and
      cross-layer cases, or explained why each is not applicable.
- [ ] I used realistic nested/adversarial fixtures for changed trust boundaries.
- [ ] I exercised the real user path, or labeled the unrun portion `UNVERIFIED`.
- [ ] I updated documentation for user-visible changes.
- [ ] I searched the repository for stale descriptions and duplicate instances
      of the failed assumption.
- [ ] I reviewed every file in the final diff and ran `git diff --check`.
- [ ] I did not include credentials, personal data, trace databases, reports, or business artifacts.

## Not Tested And Residual Risk

List every check that could not be run, why it was not run, and what evidence is
still required. Do not use a passing test count as a substitute for this list.

## Documentation

List every documentation file and generated asset updated. If none changed,
explain why the implementation has no documentation impact.
