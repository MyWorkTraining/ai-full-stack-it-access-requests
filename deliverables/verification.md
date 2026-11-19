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

Run from the repository root in the documented virtual environment. Use mock fixtures and leave `API_KEY` unset. `test_r109_contract` creates the mixed-state fixture, gates IT-001, checks live queue output, terminates the worker and injects claim expiry for reproducibility. It then releases the gate, resumes, retries IT-010 with the recovery manifest, inspects history, exports records and generates a report. The test uses the guarded mock subprocess runner. For the interactive check, use the [Step 12 replay](../planning/plan.md#accessibility-replay) with ordinary claim expiry and repeat the workflow in fresh state with the same permissions. Append `--script tests/mock_responses/manifest.json` to the displayed retry command to select the completed mock response.

```bash
env -u API_KEY python3 -m unittest discover -s tests -k test_r109_contract -v
env -u API_KEY python3 -m unittest discover -s tests -v
```

In the mixed queue, expect IT-001 to be unfinished, IT-002 to show `Security Review / review / review-risk-then-owner`, and IT-010 to show `IT Service Desk / error / retry-extraction` with a retry command. Resume should produce `System Owner / review / review-request` for IT-001 and preserve IT-002's committed result. Retry should produce `IT Service Desk / needs-information / clarify` for IT-010 and retain the failure in history. Read the export path and compare the linked extraction and queue records. Read the report path and verify 231 of 231 exact scalar matches in the independent 21-case mock accuracy run. These results describe the mock pipeline.

### Observed results

The first targeted run discovered one test and failed five subchecks. Help omitted the text format and the mixed queue, progress, history and accuracy commands rejected it. The repair added opt-in labelled text with stable field order, one-time progress messages, readable errors and shell-ready retry guidance. The accuracy report received navigable headings. A follow-up check found that a malformed input export produced a generic `ValueError` in text mode. The new regression first failed on the missing filename and guidance. Text mode now identifies the export and asks for UTF-8 JSON with a message string. The output omits the input contents. The full suite passed all 115 tests after the repair, including the text-mode regression and existing JSON and export checks.

| Requirement | Check | Evidence |
|---|---|---|
| R-106 | A-106, comparable workflow access | The standard JSON replay and the keyboard-driven text replay produced the same three routing outcomes, retained the failed attempt in history, published matching queue/extraction snapshots and reported 231 of 231 mock scalar matches. Orca read the operator information listed below. |
| R-107 | A-107, software and platform | The criterion review below records the CLI's non-Web scope. The virtual desktop checks exercised AT-SPI terminal text and speech, terminal preferences, and the whole intake and recovery workflow. |
| R-108 | A-108, keyboard-only operation | Keyboard input exercised help, intake, queue, interruption, resume, retry, history, export and accuracy. Commands use ordinary shell input with explicit submission. The application presents no interactive prompts or custom focus targets. The follow-up replay used normal claim expiry. |
| R-109 | A-109 and `test_r109_contract`, CLI text | The regression verifies labels, reading order, state, owner, action and retry, progress, history, report output and input-error guidance. The mixed queue has no terminal escape or cursor-rewrite sequences. Orca read the labelled output in the terminal workflows below. |
| R-110 | A-110, web application | Inapplicable. The delivered interface is the local CLI. The Firefox checks below concern rendered documents. |
| R-111 | A-111, rendered documents | Orca navigated the three local HTML renderings in Firefox ESR by headings and links where present, and by table cells in the reports. Firefox reached 200% zoom on all three documents. The renderer and scope are recorded below. |
| R-112 | A-112, accessibility handover | Orca found the README's keyboard and screen-reader section and read the text-mode flag and terminal preference guidance. The keyboard replay exercised those commands. Activating its named verification link opened this report. |
| R-113 | A-113, application support service | Inapplicable. The application supplies documentation and CLI help, with no help desk, support channel or referral service. |
| R-114 | A-114, evidence and replay | This record identifies the versions, test inputs, expected results, observed results, repair, applicable provisions and scope limits. |

### Orca queue navigation

On November 19, 2026, a keyboard-only replay used Ubuntu 24.04, Xfce Terminal 1.1.3, Orca 46.1 and `less` 590 in a virtual desktop. Speech Dispatcher used eSpeak NG and a virtual PulseAudio sink. The replay captured Orca's spoken text from the Speech Dispatcher log and confirmed that the sink received audio.

At the mixed queue stage, set `replay_db` to the path of the replay database. Stream the plain-text queue into the pager.

```bash
python3 -m intake queue --db "$replay_db" --include-pending --format text | less -j1
```

Press `g` for the first request. For the later requests, press `g` and search with `/Document: IT-002` or `/Document: IT-010`. In Orca's desktop layout, use `Insert` + numpad `7` to read the first visible line and numpad `9` to read each next line. Use numpad minus to leave flat review before returning to pager navigation. The spoken lines followed this order within each request.

| Request | What Orca read during navigation |
|---|---|
| IT-001.json | Document identifier, then work state Running. |
| IT-002.json | Document identifier, work state Completed, owner Security Review, status `review`, and next action `review-risk-then-owner`. |
| IT-010.json | Document identifier, work state Failed, owner IT Service Desk, status `error`, next action `retry-extraction`, the provider error, and the retry command. Orca read both terminal lines of the wrapped command in sequence. |

### Orca workflow replay

In a fresh Ubuntu 24.04 virtual desktop with Xfce Terminal 1.1.3, Orca 46.1 and `less` 590, scripted keyboard input exercised the intake, interruption and recovery commands with the TP-52 mock manifest. A nine-second provider timeout and the usual five-second claim grace period made expiry reproducible. The replay stopped the mock worker, waited for expiry and resumed the same database. Speech Dispatcher sent Orca's output to eSpeak NG and a virtual PulseAudio sink. The sink recording contained audio and the log captured the utterances below. For long outputs, the replay saved command output and used the pager controls above.

The follow-up regression verifies that text mode announces reading before export validation and that resuming after the mixed-state snapshot preserves IT-002's committed result and attempt ID.

| Action | What Orca read and what the replay verified |
|---|---|
| Open help and start intake | The queue usage with the JSON/text choices and the progress message for three input files. |
| Resume and retry | The resume progress, three current requests, one failed request and zero unfinished requests. After invoking the failed record's retry command with the recovery manifest, Orca read IT-010's retry progress and zero failed requests. The latest routing was IT Service Desk / needs-information / clarify. |
| Inspect history | The IT-010 document, failed attempt, source revision, settings fingerprint and mock provider. The JSON comparison confirmed that the failed attempt and its provenance remained available alongside the later result. |
| Export and check accuracy | The published generation path, the accuracy progress, 231 of 231 exact scalar matches and the report path. A separate check joined the exported queue and extraction records by filename and verified matching source hashes and routing. |
| Correct malformed input | When the input export lacked a message string, Orca announced the filename `invalid-export.json` and the instruction to provide UTF-8 JSON with a message string and rerun intake. |

### Terminal preferences and rendered documents

The virtual terminal honored the configured Liberation Mono font, yellow-on-black text, and a magenta underline cursor. Four zoom increments doubled the measured AT-SPI character height from 23 to 46 pixels. At that size, Orca located IT-010 in the queue and read its state, owner, action, diagnostic and both lines of the wrapped retry command. Resetting zoom restored the 23-pixel character height. Leaving the pager returned focus to the shell with the configured cursor.

The document check used Mozilla Firefox 153.7.0esr, Orca 46.1 and Python-Markdown 3.5.2 with tables, fenced code and heading anchors. Each local test page had an English language declaration, a descriptive title, a main landmark and a visible link focus style. The checks inspected headings and content at 100% and 200% browser zoom. The rendered Markdown documents contain text and tables, with no app-supplied images, audio or video.

From the repository root, generate the local test pages and open the printed README path in Firefox. The renderer uses the committed mock accuracy report by default. To render the report from a fresh replay, append `--accuracy "$work/accuracy.md"`.

```bash
rendered=$(mktemp -d)
uv run --no-project --with 'Markdown==3.5.2' python tools/render_accessibility_docs.py "$rendered"
```

The output contains `README.html`, `deliverables/accuracy.html`, and `deliverables/verification.html`. The renderer preserves link fragments and maps links among the local pages to their HTML counterparts. It also renders the linked plan and spec so the replay and requirement links work.

In Firefox's document view, the replay used `h` for headings, `k` for links, `t` for tables and `Alt` + `Shift` + arrow keys for table cells. It used Firefox's zoom shortcuts and checked the reported 200% level before resetting it. [Orca structural navigation commands](https://help.gnome.org/orca/commands_structural_navigation.html)

| Document | Observed navigation |
|---|---|
| README | Orca announced heading levels and read the text-mode and terminal-preference instructions. Its next-link command found the named verification link. Enter activated it and Orca announced the verification page. |
| Accuracy report | Orca reached the exact-match and source-evidence headings. Cell navigation identified the Document and Exact scalar matches column headers and associated IT-001 with its 11/11 result. |
| Verification report | Orca reached the Section 508 heading and identified links by name. Cell navigation announced the Step, Targeted tests, Full suite after implementation and Evidence headers with sample data. |

The tested presentation is the local renderer and browser configuration above. Repository-hosted presentation and other operating systems, renderers or assistive-technology combinations have separate platform behavior. The results apply to the tested CLI and document workflows.

### Applicable software and document criteria

For the CLI, E207.2 excepts criteria 2.4.1, 2.4.5, 3.2.3, 3.2.4 and WCAG Conformance Requirement 3. E207.2.1 supplies the terminology substitutions and E207.3 governs the complete multistep CLI workflow. E205.4 and E205.4.1 govern covered non-Web documents. The rendered-document checks also address electronic support documentation under 602.3. [Revised 508 Standards](https://www.access-board.gov/ict/)

| Provision | Scope and evidence |
|---|---|
| E203.1–E203.2, E207.3 | The same internal operator commands support the complete intake and recovery task. A-106 and A-108 trace the access needs above to the keyboard and assistive-technology test. Public access is inapplicable for this delivery. |
| WCAG 1.1.1, 1.2.1–1.2.5, 1.4.2, 1.4.5 | The CLI and reviewed documents expose text and text structure. There is no app-supplied non-text content, audio, video or image of text. |
| WCAG 1.3.1–1.3.3, 1.4.1 | CLI labels and state names survive without color or layout, as exercised by TP-54. Rendered heading levels, link names, table headers and content order were inspected in HTML. The instructions use command and field names, with no sensory-only references. |
| WCAG 1.4.3–1.4.4, 2.4.7, 503.2 | The CLI retains terminal-controlled color, contrast, font and cursor. The test rendering uses near-black text and dark blue links on white, with a visible focus outline. Firefox reported a 200% zoom level for each rendered document. |
| WCAG 2.1.1–2.1.2, 2.2.1–2.2.2, 2.3.1 | A-108 exercised ordinary shell input and return to the prompt. There is no application time limit for keystrokes, rotating display, animation or flashing. Provider timeouts apply to background processing, with persisted status and recovery. |
| WCAG 2.4.1–2.4.6, 3.2.3–3.2.4 | CLI applicability follows the non-Web exceptions above. Commands and reports have descriptive names, and CLI help and result fields use consistent labels. The local documents have titles, a main landmark and navigable headings, with no repeated authored navigation. The README points to the verification report by name. The source files are individually discoverable in the repository. |
| WCAG 3.1.1–3.1.2, 3.2.1–3.2.2 | Application labels and repository-authored reading content are English. The HTML test rendering declares English. Commands take effect on explicit invocation, with no custom fields that change context on focus or input. |
| WCAG 3.3.1–3.3.4 | Help describes inputs, and invalid CLI arguments are identified in text. The labelled input-error message identifies the affected export and gives a correction. An extraction failure associates its diagnostic and retry command with the affected document. Recovery retains prior attempts and source revisions. The UI has no legal or financial submission, approval or deletion control. |
| WCAG 4.1.1–4.1.2, 502 | Xfce Terminal exposes the CLI text through AT-SPI, and Orca read it without application controls or accessibility services. Firefox exposed the rendered headings, links, table headers and data cells through native HTML semantics. Sections 502.2.1, 502.3 and 502.4 govern platform providers and platform applications. This CLI uses the platform's services. |
| E204.1 / Chapter 3, 503.3–503.4, 504 | A-106–A-109 cover terminal speech, labelled states independent of color, magnification and keyboard-only input. Ordinary shell input and documented recovery afford sequential operations without application-imposed key timing. Labelled states, consistent help and recovery commands support task comprehension. The application has no speech input requirement, media controls, alternate assistive-technology UI or interactive authoring tools. |
| E208 / 602, 603 | A-111 and A-112 address electronic support documents and compatibility instructions. Support-service provisions are inapplicable for this release. |

## External final checks

R-104: after a submitted commit exists, inspect `git log -1 --format=fuller` and the facilitator's paired session-capture record using its supplied instructions. Entire automatically captures coding sessions at commits; no manual checkpoint/setup command is needed here. Pairing to a final submitted commit remains an external acceptance check.

R-105: inspect `git status --short`, `git log -1 --oneline`, `git branch -vv`, and `git ls-remote --symref origin HEAD` at final submission; then verify the required content/session capture is on the default remote branch and Classroom contains only the root repository URL. No push or Classroom submission was performed by this implementation task.
