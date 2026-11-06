# IT Access Request Intake

A command-line tool (`intake`) that turns exported access-request messages into records conforming to `snapshot.schema.json`, places each request in the review queue under `materials/office-policy.md`, and keeps a history of every attempt. The task is in `CHALLENGE.md`; the design is in `docs/architecture-plan.md`.

Nothing in this tool approves access. Every outcome waits for the named human reviewer.

## Supported runtime and provider

- Python 3.11 or later (verified on 3.12.3). Dependencies: `jsonschema`; `pytest` for tests. Locked in `uv.lock`.
- Linux or macOS (file locking uses `fcntl`; Windows falls back to `msvcrt`, untested).
- Model providers, chosen with `INTAKE_PROVIDER` (or `--provider`, or `provider` in the settings file):
  - `mock` (default): answers from `tests/fixtures/mock_responses.json` and can simulate provider faults. All tests use it.
  - `databricks`: a Databricks Model Serving / Foundation Model API endpoint, called over HTTPS with the standard library (`src/access_intake/llm/databricks.py`). Set `INTAKE_MODEL_ID` to the endpoint name, the workspace URL in `INTAKE_DATABRICKS_HOST` or `DATABRICKS_HOST`, and the token in `API_KEY` (`DATABRICKS_TOKEN` is used if `API_KEY` is unset). Supported configuration: `databricks-claude-sonnet-4-5` with `structured_output = json_schema` and `temperature = 0`. In one live run on the 21 sample messages it matched all 231 extracted fields and all 21 queue placements, but only 11 / 21 messages included every required quote (it tends to quote shorter fragments); see `deliverables/accuracy-databricks-claude-sonnet-4-5.md`.
  - `provider`: a placeholder for another provider (`src/access_intake/llm/provider.py`); it fails every call cleanly with `provider-not-configured`.

  Every record's `response_source` says which produced it (`mock` or `databricks/<endpoint>`).

> **The mock is for tests and demonstrations only.** It answers only the 20 supplied exports and the paraphrase IT-101, and only while their file bytes are unchanged. Running real intake with the default `provider = mock` turns every new or edited export into an error record with code `mock-fixture-missing`. For real intake, use `--provider databricks` (or set it in the settings file).

### Switching between the mock and Databricks

```
# mock (default)
uv run intake run materials/requests

# Databricks-hosted model
export API_KEY=<DATABRICKS_TOKEN>                      # replace with your token, brackets included
export DATABRICKS_HOST=https://<workspace-host>         # or INTAKE_DATABRICKS_HOST
uv run intake --provider databricks --model-id databricks-claude-sonnet-4-5 run materials/requests
```

Switching provider or model is a settings change: the next run refreshes every record, and switching back reuses the stored results for the earlier settings without model calls. Some endpoints accept only their default temperature (for example `databricks-gpt-5-mini`): set `INTAKE_TEMPERATURE=none`. If an endpoint rejects `response_format` json_schema, set `INTAKE_STRUCTURED_OUTPUT=json_object` or `none`; the validator still enforces the schema.

## Setup

```
uv sync --extra dev
```

Without uv: `python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'`.

Run every command from the repository root (default paths are relative to it).

## Commands

| Task | Command |
|---|---|
| Start intake | `uv run intake run materials/requests` |
| See progress, queue counts, errors | `uv run intake status` |
| Inspect the queue (works while a run is in progress) | `uv run intake queue` |
| Filter the queue | `uv run intake queue --owner "Security Review"`, `--status needs-information`, `--next-action clarify`, `--json` |
| One request in detail | `uv run intake show IT-004.json --history` |
| Retry one failed document | `uv run intake retry IT-002.json` (each error record names this command) |
| Retry every failed document | `uv run intake retry --errors` |
| Continue an interrupted or halted run | `uv run intake resume` (or `uv run intake resume <run_id>`) |
| Tests | `uv run pytest` |
| Mock fixture check | `uv run python scripts/build_mock_fixtures.py` |
| Accuracy check | `uv run intake accuracy --out deliverables/accuracy.md` |
| Request evidence | `uv run intake evidence --out deliverables/request-evidence.json` |

`python -m access_intake ...` works the same as `intake ...` (with `PYTHONPATH=src` if not installed).

Exit status: `0` finished, no error records; `1` finished with error records; `2` usage or configuration problem; `3` halted or incomplete (resume later); `130` interrupted.

A batch is whatever files or folders you pass to `intake run`; a single file is a batch too. Documents not in a batch keep their current records: absence from a later export never withdraws a request (withdrawal is stated in a message and routed under the policy).

