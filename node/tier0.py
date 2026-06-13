"""hone-node Tier-0 deterministic resolver — the code phase of the
prepare task. Given a patchset's text, produce the deterministic
metadata fields (no LLM): base existence + resolving tree, the
authoritative subsystem + maintainer / reviewer / list sets (via
get_maintainer.pl), and the patch_size line counts. See
docs/ARCHITECTURE-PREPARE.md → Tier 0.

Everything degrades gracefully: no base-commit trailer, a base that
doesn't resolve, or a get_maintainer failure leaves the affected
fields in their heuristic form (authoritative sets null, source
"thread") for the LLM judgment phase / downstream to handle — it
never raises.
"""
import logging
import re
from collections import Counter

from node import cgit, maintainers, refrepo

log = logging.getLogger("hone.node.tier0")

# Bump when the deterministic resolution logic changes — stamped into
# the prepare record's meta so a resolver change is auditable
# independently of methodology_version (ARCHITECTURE-PREPARE.md → Dec 4).
# tier0-2: added tree_state.base_resolution (explicit found/absent/
# unknown/no_base outcome) + expanded the probed tree set.
# tier0-3: added tree_state.base_fallback — the no_base tip-at-submission
# hint (target tree from the subject prefix + the submission time).
RESOLVER_VERSION = "tier0-3"

# get_maintainer roles → the methodology's person buckets.
_MAINTAINER_ROLES = {"maintainer", "supporter"}
_REVIEWER_ROLES   = {"reviewer"}

# patch_size.bucket thresholds on total changed lines (added + removed).
_SIZE_BUCKETS = ((5, "trivial"), (50, "small"), (250, "medium"),
                  (1000, "large"))


def base_commit_trailer(patch_text):
    """The `base-commit:` trailer hash, or None. Reuses refrepo's regex
       so prepare and review agree on what counts as a declared base."""
    m = refrepo.BASE_RE.search(patch_text or "")
    return m.group(1) if m else None


# Subject [PATCH …] prefix tokens that name a target tree, longest first
# (net-next must be matched before net, which is its substring).
_PREFIX_TREES = ("net-next", "net")
_PATCH_BRACKET = re.compile(r"\[[^\]]*\bPATCH\b[^\]]*\]", re.I)


def fallback_tree(subject, trees):
    """The tree a no-base patch is aimed at, derived from its subject's
       [PATCH …] prefix — netdev encodes net vs net-next there. Returns a
       registry tree NAME (so review's refrepo can fetch it) or None when
       no token matches or the named tree isn't in the registry. Kept
       conservative on purpose: only the explicit prefix token is trusted
       (a bare `[PATCH]` yields None rather than a guessed default), since
       a wrong base is worse than none for the review apply."""
    if not subject:
        return None
    m = _PATCH_BRACKET.search(subject)
    tag = (m.group(0) if m else "").lower()
    for name in _PREFIX_TREES:
        if re.search(rf"\b{re.escape(name)}\b", tag) and trees.tree(name):
            return name
    return None


def size_bucket(total_changed):
    for ceiling, name in _SIZE_BUCKETS:
        if total_changed <= ceiling:
            return name
    return "huge"


def count_patch_size(patch_text, *, series_length=None):
    """Diff-derived patch_size counts — pure, no tree. churn_ratio is
       left null (it needs file lengths at base → Tier 2), and source is
       "thread" since nothing here consulted a tree."""
    lines_added = lines_removed = hunks = 0
    files_added = files_deleted = files_renamed = total_files = 0
    for line in (patch_text or "").splitlines():
        if line.startswith("diff --git "):
            total_files += 1
        elif line.startswith("new file mode"):
            files_added += 1
        elif line.startswith("deleted file mode"):
            files_deleted += 1
        elif line.startswith("rename to "):
            files_renamed += 1
        elif line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1
    files_modified = max(0, total_files - files_added - files_deleted
                          - files_renamed)
    return {
        "lines_added":    lines_added,
        "lines_removed":  lines_removed,
        "files_modified": files_modified,
        "files_added":    files_added,
        "files_deleted":  files_deleted,
        "files_renamed":  files_renamed,
        "hunks":          hunks,
        "bucket":         size_bucket(lines_added + lines_removed),
        "series_length":  series_length,
        "churn_ratio":    None,        # tree-only → computed at review
        "source":         "thread",
    }


def _heuristic_subsystem():
    """The subsystem block when get_maintainer didn't run — unresolved,
       source thread (the LLM / downstream may fill it heuristically)."""
    return {"primary": None, "primary_status": None, "primary_tree": None,
            "secondary": [], "cross_cutting": False, "uncertain_paths": [],
            "source": "thread"}


def _heuristic_maintainer(recipients):
    """The maintainer block when get_maintainer didn't run. Per the
       audit-#3 rule, the authoritative sets are NULL in heuristic mode
       — never a list scraped from To:/Cc:."""
    return {"primary": None, "primary_role": None,
            "authoritative_set": None, "authoritative_reviewer_set": None,
            "mailing_lists": [], "cc_coverage": None, "list_coverage": None,
            "engagement_rate": None, "out_of_scope_engaged": [],
            "all_engaged": [],
            "cc_list_size": len(recipients) if recipients is not None
                            else None,
            "primary_uncertain_reason": None, "source": "thread"}


