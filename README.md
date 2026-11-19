# Northstar access-request intake

A Python CLI turns exported free-text messages into schema-validated extraction records and an ordered human-review queue. It prepares requests for people; a named manager is a contact, and successful intake never means approval.

## Setup and tests

Supported environment: Python 3.11+ on a local POSIX filesystem; this submission was exercised with Python 3.12 on Linux. Install `uv`, then run from the repository root:

```bash
uv sync --frozen
. .venv/bin/activate
env -u API_KEY python3 -m unittest discover -s tests -v
```

`pyproject.toml` declares jsonschema; `uv.lock` locks it and its dependencies. Setup may download locked packages. Tests and mock intake need no credentials or network. The tests forbid sockets and any read of API_KEY, including in guarded CLI subprocesses. A real-provider authentication unit check injects an inert credential function and inspects the environment-reader source; it never calls that reader. Target one planned test with `python3 -m unittest discover -s tests -k test_r53_contract -v`; zero discovered tests is not a pass.

## Reproducible mock intake and inspection

These commands run after activation from the repository root. Use a fresh database path for a fresh demonstration; rerunning the same input/configuration reuses completed valid work.

<!-- smoke -->
```bash
python3 -m intake run --input materials/requests --db var/demo.sqlite --provider mock --responses tests/mock_responses --output deliverables
python3 -m intake queue --db var/demo.sqlite --include-pending
python3 -m intake resume --db var/demo.sqlite --provider mock --responses tests/mock_responses
python3 -m intake retry --db var/demo.sqlite --document IT-010.json --provider mock --responses tests/mock_responses
python3 -m intake history --db var/demo.sqlite --document IT-010.json
python3 -m intake export --db var/demo.sqlite --output deliverables
```

`run` accepts a directory or one exported JSON file. `queue` reads committed current outcomes even while another CLI process is preparing other documents; `--include-pending` also shows pending/running work without pretending it has a completed business status. `history` retains every attempt with its source bytes/hash, configuration, policy identity and outcome. Inputs absent from a later batch remain present. A corrected message or relevant producing-settings change creates a new attempt; `--refresh` explicitly forces one. Explicit retry bypasses reuse, and a failed retry becomes the current error while previous success remains identifiable in history.

Run recovery with `resume` after a crash or deployment interruption. It continues pending work and running work whose claim expired; the default claim lifetime is request timeout + 5 seconds. An active claim is left alone. Resume reconstructs settings per document, supports parallel progress and refreshes work if prompt/schema/policy versions changed. An older or expired worker token cannot replace a newer claim's result. Pending work with changed settings is marked superseded in history rather than mislabelled as produced under those earlier settings.

Each error includes an actionable diagnostic and a reproducible `retry_command`; use that command against the same recorded database/configuration. CLI exit status is 0 on successful execution, 1 when current error records or failed accuracy comparisons remain, and 2 for command/input/configuration errors. A `needs-information` request is successful intake awaiting clarification, not an extraction error.

## Keyboard and screen-reader operation

Add `--format text` after any subcommand for labelled output in reading order. This mode uses ordinary terminal text with no color, animation, cursor control or timed input. Each queue entry starts with its document name and work state, followed by its routing, errors and retry guidance when available. In history, each attempt has its own state, source and producing settings. Use the terminal's screen-reader navigation commands to review earlier lines. Font size, contrast and cursor preferences are set in the terminal.

```bash
python3 -m intake --help
python3 -m intake queue --help
python3 -m intake queue --db var/demo.sqlite --include-pending --format text
python3 -m intake history --db var/demo.sqlite --document IT-010.json --format text
```

Use the same `--format text` option with `run`, `resume`, `retry`, `export` and `accuracy`. While intake or resume is active, inspect progress from a second terminal with `queue --include-pending`. Read the `Retry command` line under the failed document and run it to retry that document. `export --format text` identifies the published generation, and `accuracy --format text` gives the match count and report path. JSON remains the default for command output and the format of the machine-readable exports.

Use a keyboard-operable terminal exposing its text through the platform's accessibility services, such as Xfce Terminal with Orca on Linux. See the [Section 508 verification results](deliverables/verification.md#section-508-accessibility) for tested versions, workflows and scope.

To review a long queue with Orca, save the labelled output, then open it in a pager.

```bash
python3 -m intake queue --db var/demo.sqlite --include-pending --format text > queue.txt
less -j1 queue.txt
```

In `less`, press `g` to go to the beginning or type `/Document: IT-010` and press Enter to find a specific request. With the tested Orca 46.1 desktop layout, press `Insert` + numpad `7` to read from the top of the terminal and numpad `9` to read each next line. Press numpad minus to leave flat review, then press `q` to return to the shell.

## Provider configuration

