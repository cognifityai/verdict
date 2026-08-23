# Publishing a synchronized Verdict alpha

Verdict publishes `cognifity-verdict`, `cognifity-verdict-eval`, and
`cognifity-verdict-inspect` from one immutable release tag. The release workflow
builds and tests all three distributions before publishing them in dependency
order: core, eval, then inspect.

## Normal release

1. Confirm the package versions and root workspace version match the intended
   `vX.Y.Z` tag.
2. Merge the reviewed release PR and create the GitHub release from that exact
   main-branch commit.
3. Let `.github/workflows/publish.yml` run its full test, artifact, publication,
   and default-container checks.
4. Verify all three public package pages show the same version and that the
   workflow's default container imports that exact version.

Each package job has its own protected PyPI environment and OIDC identity. No
long-lived upload token is used.

## Resume after a partial publication

PyPI versions and files are immutable: do not delete, overwrite, or attempt to
roll back a package that was already uploaded. Rerun the failed workflow jobs
from the same GitHub release. Before every upload, the release guard queries the
target PyPI version and validates every remote filename and SHA-256 against the
reviewed artifact directory, which must contain exactly one wheel and one
source distribution:

- an absent version stages and publishes both reviewed files;
- a matching remote subset stages and publishes only the missing reviewed
  file;
- a byte-exact complete version is skipped and the next package may proceed;
- an unknown filename, changed digest, malformed response, or failed PyPI query
  fails closed.

This makes interruption after either the wheel or source distribution, as well
as core-only and core-plus-eval package states, resumable without re-uploading
immutable files. If the original workflow artifacts have expired, or a rebuilt
artifact does not match what PyPI already contains, stop and open a release
incident. Do not bypass the guard or create a replacement file under the same
version.

## Verification and rollback boundary

The release is complete only after all three package jobs and the published
default-container smoke test pass. A public PyPI upload cannot be rolled back;
the recovery action for an incorrect release is to stop promotion, document the
incident, fix forward under a new version, and leave the immutable file history
intact.
