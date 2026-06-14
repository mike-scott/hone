"""hone — the single source of truth for the release version.

One version spans both tiers: hone-core renders it in the operator UI footer
as ``hone-core-<version>``, and hone-node prints it as a startup banner,
``hone-node-<version>``. The git tag ``v<version>`` mirrors this constant —
this file is what the running code reports; the tag marks the release commit.

Bump it together with a CHANGELOG.md entry on every release; see CHANGELOG.md
for the versioning policy.
"""
__version__ = "0.4.0"