## Daily operation

1. **Start the intake** for the morning's exports (Databricks provider configured as above):
   `uv run intake --provider databricks --model-id databricks-claude-sonnet-4-5 run <exports-folder>`
   Each document prints a line as it finishes, and results appear in the queue immediately.
2. **Reviewers work their part of the queue** while the rest is pending:
   `uv run intake queue --owner "System Owner"`, `--owner "Security Review"`, `--owner "IT Service Desk"`; then `uv run intake show <document_id>` for the facts, quotes and issues behind one request.
3. **Follow up on failures**: `uv run intake queue --status error` or `uv run intake status`, look up the error code below, fix the cause, then `uv run intake retry <document_id>` or `uv run intake retry --errors`.
4. **After an outage, a deployment stop or Ctrl-C**: `uv run intake status` shows the run's open documents and the exact `intake resume <run_id>` command; resuming never redoes completed documents.
5. **Later exports during the day**: run `intake run` again on the new batch. Unchanged documents are skipped; a corrected export replaces its current record and the earlier version stays in history.
6. **After a model, prompt or policy change**: run `intake run` on the exports again; `intake status` lists records produced under older settings until they are refreshed.

## Reading the queue

| owner / status / next_action | Meaning |
|---|---|
| System Owner / review / `review-request` | Complete staging, read-only request; the system owner decides (policy rule 5) |
| Security Review / review / `review-risk-then-owner` | Complete request involving production, write or admin; Security Review assesses risk, then forwards to the system owner (rule 4) |
| IT Service Desk / needs-information / `clarify` | Missing, conflicting or out-of-order facts, an identifier mismatch, or not an access request; ask the requester (rule 3) |
| IT Service Desk / review / `confirm-withdrawal` | A withdrawal with its request ID and date (rule 2) |
| IT Service Desk / review / `inspect-message` | The message tries to override instructions; inspect it before anything else (rule 1) |
| IT Service Desk / error / `retry-extraction` | No valid extraction; see the error code and its retry command |

Each record's `reasons` lists why it was placed there, and `issues` uses these codes: `missing`, `conflict`, `relative-date`, `date-order`, `id-mismatch`, `injection`, `not-access-request`. `supporting_quotes` are exact text from the message. All timestamps are UTC.

## Error codes and what to do

| Code | Meaning | First action |
|---|---|---|
| `provider-not-configured` | No token or host, a 401/403 (token or permission), or a 404 (endpoint name) | Check `API_KEY`, `DATABRICKS_HOST` and `INTAKE_MODEL_ID`; the endpoint must exist and the token must be allowed to query it |
| `provider-unavailable` | 5xx or network failure after all retries | Check the endpoint's state; `intake resume` / `intake retry --errors` when it recovers |
| `provider-timeout` | No reply within `INTAKE_TIMEOUT_S` after all retries | Retry later, or raise `INTAKE_TIMEOUT_S` |
| `provider-rate-limited` | HTTP 429 after all retries | Lower `INTAKE_CONCURRENCY` (shared model capacity), then retry |
| `provider-bad-request` | Other HTTP 4xx, often an unsupported parameter | Read the message: e.g. `INTAKE_TEMPERATURE=none` or `INTAKE_STRUCTURED_OUTPUT=json_object` |
| `truncated` | The reply was cut off (output token limit) | Raise `INTAKE_MAX_OUTPUT_TOKENS`, then retry |
| `refused`, `content-filtered` | The model or provider declined | Inspect the message (`intake show`); retry, or handle manually |
| `malformed-output`, `empty-output`, `schema-violation` | The reply was not one schema-conforming JSON object | Retry; if it repeats and `INTAKE_STRUCTURED_OUTPUT` was changed from `json_schema`, set it back; otherwise try another endpoint |
| `ungrounded-quote`, `invalid-date` | A quote not found in the message, or an impossible date | Retry; repeated failures point to the model or prompt |
| `malformed-export` | The export file is not UTF-8 JSON with a `message` string | Fix or re-export the file, then retry |
| `mock-fixture-missing` | The mock has no reply for this document or file version | Use `--provider databricks` for real intake |
| `client-error` | An unexpected fault in the model client | Check the message; report it to the developer |

After `INTAKE_BREAKER_THRESHOLD` consecutive documents fail with an unreachable-service code, the run halts with status `halted` instead of producing hundreds of error records.

## Running in Databricks

