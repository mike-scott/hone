"""check_gates.py — deterministic applicability of methodology review checks.

The "most used checks" metric needs a denominator: of the patches where a
check *could* apply, how often did it fire? A review records only the numerator
(concerns, each tagged with `candidate_or_check_id`). The methodology checks
state applicability in prose only — the check object is id/stage/title/body,
with no machine-readable gate — so this module derives applicability
deterministically from the patch: a small feature vector grepped from the diffs
plus the prepared `patch_type` covariate, evaluated against a per-check gate
registry.

Derived, not model-reported: an LLM asked "which checks did you apply?" has no
ground truth and rubber-stamps everything, so a self-reported denominator would
be near-constant. The features here are cheap and objective. And because every
input (patch bodies, covariates, the methodology check list) is retained in the
DB, coverage is RECOMPUTABLE — refine a gate and rederive past reviews via
coverage_for_review(); no data is lost.

The check LIST comes from the methodology document, so the universe tracks the
methodology version automatically. Only the per-check predicate lives here; a
check id with no registered gate falls back to FEATURE_TOUCHES_C and is marked
gate="default", so a newly added check that needs its own gate shows up in the
data instead of being silently mis-bucketed.

`applicable` and `fired` are computed INDEPENDENTLY: a concern can fire under a
check our gate marked not-applicable (the model applied it anyway, or the gate
is too narrow). That `applicable=false, fired=true` mismatch is signal — it
tells you to widen a gate — so it is deliberately not papered over.
"""
import json
import re

from core import core_db

# --- feature vocabulary -----------------------------------------------------

FEATURE_TOUCHES_C = "touches_c"
FEATURE_USES_RCU = "uses_rcu"
FEATURE_USES_LOCKS = "uses_locks"
FEATURE_ADDS_FUNCTION = "adds_function"
FEATURE_IS_BUGFIX = "is_bugfix"
FEATURE_TOUCHES_DOC_CONTRACT = "touches_doc_contract"

_C_EXT = (".c", ".h")
_DOC_PATH_MARKERS = ("/uapi/", "include/uapi/", "documentation/")

_RE_DIFF_FILE = re.compile(r'^\+\+\+ b/(\S+)', re.M)
_RE_RCU = re.compile(
    r'\b(?:rcu_read_lock|rcu_read_unlock|rcu_dereference\w*|rcu_assign_pointer'
    r'|call_rcu|kfree_rcu|synchronize_rcu|srcu_\w+)\b|\blist_\w+_rcu\b')
_RE_LOCK = re.compile(
    r'\b(?:mutex_lock\w*|mutex_unlock|spin_lock\w*|spin_unlock\w*'
    r'|raw_spin_lock\w*|read_lock\w*|write_lock\w*|down_read\w*'
    r'|down_write\w*)\b')
_RE_EXPORT = re.compile(r'\bEXPORT_SYMBOL(?:_GPL|_NS|_NS_GPL)?\b')
# A function-definition opener on an added line — identifier-led signature
# ending in `)` with an optional `{`. Heuristic: the function-contract gate is
# the fuzzy one (it may over/0r under-count "introduced or generalised"); the
# mismatch signal above is how we'll learn to tune it.
_RE_FUNC_DEF = re.compile(
    r'^[A-Za-z_][\w\s\*]*\b[A-Za-z_]\w*\s*\([^;]*\)\s*\{?\s*$')


def extract_features(patch_texts, *, patch_type_primary=None):
    """The objective feature vector for a series, grepped from its patch diffs
       (raw bodies, hunks included) plus the prepared patch_type covariate."""
    blob = "\n".join(t for t in patch_texts if t)
    files = _RE_DIFF_FILE.findall(blob)
    added = "\n".join(
        line[1:] for line in blob.splitlines()
        if line.startswith("+") and not line.startswith("+++"))
    adds_function = bool(_RE_EXPORT.search(added)) or any(
        _RE_FUNC_DEF.match(line) for line in added.splitlines())
    return {
        FEATURE_TOUCHES_C: any(f.lower().endswith(_C_EXT) for f in files),
        FEATURE_USES_RCU: bool(_RE_RCU.search(blob)),
        FEATURE_USES_LOCKS: bool(_RE_LOCK.search(blob)),
        FEATURE_ADDS_FUNCTION: adds_function,
        FEATURE_IS_BUGFIX: patch_type_primary == "bugfix",
        FEATURE_TOUCHES_DOC_CONTRACT: any(
            m in f.lower() for f in files for m in _DOC_PATH_MARKERS),
    }


