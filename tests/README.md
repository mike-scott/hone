# tests

Unit tests for hone. Run from the project root, on the **same Python the
container images pin** (`core/Dockerfile` — currently 3.14):

```
python3.14 -m venv .venv && . .venv/bin/activate
pip install -r core/requirements.txt -r node/requirements.txt -r requirements-dev.txt
pytest
```

`conftest.py` puts the project root on `sys.path` so `core` / `node` import
as packages, and **fails the run if the interpreter does not match** the
Python version the Dockerfiles pin — the test environment must match the
runtime environment. Current coverage:

- `test_completion_record_schema.py` — `core/completion-record.schema.yaml`:
  the schema is valid draft-2020-12, well-formed records validate, malformed
  ones are rejected, and the review / maintenance shapes are isolated.
- `test_api_submit_result.py` — `POST /v1/claims/{claim_id}/result`: the
  schema validation, `state`/`outcome` guard, and dispatch in `submit_result`.
- `test_tls.py` — `core/tls.py`: the self-generated CA + server certificate
  (chain, SAN, key identifiers, idempotence).
- `test_core_db_enrollment.py` — the OAuth enrollment / token data layer
  (schema migration 2): enrollments, approval, token issue/resolve/rotate,
  node revocation.
- `test_oauth_endpoints.py` — the `/v1/oauth/*` device-grant endpoints and
  bearer auth on the main API.
- `test_ui_enrollment.py` — the operator node-management / enrollment UI.
- `test_ui_queue.py` — the review-queue home page and the queue query
  helpers (`review_counts`, `list_reviews`).
- `test_ui_settings.py` — the Settings page: rendering, masked secrets,
  save-and-apply, and the validation rejections.
- `test_node_client.py` — the hone-node `HoneCoreClient`: identity
  persistence and the auth-failure paths.
- `test_node_backoff.py` — the hone-node transient-failure backoff: which
  failures are transient, `Retry-After`, and the retry loop.
- `test_gather.py` — the GATHER pass: a gather module's patchsets ingested
  into the corpus (dedup, `.tar.zst` blob, findings, queued review), the
  per-source watermark, and the supervisor's per-source task scheduling.
- `test_runtime_config.py` — `core/runtime_config.py`: the `config.yaml`
  operator-tunable layer (defaults, env seeding, file overlay, save).
- `test_version.py` — `common/version.py`: the release version is valid
  SemVer and the hone-node startup banner carries it.