Mock mode reads only saved JSON replies and the development manifest under `tests/mock_responses/`; it never reads API_KEY. `--script` selects another saved manifest. Reply sequences reset when a new mock process is created; the durable record of attempts remains in SQLite. The production extractor does not look up known filenames or import tests/expected. `--mock-gates` is a development-only directory of file barriers for repeatable process/interruption tests.

Implemented live configuration: OpenAI Responses API, explicit `--model` (the documented reference configuration is `gpt-4.1-mini`), HTTPS endpoint `https://api.openai.com/v1/responses`, structured JSON schema with strict mode, `store=false`, and `--max-output-tokens 4096`. The prompt is `intake/prompt.txt`, and the schema is `snapshot.schema.json`. The adapter validates completed responses, refusal content, malformed JSON, schema types and literal evidence before routing. It does not execute embedded source instructions.

For an optional live run, have the operator supply API_KEY through the environment and select `--provider openai --model gpt-4.1-mini`. This implementation session did not read a real key, call a real model, or observe live performance. `API_KEY` is the only application credential environment variable. Model/provider/endpoint/timeouts are CLI configuration, not inferred from environment variables; changing `--endpoint` requires a compatible HTTPS service. Authentication is loaded lazily only by the real adapter. Logs/errors/evidence omit credential values and untrusted provider error bodies.

`run`, `resume` and `retry` accept `--workers` (default 4), `--timeout` seconds (default 30), `--max-retries` (default 0) and `--retry-delay` seconds (default 1, exponential between retryable attempts). Retries are bounded per document. Request limits, temporary provider errors and timeouts affect the document involved while other workers can continue. There is no organization-wide quota coordinator. Request settings, prompt/schema hashes and policy hash are persisted; changes to relevant production/routing settings refresh affected results. Policy changes currently re-extract rather than optimize to rerouting alone.

Provider request and response fields were checked against the official [Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create), [structured outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs) and [model documentation](https://developers.openai.com/api/docs/models/gpt-4.1-mini). `deliverables/request-evidence.json` is an intercepted real-adapter request backed by a saved mock reply, with credential redaction; it is not a live observation.

## Accuracy and source evidence

```bash
python3 -m intake accuracy --input materials/requests --synthetic tests/fixtures/IT-P01.json --expected tests/expected --responses tests/mock_responses --provider mock --output deliverables/accuracy.md
```

Expected facts were authored from the plan/messages/policy before extraction runs, separately from the mocked provider replies. The synthetic input uses filename IT-P01.json to match its stated identifier and preserve the plan's complete staging/read case. The report contains 20 supplied cases plus that paraphrase, exact source quotes, producing configuration and 11 non-purpose scalar comparisons per case (231 total). Missing/error or duplicate-case extraction counts as 11 mismatches; nulls count and comparisons are exact, case-sensitive, without normalization. Extra cases, routing errors and schema/evidence/issues problems are reported separately. Purpose prose is reviewed against source evidence separately; a faithful paraphrase is not rejected for differing wording. Nonliteral purpose output is flagged for manual semantic review rather than automatically declared factually wrong. The JSON findings are saved beside the Markdown report.

The mock result tests the pipeline, not live model accuracy. It cannot establish fresh-wording extraction quality or universal resistance to instruction attacks. Factual-error mutation tests deliberately change otherwise valid saved-response facts so copied or self-derived expectations cannot silently pass. No minimum accuracy score or latency/throughput threshold has been invented.

## Shared operation, history and publication limits

Support is for multiple CLI processes on one host sharing one local SQLite database and local output directory; multi-host and network-share locking are not supported. Transactions keep claims/current pointers coherent. Immutable attempts are separate from current outputs. The latest accepted ingestion order controls the current source revision; export timestamps are display metadata. A late response can be retained in earlier history but cannot replace a newer current document revision.

Exports are generated from one committed snapshot. An advisory publisher lock serializes snapshot-to-publication work. `deliverables/current` atomically selects an immutable `.generations/` directory, and `extractions.jsonl`/`queue.json` are stable links through it. A reader inspecting both files during concurrent publication must resolve and pin `current` once, then read both files in that directory. Abandoned unpublished generations may remain on disk after interruption; retain them for investigation. Export again to publish the latest database state. The database is authoritative between exports.

The source's ordinary 200–400 messages/day and roughly 2,000-message bursts are operating context, not measured capacity. Verification covers the 20 supplied messages, a paraphrase, controlled concurrency/failure/interruption scenarios, and a 200-word input. It includes no 2,000-message load test, distributed deployment check, power-loss durability certification, live quota test or live-model accuracy measurement. Generic free-text facts and attack intent still need human scrutiny; exact quotes and schema checks cannot prove all semantics.

See `deliverables/verification.md` for per-step red/green evidence, assertions and remaining external checks. Final facilitator session-capture pairing and Classroom submission are external acceptance work; the application and the mock suite do not perform them.
