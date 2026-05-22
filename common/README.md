# common

Code shared by the **hone-core** and the **AI node** tiers — chiefly the
typed models for the REST payloads defined in `API.md` (the claim, the review
completion record, the maintenance-task records) and any other cross-tier
types. Populated as the two services are built.

- `version.py` — `__version__`, the single source of truth for the release
  version. hone-core renders it in the operator UI footer and hone-node prints
  it as a startup banner. See `../CHANGELOG.md` for the versioning policy.