- From a notebook, run the same commands with `!` from the repository folder, e.g. `!cd /Workspace/<path-to-repo> && uv run intake status`. `uv` was available on the serverless compute used for development; otherwise `%pip install -e .` and `!python -m access_intake ...`.
- Notebook and job environments often already set `DATABRICKS_HOST` and `DATABRICKS_TOKEN`; the client uses them when `INTAKE_DATABRICKS_HOST` and `API_KEY` are not set.
- For scheduled intake, run `intake run` as a job task; exit status `1` (error records) or `3` (halted) can be used to alert.

### Outputs

- `deliverables/extractions.jsonl`: one current record per document: `document_id`, `source_sha256`, `extraction` (or null on error), `status`, `owner`, `next_action`, `error` (or null), `response_source`, plus routing reasons, flags, provider, model, prompt and policy versions, settings fingerprints, `attempt_id` and `last_valid_attempt`.
- `deliverables/queue.json`: the review queue, one entry per record, linked by `document_id`, with counts by owner and next action.
- `state/`: history of every attempt, current records, run logs and claims. Not committed (`.gitignore`); keep it with the deliverables it produced. Never edit it by hand.
- `state/accuracy/`: a separate scratch state used by `intake accuracy`, so accuracy checks never change the review queue.
- The committed `deliverables/extractions.jsonl` and `deliverables/queue.json` come from `uv run intake run materials/requests` with the mock (`response_source` is `mock`; policy `northstar-intake-policy/2026-10-22.4`). Regenerate and recommit them after any change that affects records (settings, prompt or policy version).

## Configuration

Precedence: built-in defaults, then a TOML file (`--config config/settings.toml`; start from `config/settings.example.toml`), then environment variables, then command-line options (`--state-dir`, `--output-dir`, `--provider`, `--model-id`, `--concurrency`).

| Variable | Meaning | Default |
|---|---|---|
| `API_KEY` | Provider credential, e.g. a Databricks token (read at call time; never stored or logged) | none |
| `DATABRICKS_TOKEN` | Fallback credential for `databricks` when `API_KEY` is unset | none |
| `INTAKE_PROVIDER` | `mock`, `databricks` or `provider` | `mock` |
| `INTAKE_MODEL_ID` | Model identifier recorded with each result; for `databricks`, the serving endpoint name | `mock-extractor-v1` |
| `INTAKE_DATABRICKS_HOST` (else `DATABRICKS_HOST`) | Databricks workspace URL | none |
| `INTAKE_STRUCTURED_OUTPUT` | `json_schema` (strict schema), `json_object` or `none` | `json_schema` |
| `INTAKE_TEMPERATURE`, `INTAKE_MAX_OUTPUT_TOKENS` | Model parameters (`INTAKE_TEMPERATURE=none` sends none) | `0.0`, `2000` |
| `INTAKE_TIMEOUT_S` | Per-call timeout (enforced by the provider client) | `60` |
| `INTAKE_MAX_RETRIES` | Retries for timeout, rate limit, unavailable | `3` |
| `INTAKE_BACKOFF_BASE_S`, `INTAKE_BACKOFF_MAX_S` | Retry wait, doubling with jitter, capped; a provider Retry-After wins | `1`, `30` |
| `INTAKE_BREAKER_THRESHOLD` | Consecutive unreachable documents before a run halts | `5` |
| `INTAKE_CONCURRENCY` | Parallel model calls | `4` |
| `INTAKE_CLAIM_TTL_S` | Age after which another run's claim on a document is abandoned | `900` |
| `INTAKE_STATE_DIR`, `INTAKE_OUTPUT_DIR` | State and output folders | `state`, `deliverables` |
| `INTAKE_SCHEMA_PATH`, `INTAKE_MOCK_RESPONSES` | Schema and mock fixture paths | `snapshot.schema.json`, `tests/fixtures/mock_responses.json` |
| `MOCK_FAULTS` | Simulated faults, e.g. `IT-002:unavailable,IT-003:timeout*1` | none |

Changing the provider, model, model parameters, prompt or schema changes the settings fingerprint, so the next `intake run` refreshes the affected records. A policy-only change re-routes stored extractions without model calls. `intake status` lists records produced under other settings.

## How it handles the operating context

