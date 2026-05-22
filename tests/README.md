# tests

Unit tests for hone. Run from the project root:

```
pip install -r core/requirements.txt -r requirements-dev.txt
pytest
```

`conftest.py` puts the project root on `sys.path` so `core` / `node` import
as packages. Current coverage:

- `test_completion_record_schema.py` — `core/completion-record.schema.yaml`:
  the schema is valid draft-2020-12, well-formed records validate, malformed
  ones are rejected, and the review / maintenance shapes are isolated.
- `test_api_submit_result.py` — `POST /v1/claims/{claim_id}/result`: the
  schema validation, `state`/`outcome` guard, and dispatch in `submit_result`
  (with `core_db` stubbed).
