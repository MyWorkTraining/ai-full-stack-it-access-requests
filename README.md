# IT Access Request Intake

A command-line tool (`intake`) that turns exported access-request messages into records conforming to `snapshot.schema.json`, places each request in the review queue under `materials/office-policy.md`, and keeps a history of every attempt. The task is in `CHALLENGE.md`; the design is in `docs/architecture-plan.md`.

Nothing in this tool approves access. Every outcome waits for the named human reviewer.

## Supported runtime and provider

- Python 3.11 or later (verified on 3.12.3). Dependencies: `jsonschema`; `pytest` for tests. Locked in `uv.lock`.
- Linux or macOS (file locking uses `fcntl`; Windows falls back to `msvcrt`, untested).
- Model providers, chosen with `INTAKE_PROVIDER` (or `--provider`, or `provider` in the settings file):
  - `mock` (default): answers from `tests/fixtures/mock_responses.json` and can simulate provider faults. All tests use it.
  - `databricks`: a Databricks Model Serving / Foundation Model API endpoint, called over HTTPS with the standard library (`src/access_intake/llm/databricks.py`). Set `INTAKE_MODEL_ID` to the endpoint name, the workspace URL in `INTAKE_DATABRICKS_HOST` or `DATABRICKS_HOST`, and the token in `API_KEY` (`DATABRICKS_TOKEN` is used if `API_KEY` is unset). Supported configuration: `databricks-claude-sonnet-4-5` with `structured_output = json_schema` and `temperature = 0`.
  - `provider`: a placeholder for another provider (`src/access_intake/llm/provider.py`); it fails every call cleanly with `provider-not-configured`.

  Every record's `response_source` says which produced it (`mock` or `databricks/<endpoint>`).

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

### Outputs

- `deliverables/extractions.jsonl`: one current record per document: `document_id`, `source_sha256`, `extraction` (or null on error), `status`, `owner`, `next_action`, `error` (or null), `response_source`, plus routing reasons, flags, provider, model, prompt and policy versions, settings fingerprints, `attempt_id` and `last_valid_attempt`.
- `deliverables/queue.json`: the review queue, one entry per record, linked by `document_id`, with counts by owner and next action.
- `state/`: history of every attempt, current records, run logs and claims. Not committed (`.gitignore`); keep it with the deliverables it produced.

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
- Two operators in one process are tested; separate processes rely on the same file lock but are not tested directly. Network filesystems without reliable locking are not supported.
- Run `resume` and `retry` from the same folder as the original `run` (stored paths are relative to it).
- A document in progress when the process is killed is redone by `resume`; its claim blocks other runs for `INTAKE_CLAIM_TTL_S`.
- The Databricks client enforces `INTAKE_TIMEOUT_S` as its HTTP timeout; any other provider client must do the same.
- The automated tests use mocked responses (and a fake HTTP transport for the Databricks client). `deliverables/accuracy.md` is a mocked report: it measures the pipeline and comparison, not a model. For a live measurement run `uv run intake --provider databricks --model-id <endpoint> accuracy --out deliverables/accuracy-<endpoint>.md` and keep that report labelled with its configuration.

See `deliverables/verification.md` for the checks, observed results and repairs.

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