- Each document is processed independently, several at a time, and committed as soon as it finishes, so reviewers can work from `intake queue` while the rest is pending, and a slow reply holds up only its own document.
- Unchanged documents (same SHA-256 of the file bytes and same settings) are skipped. A changed export gets a new current record; earlier versions stay in `state/history/` with their hashes.
- Failures (provider errors, refusals, truncated or malformed replies, schema violations, ungrounded quotes, malformed exports) become error records routed to IT Service Desk / `retry-extraction` with the exact retry command. If an earlier attempt succeeded, the record points to it and says whether it was for the same source version.
- After `INTAKE_BREAKER_THRESHOLD` consecutive documents cannot reach the model service, the run halts and the rest stay pending; `intake resume` continues it. An interrupted run is resumed the same way without redoing completed documents.
- Two operators can run at once on the same state folder: each document is claimed by one run, and the other skips it.
- Messages are treated as data. The prompt says so, a detector flags instruction-override attempts in code, and routing is computed in code from structured values only.
- Contradictions are checked in code as well as by the model: a manager, environment, permission, access start date or end date stated with different values and no correction label is set to null, gets a conflict issue with both statements quoted, and goes to IT Service Desk / clarify, even if the model picked one value. Change requests are exempt for permission (they name the old and new permission), and the sentences of an instruction-override attempt are ignored.

## Handover notes: assumptions and limits

- Sized for 200-400 messages a day and bursts of about 2,000 on one shared filesystem. Outputs are rebuilt after every commit; much larger volumes need a database-backed store.
- Shared operation: every operator must use the same `INTAKE_STATE_DIR` and `INTAKE_OUTPUT_DIR`. Two operators in one process are tested; separate processes rely on the same file lock but are not tested directly. Network filesystems without reliable locking are not supported.
- Where state can live: all testing used a local disk. Placing `state/` on `/Workspace` or a Unity Catalog Volume (FUSE-mounted in Databricks) is untested; confirm that `fcntl` locking and atomic rename work there before two operators share it, or keep state on one driver's local disk with a single operator.
- Logs: `-v` writes step-by-step logs (credentials redacted) to stderr only; there is no log file. Redirect it (`2> intake.log`) or rely on `state/runs/<run_id>.json` and `.events.jsonl`, which record every run and document state change.
- Data sensitivity: `state/` and `deliverables/` hold message text, names and managers in plain JSON. Restrict access to those folders as you would the original exports. The API key is never written to them.
- Growth and backup: `state/history/` keeps every attempt and is never pruned. Back up `state/` and `deliverables/` together, while no run is active, so records, queue and history stay consistent.
- Run `resume` and `retry` from the same folder as the original `run` (stored paths are relative to it).
- A document in progress when the process is killed is redone by `resume`; its claim blocks other runs for `INTAKE_CLAIM_TTL_S`.
- The Databricks client enforces `INTAKE_TIMEOUT_S` as its HTTP timeout; any other provider client must do the same.
- The automated tests use mocked responses (and a fake HTTP transport for the Databricks client). `deliverables/accuracy.md` is a mocked report: it measures the pipeline and comparison, not a model. For a live measurement run `uv run intake --provider databricks --model-id <endpoint> accuracy --out deliverables/accuracy-<endpoint>.md` and keep that report labelled with its configuration.

See `deliverables/verification.md` for the checks, observed results and repairs.

## For the next developer

- **Versioning.** Bump `POLICY_VERSION` in `src/access_intake/policy.py` whenever routing rules or the deterministic record checks (`validate.py`, `conflicts.py`, `injection.py`) change. The next run then re-checks and re-routes stored extractions without model calls. `PROMPT_VERSION` in `prompt.py` includes a hash of the prompt text and changes automatically; a changed prompt refreshes records with new model calls.
- **Adding a provider.** Implement a client with `complete(request) -> ModelResponse` that maps its failures onto the errors in `llm/base.py`, as `llm/databricks.py` does (the header of `llm/provider.py` lists the contract), and select it in `llm/__init__.py:get_client` and `config.PROVIDERS`.
- **Mock fixtures.** Replies live in `tests/fixtures/mock_responses.json`, pinned to each export's SHA-256. After an intended edit to an example export, run `uv run python scripts/build_mock_fixtures.py --stamp` (it re-stamps the hashes, then runs the full check); update the fixture's extraction first if the facts changed. Expected truth is in `tests/expected/expected_fields.json`; edit it only from the source messages and policy, never from model output.
- **Tests** never call a provider; the Databricks client is tested against a fake HTTP transport.

## Layout

```
src/access_intake/   cli, config, loader, prompt, validate, injection, policy, store, pipeline,
                     accuracy, evidence, logging_redact, issues, llm/{base, mock, databricks, provider}
tests/               pytest suite; expected/expected_fields.json (truth); fixtures/ (mock replies, IT-101)
scripts/             build_mock_fixtures.py
config/              settings.example.toml
docs/                requirements, architecture-plan
deliverables/        accuracy.md, request-evidence.json, verification.md (+ outputs of intake run)
```