def bucket_maintainer_entries(entries, *, recipients=None):
    """Split parsed get_maintainer entries into the methodology's
       subsystem + maintainer blocks. `recipients` (lower-cased To:/Cc:
       addresses) drives was_cc_d + the coverage ratios; pass None when
       unknown and those become null."""
    recips = {r.lower() for r in recipients} if recipients is not None \
        else None
    maint, revs, lists, sections = [], [], [], []
    for e in entries:
        if e.role in _MAINTAINER_ROLES:
            entry = {"email": e.email, "role": "maintainer"}
            if e.name is not None:
                entry["name"] = e.name
            maint.append(entry)
            if e.subsystem:
                sections.append(e.subsystem)
        elif e.role in _REVIEWER_ROLES:
            entry = {"email": e.email, "role": "reviewer"}
            if e.name is not None:
                entry["name"] = e.name
            revs.append(entry)
            if e.subsystem:
                sections.append(e.subsystem)
        elif "list" in e.role:
            lists.append({
                "address": e.email,
                "was_cc_d": (e.email.lower() in recips
                              if recips is not None else None)})

    counts = Counter(sections)
    ordered = [s for s, _n in counts.most_common()]
    cc_coverage = list_coverage = None
    if recips is not None:
        if maint:
            cc_coverage = sum(1 for m in maint
                              if m["email"].lower() in recips) / len(maint)
        if lists:
            list_coverage = sum(1 for x in lists
                                if x["address"].lower() in recips) / len(lists)

    # Authoritative ("tree") only when get_maintainer actually mapped the
    # patch to a MAINTAINERS section. A run that returned only mailing lists
    # (no M:/section) hasn't characterised the subsystem — leave it heuristic
    # so the LLM's path-derived guess survives _merge_deterministic instead of
    # being overwritten with a null primary, which the schema (primary must be
    # a non-empty string) then rejects with a 422.
    if ordered:
        subsystem = {"primary": ordered[0],
                     "primary_status": None, "primary_tree": None,
                     "secondary": ordered[1:], "cross_cutting": len(counts) >= 3,
                     "uncertain_paths": [], "source": "tree"}
    else:
        subsystem = _heuristic_subsystem()
    maintainer = {"primary": maint[0]["email"] if maint else None,
                  "primary_role": "maintainer" if maint else None,
                  "authoritative_set": maint,
                  "authoritative_reviewer_set": revs,
                  "mailing_lists": lists,
                  "cc_coverage": cc_coverage, "list_coverage": list_coverage,
                  "engagement_rate": None, "out_of_scope_engaged": [],
                  "all_engaged": [],
                  "cc_list_size": len(recipients) if recipients is not None
                                  else None,
                  "primary_uncertain_reason": None, "source": "tree"}
    return subsystem, maintainer


def resolve_deterministic(trees, patch_text, *, recipients=None,
                           series_length=None, timeout=None,
                           subject=None, sent=None):
    """Orchestrate the Tier-0 code phase. `trees` is a cgit.KernelTrees;
       `patch_text` is the patchset's combined content. Returns a dict of
       the deterministic metadata fields (base_*, subsystem, maintainer,
       patch_size) + resolver_version.

       Resolution is authoritative (source "tree") only when the base
       resolves in some tree AND get_maintainer succeeds; otherwise the
       subsystem/maintainer blocks stay heuristic. patch_size and the
       base trailer are always computed.

       `subject` + `sent` (the series subject and its submission unix
       time) feed the no_base fallback: when no base is declared but the
       subject prefix names a registry tree, record a tip-at-submission
       hint for the review task to resolve + apply against."""
    declared = base_commit_trailer(patch_text)
    result = {
        "base_in_tree":         None,
        "base_resolution":      "no_base",
        "base_tree":            None,
        "base_fallback":        None,
        "base_commit_declared": declared,
        "base_commit_source":   "trailer" if declared else "none",
        "subsystem":            _heuristic_subsystem(),
        "maintainer":           _heuristic_maintainer(recipients),
        "patch_size":           count_patch_size(patch_text,
                                                  series_length=series_length),
        "resolver_version":     RESOLVER_VERSION,
    }
    if not declared:
        target = fallback_tree(subject, trees)
        if target and sent is not None:
            result["base_fallback"] = {"tree": target,
                                       "strategy": "tip-at-submission",
                                       "as_of": sent}
        return result                       # base_resolution stays "no_base"

    lookup = trees.resolve_base(declared)
    if lookup.state == cgit.BASE_FOUND:
        result["base_in_tree"] = True
        result["base_resolution"] = "found"
        result["base_tree"] = lookup.tree.name
        kw = {"timeout": timeout} if timeout is not None else {}
        entries = maintainers.resolve_maintainers(
            lookup.client, declared, patch_text, **kw)
        if entries is not None:
            subsystem, maintainer = bucket_maintainer_entries(
                entries, recipients=recipients)
            result["subsystem"] = subsystem
            result["maintainer"] = maintainer
        else:
            log.info("tier0: base %s in %s but get_maintainer failed — "
                      "maintainer/subsystem heuristic", declared,
                      lookup.tree.name)
    elif lookup.state == cgit.BASE_ABSENT:
        result["base_in_tree"] = False
        result["base_resolution"] = "absent"
    else:                                   # BASE_UNKNOWN — couldn't determine
        result["base_resolution"] = "unknown"
    return result
