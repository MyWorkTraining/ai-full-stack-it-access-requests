# Verification and handover

All provider observations here use saved mock responses or intercepted transport, with no live model call. The scalar result is pipeline validation, not live model accuracy. Expected facts come from planning/plan.md and exact message/policy evidence in tests/expected; the mock provider never reads that oracle.

## Commands and comparisons

Run after `uv sync --frozen` and activation from the repository root:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
python3 -m unittest discover -s tests -k Step11 -v
python3 -m intake run --input materials/requests --db var/verification.sqlite --provider mock --responses tests/mock_responses --output deliverables
python3 -m intake queue --db var/verification.sqlite --include-pending
python3 -m intake history --db var/verification.sqlite --document IT-001.json
python3 -m intake accuracy --input materials/requests --synthetic tests/fixtures/IT-P01.json --expected tests/expected --responses tests/mock_responses --provider mock --output deliverables/accuracy.md
```

The full suite compares fixed source-derived facts and ordered routing, not values copied from extraction runs. API_KEY reads and sockets are forbidden by test guards; CLI subprocesses install their own guards. Provider authentication tests use an injected inert function; actual environment retrieval is inspected statically. Source-loading and database tests need no provider invocation; all extraction/provider tests consume responses saved in tests/mock_responses. Setup downloads are distinct from test execution.

## Red/green observations

Each step has a test-first commit before its implementation commit; the first-step failing run is also recorded in the coding session. Steps 2 onward retain exact failing and passing run logs under step-checks/. Step 5's first red run also exposed the paraphrase filename mismatch; the input was renamed IT-P01.json to preserve the stated expected facts under the supplied policy. Step 8's harness initially patched sockets before SSL class initialization; it was corrected before the test-first commit was finalized, and the retained red run shows missing stale-claim fencing and generation publication. No full-suite failure was observed after any completed step.

| Step | Targeted tests | Full suite after implementation | Evidence |
|---|---:|---:|---|
| 1 | 2 passed after missing-provider failures | 5 passed | test-first commit e72e461; implementation 03f6d78; session and step-01 logs |
| 2 | 5 passed after 5 failures | 10 passed | step-02-red/targeted/suite.log |
| 3 | 23 passed after 23 failures | 33 passed | step-03-red/targeted/suite.log |
| 4 | 9 passed after missing-adapter failures | 42 passed | step-04-red/targeted/suite.log |
| 5 | 33 passed after missing-routing failures | 75 passed | step-05-red/targeted/suite.log |
| 6 | 7 passed after 7 failures | 82 passed | step-06-red/targeted/suite.log |
| 7 | 14 passed after 14 failures | 96 passed | step-07-red/targeted/suite.log |
| 8 | 2 passed after 2 failures | 98 passed | step-08-red/targeted/suite.log |
| 9 | 3 passed after 3 failures | 101 passed | step-09-red/targeted/suite.log |
| 10 | 3 passed after 3 failures | 104 passed | step-10-red/targeted/suite.log |
| 11 | 10 passed after packaging/provenance/recovery and supplemental progress/publication failures | 114 passed | step-11-red.log, supplementary red logs, step-11-targeted.log and step-11-suite.log |

Dependency setup: `uv lock` initially failed in the network-restricted sandbox, then succeeded with approved registry access; `uv sync --frozen --cache-dir /tmp/northstar-uv-cache` succeeded. This is a setup observation, not a test or provider observation.

## Operational checks and intended assertions

| Operational situation | Test | Assertion |
|---|---|---|
| Ready results during slow intake | test_r44_contract, test_r02_contract | Second document can be inspected while another saved response remains blocked. |
| Provider errors, limits and retry budget | test_r39_contract through test_r43_contract, test_retry_budget | Explicit current error/null extraction/retry action; bounded retry timing; unrelated successes survive. |
| Unchanged rerun / changed content | test_r47_contract, test_r48_contract | No extra reusable extraction call; revised source has a new hash/result and retained earlier version. |
| Relevant configuration/policy change | test_r51_contract, test_r52_contract | Affected results refresh with identifiable producing settings and policy. |
| Failed retry after success | test_r69_contract | Latest current error and null extraction; earlier valid record stays in history with original attempt/source. |
| Interrupt and resume | test_r54_contract, test_r02_contract | Recorded finished/unfinished states are distinguishable; a terminated subprocess's abandoned claim is resumed after injected lease expiry without rerunning completed work. |
| Overlapping local processes / stale claims | test_r53_contract | One coherent current record, distinct attempt history, stale claim rejected. |
| Changed source finishes before old response | test_changed_source_overlap | Current source keeps corrected end_date=2026-11-09; late older end_date=2026-11-06 remains historical. |
| Same-version queue/extraction publication | test_r55_contract | Fault injection around generation writes/publication exposes a complete matching old/new generation. |
| Overlapping exporters | test_export_publishers_do_not_revert_current | Publisher lock prevents a slow old snapshot from replacing a newer published snapshot. |
| Prompt version changes during recovery | test_resume_configuration_refresh | New attempt uses actual current prompt hash, preserving the older attempt's provenance. |
| Mixed recovery configurations | test_recovery_configuration_isolation | Each pending document uses its own recorded mock script/settings, with the expected success/error outcome. |
| Partial progress during resume | test_resume_keeps_other_work_available | A blocked resumed message leaves other resumed results available. |
| Independent factual-error detection | test_r70_contract, test_accuracy_routing_diagnostics | WrongSystem, fabricated evidence, nonliteral purpose and wrong owner are reported against independent source expectations. |
| Packaging, examples and mock labels | test_readme_command_smoke, test_package_manifest_contract, test_r102_contract | README commands work in isolated paths; dependency artifacts exist; 20 current outputs state mock origin and match source facts. |

Repair observations: final-step tests detected stale prompt attribution, cross-document configuration reuse, serialized resume progress and concurrent publisher reversion; these are covered by the named regression tests. The final targeted run passed all 10 step-11 tests and the complete suite passed all 114 tests (see the retained logs). The generated mock intake had 20 current records, zero extraction errors and zero pending records. The accuracy command observed 231/231 exact scalar comparisons across the 21 source-derived expected cases, with no automatic evidence/routing findings; this is a mock pipeline observation, not live model accuracy.

## Operating assumptions and limits

Supported arrangement: concurrent CLI processes on one host using local SQLite and POSIX advisory file locks, symlinks, rename and fsync; multi-host/shared network filesystems are not supported or verified. Current reads are transactional database views; static files are point-in-time snapshots and readers must pin one generation across multi-file inspection. Default concurrency is four requests, request timeout is 30 seconds and claim lifetime is timeout plus 5 seconds. Retryable failures have configurable bounded exponential retries, default zero; there is no shared organization-wide request-rate coordinator.

The brief's 200–400 ordinary daily messages and roughly 2,000-message burst are context. No 2,000-message load test, throughput/latency SLA, distributed recovery or actual power-loss test was performed. Process termination, fake clock expiry, thread/process barriers and publication fault hooks exercise specific recovery paths; they do not simulate every filesystem/hardware outage. Mock fixture sequences reset per mock process, and fixtures intentionally contain known facts. There are no live provider/credential observations, and mock accuracy cannot establish general extraction quality, universal attack resistance or arbitrary prose support. Business purpose/issue prose beyond deterministic checks needs human source review; nonliteral purpose text is flagged, not automatically rejected as false.

## External final checks

R-104: after a submitted commit exists, inspect `git log -1 --format=fuller` and the facilitator's paired session-capture record using its supplied instructions. Entire automatically captures coding sessions at commits; no manual checkpoint/setup command is needed here. Pairing to a final submitted commit remains an external acceptance check.

R-105: inspect `git status --short`, `git log -1 --oneline`, `git branch -vv`, and `git ls-remote --symref origin HEAD` at final submission; then verify the required content/session capture is on the default remote branch and Classroom contains only the root repository URL. No push or Classroom submission was performed by this implementation task.
