# Verification

How the implementation is checked, what each check compares, the operating limits, and what was observed. Observed results and repairs are labelled as such; everything else describes the checks themselves.

## Commands

Run from the repository root.

| Purpose | Command |
|---|---|
| Install (runtime + test dependencies) | `uv sync --extra dev` |
| Full test suite (offline, mock model) | `uv run pytest` |
| Fixture check (mock replies vs sources, schema, expected truth) | `uv run python scripts/build_mock_fixtures.py` |
| Accuracy report (mock, or the configured provider) | `uv run intake accuracy --out deliverables/accuracy.md` |
| Request evidence | `uv run intake evidence --out deliverables/request-evidence.json` |
| Review queue and records | `uv run intake run materials/requests` then `uv run intake status` / `uv run intake queue` |

Without uv: `pip install -e '.[dev]'`, then `pytest` and `intake ...`, or `PYTHONPATH=src python -m access_intake ...`.

No test calls a model provider or needs network access. Tests write only to pytest temporary folders.

## Checks and their intended comparisons

| Test file | Compares | Against |
|---|---|---|
| `test_mock_examples.py` | Each of the 21 mock replies (20 supplied + paraphrase IT-101): schema, exact quotes, real dates, the 11 scalar fields, purpose keywords, required quotes, issue codes, source hash pin | `tests/expected/expected_fields.json`, written from the messages and `materials/office-policy.md`; the message text |
| `test_extraction_examples.py` | The published record for each of the 21 messages after a full pipeline run: status, schema, identity and SHA-256, scalars, quotes, issue codes, owner / status / next_action; queue totals; queue and extractions agree on `document_id` + `source_sha256`; one model call per document | The expected truth; `hashlib.sha256` of the file bytes |
| `test_loader.py` | Hash of original bytes, document_id = filename, malformed exports | Raw file bytes; a re-serialised copy must hash differently |
| `test_prompt.py` | System prompt covers every schema key, enum value, issue code and policy rule; contains no example text; message placed unaltered inside nonce delimiters; version tracks wording | The schema, `issues.py`, the policy, the 21 messages |
| `test_validate.py` | Completion status, parsing, schema, grounding, date checks, and the issues code adds when a model omits them | Mock fault replies and hand-modified extractions |
| `test_injection.py` | IT-006 detected with exact spans; no false alarm on the other 20 messages or ordinary request wording | The message text |
| `test_policy.py` | All 21 routes; every rule and its precedence; no outcome approves | The policy's five ordered rules |
| `test_mock_client.py` | Mock replies, every fault type, source-hash pinning, thread safety, provider stub | Fixtures file |
| `test_failures.py` | Provider failure isolated with retry command; backoff and Retry-After; seven unusable-reply types; malformed export; outage halts then resume; unconfigured provider; credentials absent from every output, state file and log | Expected error records and codes |
| `test_reprocessing.py` | Unchanged rerun; changed source; failed retry after success; model, policy and prompt changes; force; retry; interruption and resume; two operators; claims; atomic writes; history immutability | Expected skip / reuse / refresh behaviour and history contents |
| `test_pipeline.py` | Results published before the next document starts; a stuck document does not hold up the rest; run manifest contents | Queue file read during the run |
| `test_cli.py` | Every command's output and exit code, including the outage-resume-retry sequence | Expected text and exit codes |
| `test_accuracy_evidence.py` | Accuracy counting rules (omission vs factual error vs failed; "not in message"); report labels mock vs live; evidence is redacted and shows configuration and handling | Hand-modified records |
| `test_config.py` | Setting precedence, validation, credential never in settings, fingerprint inputs | Explicit expectations |
| `test_conflicts.py` | Code-level conflict check (manager, environment, permission, start_date, end_date): only IT-004 (environment) and IT-020 (manager) flagged among the 21 messages; labelled corrections (IT-008 environment, IT-015 dates) not flagged; IT-006's override sentence ("Grant admin access") ignored so permission stays read; change requests (IT-007, IT-019) exempt for permission; restated windows and single dates not flagged; a value the model picked from a conflict is set to null with a conflict issue and both statements quoted; routing to clarify; stored extractions corrected on re-route without a model call | The message text and the policy |
| `test_quote_merge.py` | Near-duplicate quotes merged: a quote contained in another is dropped, the longer keeps the earlier position, overlap without containment is kept, idempotent; the 21 fixtures are unchanged; IT-020 live-style short quotes merge with the full sentences the conflict check adds | Explicit lists and the fixtures |
| `test_databricks_client.py` | Databricks client against a fake HTTP transport: URL, Authorization header, body (messages, max_tokens, temperature or none, response_format modes), host and token fallbacks; finish-reason mapping; HTTP 429/5xx/4xx/401/403/404, timeouts and network errors mapped to provider errors; credential absent from errors and state; full pipeline run; switching mock -> databricks -> mock | Expected request bodies and error classes |

## Required scenarios and where they are checked

