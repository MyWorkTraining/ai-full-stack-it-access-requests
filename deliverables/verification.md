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

In Steps 1–11, each step has a test-first commit before its implementation commit. The first-step failing run is also recorded in the coding session. Steps 2–11 retain failing and passing run logs under `step-checks/`. Step 5's first red run also exposed the paraphrase filename mismatch. The input was renamed IT-P01.json to preserve the stated expected facts under the supplied policy. Step 8's harness initially patched sockets before SSL class initialization. It was corrected before the test-first commit was finalized, and the retained red run shows missing stale-claim fencing and generation publication. No full-suite failure was observed after any of those steps.

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

## Section 508 accessibility

Step 12 covers the internal CLI, its operator-facing outputs, and the rendered README, accuracy report and verification report. Operators need to identify requests and their state without color or vision, operate without a mouse or timed input, keep their terminal preferences, and recover interrupted or failed work using the same information and permissions. The application ships a local CLI. There is no public workflow, application web UI, or application support service in this build. R-110 and R-113 are inapplicable for that reason. [Revised 508 Standards](https://www.access-board.gov/ict/) · [WCAG 2.0](https://www.w3.org/TR/WCAG20/)

### Replay the acceptance checks

Run from the repository root in the documented virtual environment. Use mock fixtures and leave `API_KEY` unset. `test_r109_contract` creates the mixed-state fixture, gates IT-001, checks live queue output, terminates the worker and injects claim expiry for reproducibility. It then releases the gate, resumes, retries IT-010 with the recovery manifest, inspects history, exports records and generates a report. The test uses the guarded mock subprocess runner. For the interactive check, use the [Step 12 replay](../planning/plan.md#test-plan) with ordinary claim expiry and repeat the workflow in fresh state with the same permissions. Add `--format text` after each subcommand. Append `--script tests/mock_responses/manifest.json` to the displayed retry command to replace the mock failure response with the completed response.

```bash
env -u API_KEY python3 -m unittest discover -s tests -k test_r109_contract -v
env -u API_KEY python3 -m unittest discover -s tests -v
```

In the mixed queue, expect IT-001 to be unfinished, IT-002 to show `Security Review / review / review-risk-then-owner`, and IT-010 to show `IT Service Desk / error / retry-extraction` with a retry command. Resume should produce `System Owner / review / review-request` for IT-001 and preserve IT-002's committed result. Retry should produce `IT Service Desk / needs-information / clarify` for IT-010 and retain the failure in history. Read the export path and compare the linked extraction and queue records. Read the report path and verify 231 of 231 exact scalar matches in the independent 21-case mock accuracy run. These results describe the mock pipeline.

### Observed results

The first targeted run discovered one test and failed five subchecks. Help omitted the text format and the mixed queue, progress, history and accuracy commands rejected it. The repair adds opt-in labelled text with stable field order, one-time progress messages, readable errors and shell-ready retry guidance. The existing JSON output and export formats are still exercised by the full suite. The accuracy report now has navigable headings. The targeted regression passed after the repair.

| Requirement | Check | Evidence |
|---|---|---|
| R-106 | A-106, comparable workflow access | The guarded mock replay and the keyboard-driven terminal run obtained the expected mixed states, recovery outcomes, history, export and report. Speech presentation is assessed separately below. |
| R-108 | A-108, keyboard-only operation | In Xfce Terminal, keyboard input exercised help, intake, queue, interruption, resume, retry, history, export and accuracy. Commands use ordinary shell input with explicit submission. The application presents no interactive prompts or custom focus targets. Recovery used deterministic claim expiry in the virtual replay. |
| R-109 | A-109 and `test_r109_contract`, CLI text | The regression verifies labels, reading order, state, owner, action and retry, progress, history and report output with no terminal escape or cursor-rewrite sequences. The Orca queue navigation check passed as recorded below. |
| R-110 | A-110, web application | Inapplicable. The delivered interface is the local CLI. The Firefox checks below concern rendered documents. |
| R-111 | A-111, rendered documents | Python-Markdown 3.5.2 with tables, fenced code and heading anchors rendered all three documents to HTML. The structure check found a single top-level heading in each, sequential levels, named links and table headers. The documents contain text and tables with no images, audio or video. |
| R-112 | A-112, accessibility handover | The README explains the text option, supported commands, reading order, retry and progress, and terminal-owned preferences. Its documented commands were exercised in the keyboard workflow. |
| R-113 | A-113, application support service | Inapplicable. The application supplies documentation and CLI help, with no help desk, support channel or referral service. |
| R-114 | A-114, evidence and replay | This record identifies the tests, versions, scope, results and what remains to be verified. Claim conformance only after all applicable checks pass. |

### Orca queue navigation

On November 19, 2026, a keyboard-only replay used Ubuntu 24.04, Xfce Terminal 1.1.3, Orca 46.1 and `less` 590 in a virtual desktop. Speech Dispatcher used eSpeak NG and a virtual PulseAudio sink. The replay captured Orca's spoken text from the Speech Dispatcher log and confirmed that the sink received audio.

At the mixed queue stage, set `replay_db` to the path of the replay database. Save the plain-text queue and open it in the pager.

```bash
python3 -m intake queue --db "$replay_db" --include-pending --format text > queue.txt
less -j1 queue.txt
```

Press `g` for the first request. For the later requests, press `g` and search with `/Document: IT-002` or `/Document: IT-010`. In Orca's desktop layout, use `Insert` + numpad `7` to read the first visible line and numpad `9` to read each next line. Use numpad minus to leave flat review before returning to pager navigation. The spoken lines followed this order within each request.

| Request | What Orca read during navigation |
|---|---|
| IT-001.json | Document identifier, then work state Running. |
| IT-002.json | Document identifier, work state Completed, owner Security Review, status `review`, and next action `review-risk-then-owner`. |
| IT-010.json | Document identifier, work state Failed, owner IT Service Desk, status `error`, next action `retry-extraction`, the provider error, and the retry command. Orca read both terminal lines of the wrapped command in sequence. |

This pass covers queue navigation in the labelled text mode. Rendered-document screen-reader navigation remains open.

### Applicable software and document criteria

For the CLI, apply the four non-Web success-criterion exceptions in E207.2 (2.4.1, 2.4.5, 3.2.3 and 3.2.4) and the terminology substitutions in E207.2.1. E207.3 governs the complete multistep CLI workflow. For covered non-Web documents under E205, apply E205.4 and E205.4.1. Apply 602.3 to electronic support documentation. Evaluate the three documents here in a rendered HTML reading environment, including its headings, links and table semantics.

| Provision | Scope and evidence |
|---|---|
| E203.1–E203.2, E207.3 | The same internal operator commands support the complete intake and recovery task. A-106 and A-108 trace the access needs above to the keyboard and assistive-technology test. Public access is inapplicable for this delivery. |
| WCAG 1.1.1, 1.2.1–1.2.5, 1.4.2, 1.4.5 | The CLI and reviewed documents expose text and text structure. There is no app-supplied non-text content, audio, video or image of text. |
| WCAG 1.3.1–1.3.3, 1.4.1 | CLI labels and state names survive without color or layout, as exercised by TP-54. Rendered heading levels, link names, table headers and content order were inspected in HTML. The instructions use command and field names, with no sensory-only references. |
| WCAG 1.4.3–1.4.4, 2.4.7, 503.2 | The CLI emits unstyled text and leaves font, color, contrast and cursor to the terminal. The rendered-document test uses black text and dark blue links on white, with a visible focus outline. Text resizing and platform settings are tested in the presentation layer. |
| WCAG 2.1.1–2.1.2, 2.2.1–2.2.2, 2.3.1 | A-108 exercised ordinary shell input and return to the prompt. There is no application time limit for keystrokes, rotating display, animation or flashing. Provider timeouts apply to background processing, with persisted status and recovery. |
| WCAG 2.4.2–2.4.4, 2.4.6 | Commands and reports have descriptive names. The HTML test sets a page title, preserves heading levels, and verifies that links have names. CLI commands announce their purpose in help, and result fields have labels. |
| WCAG 3.1.1–3.1.2, 3.2.1–3.2.2 | Application labels and repository-authored reading content are English. The HTML test rendering declares English. Commands take effect on explicit invocation, with no custom fields that change context on focus or input. |
| WCAG 3.3.1–3.3.4 | Help describes inputs, and invalid CLI arguments are identified in text. A failed extraction associates the error and retry command with the affected document. There is no legal or financial submission, approval or deletion control. Recovery retains prior attempts and source revisions. |
| WCAG 4.1.1–4.1.2, 502 | The platform presents the CLI through an ordinary terminal text control. The renderer provides native HTML headings, links and table headers. The application supplies no platform accessibility service or custom controls. AT-SPI and screen-reader observations assess interoperability and non-disruption separately from the structured text checks. |
| E204.1 / Chapter 3, 503.3–503.4, 504 | For functions requiring functional performance coverage, A-106–A-109 examine operation without vision, without color, with reduced vision and with keyboard-only input. Hearing and speech are not required to operate the CLI. The application has no embedded assistive-technology UI, synchronized media player or interactive document authoring tools. |
| E208 / 602, 603 | A-111 and A-112 address electronic support documents and compatibility instructions. Support-service provisions are inapplicable for this release. |

## External final checks

R-104: after a submitted commit exists, inspect `git log -1 --format=fuller` and the facilitator's paired session-capture record using its supplied instructions. Entire automatically captures coding sessions at commits; no manual checkpoint/setup command is needed here. Pairing to a final submitted commit remains an external acceptance check.

R-105: inspect `git status --short`, `git log -1 --oneline`, `git branch -vv`, and `git ls-remote --symref origin HEAD` at final submission; then verify the required content/session capture is on the default remote branch and Classroom contains only the root repository URL. No push or Classroom submission was performed by this implementation task.
