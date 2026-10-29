# Upstream tracking and retained builds

The builder checks the latest stable `Wei-Shaw/sub2api` release daily at
00:20 Asia/Shanghai (16:20 UTC). GitHub's scheduled delivery can be delayed.
Pushes to `main` and manual runs with an empty upstream ref also check for a
new stable release. A scheduled check with no new release skips the image build.
Manual runs can specify an explicit release tag to rebuild an older version.

The build resolves a release to a full upstream commit and source SHA256,
applies all three patches, and runs the frontend/backend checks and disposable
smoke tests at `/`, `/s2a/` and `/tools/ai/`. The build job has read-only
repository permissions and does not receive the publisher's package-write token.
If upstream raises its minimum Go version, the resolver pins the corresponding
official `library/golang` image by digest. Existing newer compiler pins are kept;
other base images are not silently changed. Unavailable compiler images stop the
build rather than use an unpinned fallback.
Failed patches, builds or tests never reach the publisher. Production containers
and databases are not accessed by this workflow.

## A separate version for every build

An example version is:

`0.2.18-subpath.2-run.123456789.1`

It records the upstream version, patch revision, GitHub run ID and run attempt.
A new run or a complete rerun produces a different version, even for the same
upstream release and builder commit. Local builds use a UTC timestamp and random
suffix instead. The unique version is embedded into the application and OCI labels.
`build.py --image` accepts a repository without a tag; the unique tag is appended.

That unique tag is pushed to `ghcr.io/suzuira01/sub2api-subpath` and never replaced.
The publisher refuses a pre-existing image version or build release. It also
updates the movable `latest` image alias and GitHub Latest release when the new
build is a newer verified stable build from `main`. Older upstream rebuilds,
older patch revisions, prereleases and branch builds cannot move Latest backwards.
Commit-only tags and bare upstream-version tags remain unchanged. Updating the
Latest pointers does not delete or replace any older uniquely versioned image.

Each publication also creates a separate `build-<version>` GitHub release with
the image digest, upstream and recipe commits, source/patch checksums, tested
paths and workflow link. Pin deployments to a version or image digest. Neither
old images nor releases are deleted by any automation in this repository.
Actions image archives and receipts have 90-day retention; versioned GHCR images
and GitHub releases are independent of that artifact expiration.

An existing version causes publishing to stop rather than overwrite it. To make
a new build after a partial publication, use **Re-run all jobs** or start a new
manual run. Re-running only a failed publisher reuses the original tested artifact
and therefore cannot replace a version that has already been pushed.

## Advancing the lock

After a GitHub account or repository rename, update `builder_repository` in
`upstream.lock.json` to match the current `GITHUB_REPOSITORY` before rebuilding.
The lock supplies the image source label and exported provenance; the publisher
keeps requiring an exact repository match. A new build publishes under the new
GHCR namespace. GitHub moved the existing package from `celesrain/sub2api-subpath`
to `suzuira01/sub2api-subpath`; its old version tags and image digests are retained,
but the old pull address no longer works. Existing GitHub release records remain
unchanged, and their provenance can still be compared when advancing Latest.
The deployment template uses the new namespace with its existing fixed version.
Existing deployments need the same address change before their next image pull;
upgrading their application version remains a separate deployment step.

The new lock is initially local to the build. Only after tests and publishing
succeed does the publisher advance `upstream.lock.json` on `main`. It updates only
the upstream ref, commit, commit date and archive checksum, plus a verified compiler
upgrade when required. Other image pins, patches and deployment settings remain
unchanged. A concurrent change to `main` is never
force-pushed over; a later check can retry promotion.

The lock-only commit uses the workflow's `GITHUB_TOKEN`. GitHub does not create
another push-triggered workflow for that commit, avoiding a rebuild loop. The
daily workflow itself performs the check, build and publication; it does not
depend on a token-generated commit triggering a second workflow.

This repository builds and publishes images. Server deployment is a separate step.