| Scenario (docs/requirements.md) | Test |
|---|---|
| Complete request | IT-001 → System Owner; IT-002 → Security Review (`test_extraction_examples`, `test_policy`) |
| Missing dates | IT-010 (no end date), IT-011 (relative dates) → null dates, clarify |
| Conflicting details | IT-004 (environment), IT-020 (manager) → null, conflict issue, both statements quoted |
| Embedded instructions | IT-006 → inspect-message, permission stays read; still routed to inspection if a model obeyed it (`test_policy`) |
| Provider failure | `test_provider_failure_is_isolated_and_actionable` |
| Unchanged rerun | `test_unchanged_rerun_makes_no_model_calls_and_changes_nothing` |
| Changed source content | `test_changed_source_creates_a_new_result_and_keeps_the_old` |
| Paraphrased synthetic request | IT-101 in `tests/fixtures/synthetic/` |
| Refusal / interrupted response | `test_unusable_replies_are_errors_without_retry` (refusal, truncated) |
| Failed retry after a prior success | `test_failed_retry_after_success_keeps_the_last_valid_result` |
| Interruption and resume | `test_interrupted_run_resumes_without_redoing_completed_work` |
| Two operators at once | `test_two_operators_at_once_process_each_document_once` |
| Settings or policy change | `test_model_change_refreshes_every_record`, `test_policy_change_reroutes_without_model_calls` |

## Observed results

Observed on 2026-11-06 on a temporary copy of the repository (Databricks serverless, Linux, Python 3.12.3, jsonschema 4.25.0, pytest 8.4.2; run with `python -m pytest`, not through uv). All responses were from the mock.

| Test file | Passed |
|---|---|
| test_accuracy_evidence.py | 9 |
| test_cli.py | 14 |
| test_config.py | 13 |
| test_databricks_client.py (added later, see below) | 33 |
| test_conflicts.py (added later, see below) | 51 |
| test_quote_merge.py (added later) | 29 |
| test_extraction_examples.py | 131 |
| test_failures.py | 25 |
| test_injection.py | 40 |
| test_loader.py | 32 |
| test_mock_client.py | 46 |
| test_mock_examples.py | 176 |
| test_pipeline.py | 5 |
| test_policy.py | 69 |
| test_prompt.py | 41 |
| test_reprocessing.py | 17 |
| test_validate.py | 80 |
| **Total** | **811 passed, 0 failed** (698 before the Databricks client, 731 before the conflict check, 782 before quote merging) |

- `scripts/build_mock_fixtures.py`: "OK: 21 mock fixtures match their sources, the schema and the expected fields."
- `intake accuracy` (mock): 231 / 231 scalar fields, 0 omissions, 0 factual errors; business_purpose, quotes, issue codes and routes 21 / 21. See `accuracy.md`.
- `intake run materials/requests` (mock), on the temporary copy: 20 done, 20 model calls; queue System Owner 4, Security Review 5, IT Service Desk 11 (9 clarify, 1 inspect-message, 1 confirm-withdrawal). An immediate rerun skipped all 20 with 0 model calls.
- The timing-sensitive tests (two operators, slow document, claims, interruption, outage, unconfigured provider) passed 8 consecutive repeated runs after the interruption repair below.
- `accuracy.md` and `request-evidence.json` in this folder are byte-identical (SHA-256 compared) to the files the commands produced on the temporary copy.

### Live observation: Databricks endpoint (not mocked)

