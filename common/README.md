# common

Cross-tier content shared by **hone-core** and **hone-node** — the things
both sides need to agree on at the wire-contract level. `common/` is a
Python package, so both tiers import from it as `from common import …`;
the JSON Schemas under `schema/` are language-agnostic but ship in the
same directory.

- [`schema/`](schema/) — JSON Schemas (draft 2020-12) that govern the
  contract:
  - [`methodology.schema.yaml`](schema/methodology.schema.yaml) —
    validates the methodology YAML (the seed in
    [`../core/default-methodology.yaml`](../core/default-methodology.yaml)
    and any later import / export). Every property carries a
    `description`, so the schema doubles as the functional spec of the
    methodology format.
  - [`completion-record.schema.yaml`](schema/completion-record.schema.yaml)
    — validates the body of `POST /v1/claims/{claim_id}/result`, with
    the four task-type branches `prepare` / `review` / `train` / `draft`.
- [`version.py`](version.py) — `__version__`, the single source of truth
  for the release version. hone-core renders it in the operator UI footer
  and hone-node prints it as a startup banner. See
  [`../CHANGELOG.md`](../CHANGELOG.md) for the versioning policy.