# --- per-check applicability gates ------------------------------------------
# The feature that must hold for a check to be "applicable" to a patch.
# Deliberately conservative — widen as the applicable=false/fired=true mismatch
# shows a gate excluding patches a check genuinely runs on. Keyed by the
# default-methodology check ids; an id absent here falls back to _DEFAULT_GATE.
_GATES = {
    "object-lifetime":         FEATURE_TOUCHES_C,          # any C: every deref
    "concurrency":             FEATURE_TOUCHES_C,          # any C: shared state
    "lock-storage-lifetime":   FEATURE_USES_LOCKS,         # only if it locks
    "integer-safety":          FEATURE_TOUCHES_C,          # any C: arithmetic
    "error-teardown":          FEATURE_TOUCHES_C,          # any C: error paths
    "efficacy-and-root-cause": FEATURE_IS_BUGFIX,          # does the fix work
    "function-contract":       FEATURE_ADDS_FUNCTION,      # new/generalised fn
    "preexisting-issues":      FEATURE_TOUCHES_C,          # any C nearby
    "documented-contract":     FEATURE_TOUCHES_DOC_CONTRACT,  # uapi/ABI/docs
    "subsystem-checklists":    FEATURE_USES_RCU,           # only RCU checklist
}
_DEFAULT_GATE = FEATURE_TOUCHES_C


def _fired_index(concerns):
    """check/candidate id -> number of concerns it produced. A concern counts
       for its primary `candidate_or_check_id` and for every
       `contributing_check_ids` entry (the full set Stage C records on a merge),
       so a check credited only as a contributor still reads as fired."""
    counts = {}
    for c in concerns or []:
        ids = set()
        if c.get("candidate_or_check_id"):
            ids.add(c["candidate_or_check_id"])
        ids.update(cid for cid in (c.get("contributing_check_ids") or []) if cid)
        for cid in ids:
            counts[cid] = counts.get(cid, 0) + 1
    return counts


def compute_coverage(check_ids, patch_texts, *, patch_type_primary=None,
                     concerns=None):
    """Per-check coverage for one review. For every id in `check_ids`: was it
       applicable (its gate over the series features) and did it fire (from
       concerns). Returns a list of dicts
         {id, applicable, gate ("specific"|"default"), fired, n_concerns}.
       Pure — all inputs are plain data — so it is unit-testable and
       recomputable from stored review data."""
    feats = extract_features(patch_texts, patch_type_primary=patch_type_primary)
    fired = _fired_index(concerns)
    out = []
    for cid in check_ids:
        gate = _GATES.get(cid, _DEFAULT_GATE)
        out.append({
            "id":         cid,
            "applicable": bool(feats.get(gate)),
            "gate":       "specific" if cid in _GATES else "default",
            "fired":      cid in fired,
            "n_concerns": fired.get(cid, 0),
        })
    return out


def coverage_for_review(db, root_message_id, methodology_document, concerns):
    """DB-aware wrapper: gather the series' patch bodies and prepared
       patch_type from the corpus and compute per-check coverage against the
       methodology document's check list. Recompute-friendly — call any time to
       rederive a stored review's coverage. Returns the coverage list, or None
       when the methodology carries no checks (nothing to track)."""
    checks = (methodology_document or {}).get("checks") or []
    check_ids = [c["id"] for c in checks if c.get("id")]
    if not check_ids:
        return None
    patch_texts = [
        m["body"] for m in core_db.messages_for_patchset(
            db, root_message_id, type=core_db.MSG_TYPE_PATCH)
        if m.get("body")]
    meta = core_db.get_patchset_metadata(db, root_message_id) or {}
    patch_type = meta.get("patch_type")
    if isinstance(patch_type, str):           # tolerate an undecoded column
        try:
            patch_type = json.loads(patch_type)
        except ValueError:
            patch_type = {}
    primary = patch_type.get("primary") if isinstance(patch_type, dict) else None
    return compute_coverage(check_ids, patch_texts,
                            patch_type_primary=primary, concerns=concerns)