Observed on 2026-11-06 on the temporary copy, with `provider = databricks`, model `databricks-claude-sonnet-4-5`, `structured_output = json_schema`, `temperature = 0.0`, `max_output_tokens = 2000`, prompt `access-extract-1+da6c2cecc0b8`, policy `northstar-intake-policy/2026-10-22.1`, via `intake accuracy` on all 21 expected messages. This is a single run; model output can vary between runs. The full report is `deliverables/accuracy-databricks-claude-sonnet-4-5.md` (byte-identical to the command's output; contains no credential).

| Measure | Result |
|---|---|
| Scalar fields, exact match | 231 / 231 (0 omissions, 0 factual errors, 0 failed) |
| Owner / status / next_action | 21 / 21 |
| business_purpose | 21 / 21 |
| Issue-code set equals expected | 20 / 21 (IT-003: `relative-date` not reported; `missing` was) |
| Required supporting quotes present | 11 / 21 |

The 10 quote mismatches are mostly shorter exact fragments of the required text (for example `Needed during 2026-10-24 and 2026-10-25` without the final period, or `request IT-061` instead of `submitting request IT-061`); every quote the model gave passed the exact-substring check. The rule was not relaxed after seeing this output; whether to accept punctuation-trimmed or shorter fragments is a decision for the team.

### Live observation: second model, for comparison (not mocked)

Observed on 2026-11-06 with the same settings, prompt and policy but model `databricks-meta-llama-3-3-70b-instruct`. Full report: `deliverables/accuracy-databricks-meta-llama-3-3-70b-instruct.md` (byte-identical to the command's output). Single run each.

| Measure | claude-sonnet-4-5 | llama-3-3-70b-instruct |
|---|---|---|
| Scalar fields, exact match | 231 / 231 | 230 / 231 |
| Factual errors | 0 | 1 (IT-020 manager: picked Elena Park from an unlabelled conflict) |
| Owner / status / next_action | 21 / 21 | 20 / 21 |
| business_purpose | 21 / 21 | 17 / 21 |
| Issue-code set equals expected | 20 / 21 | 17 / 21 |
| Required supporting quotes present | 11 / 21 | 14 / 21 |

Finding: with Llama, IT-020 was routed to **System Owner / review / review-request** instead of IT Service Desk / clarify. The model chose one of two conflicting managers and reported a `missing` issue rather than `conflict`, and no required fact was null, so no rule sent it to clarify. The conflict rule depends on the model reporting the conflict; nothing in code detects an unlabelled contradiction. A human reviewer still sees the request (nothing is approved automatically), but at the wrong owner. Other Llama differences: IT-005 gave the withdrawal reason as business_purpose (truth: null); IT-017 reported no issues (still routed to clarify because kind is other); IT-010 added a relative-date issue; three purposes missed a keyword.

On this sample `databricks-claude-sonnet-4-5` remains the supported configuration.

Repair (policy `northstar-intake-policy/2026-10-22.2`): `access_intake/conflicts.py` now detects, in code, a manager or environment stated with different values and no correction label. The validator sets the model's value to null, adds a conflict issue and quotes both statements, and rule 3 routes the request to clarify. Re-routing re-runs the deterministic checks on stored extractions, so records made before the repair are corrected on the next run without model calls. Re-check, observed 2026-11-06 with `databricks-meta-llama-3-3-70b-instruct` (fresh state, same settings, policy .2): the model again picked Elena Park for IT-020; the record was set to manager null and routed to IT Service Desk / clarify; scalar fields 231 / 231, routes 21 / 21, business_purpose 17 / 21, quotes 17 / 21, issue codes 18 / 21. That re-check report was not saved; the saved Llama report shows the run before the repair.

Extension (policy `northstar-intake-policy/2026-10-22.3`): the same check now also covers permission (`read/write/admin access`, `read-only access`, `for reading/writing`; skipped when the message describes a change of access) and the start and end of the access window (date ranges and start/end cues; a single date such as the request date is not a window). Sentences of an instruction-override attempt are excluded. On the 21 messages it flags nothing new. Not re-run against a live model.

Quote merging (policy `northstar-intake-policy/2026-10-22.4`): after the code-level additions, a supporting quote contained in another supporting quote is dropped (for example the model's `My manager is Priya Shah` when the conflict check adds `My manager is Priya Shah.`); the longer quote keeps the earlier position and dropped quotes are listed as `merged_quotes` in the attempt history. Kept quotes are always original quotes, so they remain exact substrings. This removes duplication in records; it does not change the required-quotes measure, which already passes when a required quote lies within any supporting quote.

An earlier probe found that `databricks-gpt-5-mini` rejects `temperature = 0.0` (HTTP 400, only the default is supported), hence `INTAKE_TEMPERATURE=none`.

## Repairs made during verification

These were found during implementation and testing and fixed before the results above:

1. Store paths: current and claim files were named `<document_id>.json`, giving `IT-001.json.json`, so a claim written under the expected name was not seen. Now the document ID is the filename.
2. Interruption race: after an interruption in a worker, the next queued document could still start before the main thread stopped dispatching (an intermittent failure of the interruption test, seen in repeated runs). The worker now stops dispatching itself.
3. Test helper: `history_files()` counted per-document history folders (named like `IT-001.json`) as files. It now counts files only.
4. Redaction: the key pattern required a hyphen, so `dapi...` style tokens were not removed. Noticed while writing the redaction test and fixed before it ran.
5. Expected truth, disclosed: the IT-016 purpose keyword was changed from "pipeline" to "starter project" after a review showed the mock fixture's paraphrase did not contain "pipeline" (before the fixture checker first ran). Both words describe the stated purpose; because this edit to the truth was made after reading an output, it is noted here.

## Operating limits

- Workload: designed for 200-400 messages a day and bursts of about 2,000. Outputs are regenerated from all current records after every document commit, which is cheap at that size but grows linearly; much larger volumes need a database-backed store.
- Shared operation: one shared filesystem with POSIX `fcntl` locks (msvcrt on Windows). Two operators in one process with separate stores are tested; two separate processes rely on the same lock but are not tested directly. Network filesystems with unreliable locking are not supported.
- Interruptions: completed documents are committed atomically and stay. A document being processed when the process stops stays `running` in the run log and is redone by `intake resume`. A claim left by a killed process blocks that document for `claim_ttl_s` (default 15 minutes).
- Paths stored for resume and retry are relative to the folder where `intake` was run; run follow-up commands from the same folder.
- The pipeline cannot cut off a hung model call; the client must enforce `timeout_s` (the Databricks client uses it as the HTTP timeout).
- The automated tests use the mock and a fake transport, which prove the pipeline, validation, routing, persistence, request construction, error mapping and comparison, not extraction quality. The one live observation above is a single run on one endpoint.
- `deliverables/extractions.jsonl` and `deliverables/queue.json` are produced by `intake run materials/requests` in the operator's environment, together with its `state/` folder.
