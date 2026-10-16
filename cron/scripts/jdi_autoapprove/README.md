# JDI auto-approval source

This package is the maintained source for the existing `~/.hermes/scripts/jdi_autoapprove` runtime. It was adopted from the local runtime during the workstation source-map recovery on October 16, 2026. Adoption does not authorize changing approval policy or deploying this checkout.

The package depends on sibling `boris_intake` modules maintained in this repository and a deployed sibling `slack_read.py`, maintained in `hermes-control-plane/scripts/slack_read.py`. Deployment must provide those existing dependencies; do not substitute stubs or copy a whole runtime home over another checkout. `poll.py` resolves sibling imports relative to its script directory.

Runtime configuration, credentials, audit records, watermarks and deduplication state remain outside the repository. Preserve them during deployment. Before any later rollout, compare all live file hashes with the adoption receipt, inspect changes since adoption, back up the exact affected files, and install only reviewed files. No scheduler or credential changes are required for source recovery.

Validation: run `scripts/run_tests.sh tests/cron/test_jdi_autoapprove.py` through the workstation heavy-run admission gate. The test file imports this checkout, uses temporary state, and replaces the external Slack/credential adapter with a refusal stub. Approval subprocesses and Slack reaction calls use fixture adapters. Do not invoke the poller or its network self-test as a source validation step.

The existing approval is a policy stamp, not independent code review or merge authorization. A review's `commit_id` binds the revision; it is not an atomic assertion that the current PR head still matches. See the [GitHub review API contract](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request). Current-head review evidence, human change-request clearance and merge policy remain separate requirements in the workstation readiness roadmap.
