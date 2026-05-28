"""hone-node cgit client — point lookups against a cgit-hosted kernel
tree (kernel.org's linux-next by default), for the prepare task's
deterministic phase. See docs/ARCHITECTURE-PREPARE.md → Tier 0.

cgit is an HTML git viewer, not a REST API, but its HTTP semantics give
exactly the two lookups the deterministic phase needs:

  - base_in_tree(sha)   — does the commit exist in the tree?
                          HEAD /commit/?id=<sha> → 200 yes / 404 no.
                          HEAD short-circuits cgit's diff render, so it
                          stays sub-second even for huge commits.
  - fetch_file_at(p, s) — the file's text at that commit.
                          GET /plain/<path>?id=<sha>.

Why this lives on the node rather than in hone-core: each node fetches
from its own egress IP, so a corpus-scale backfill spreads across the
fleet instead of hammering kernel.org from one address (the profile it
rate-limits). Transient failures (429 / timeout) return None so the
caller degrades to heuristic mode; the node's existing backoff
(node/runner.py) handles retry on the next claim.

Caching is per-node and per-process: definite answers (a commit's
existence, a file's bytes at a SHA) are immutable for the window a node
processes a batch, so they're cached. Indeterminate results (network
error → None) are never cached, so a later attempt can retry.
"""
import logging
from collections import namedtuple

import httpx

log = logging.getLogger("hone.node.cgit")

_KERNELORG_CGIT = "https://git.kernel.org/pub/scm/linux/kernel/git"
_KERNELORG_GIT  = "git://git.kernel.org/pub/scm/linux/kernel/git"

# One registry entry: a tree accessed two ways — cgit (HTTP, for the
# Tier-0 existence probe + MAINTAINERS fetch) and git (for review's
# refrepo fetch). Keyed by a canonical `name` that crosses both phases
# (a prepare record's `tree_state.base_tree` is this name; refrepo maps
# it back to `git_url`). See docs/ARCHITECTURE-PREPARE.md → named-trees
# registry.
Tree = namedtuple("Tree", ["name", "cgit_url", "git_url"])


def _tree(name, path):
    return Tree(name, f"{_KERNELORG_CGIT}/{path}", f"{_KERNELORG_GIT}/{path}")


# Ordered registry the deterministic phase probes for a declared base
# commit. Order is resolution priority — the first tree containing the
# commit wins, the rest are skipped. linux-next leads deliberately: it
# merges the subsystem trees daily, so a base really in (say) net-next
# is usually also in linux-next, hitting on probe #1. mainline catches
# release / -rc-based patches, stable catches backports.
#
# The remaining trees are subsystem integration trees, probed only when
# next/mainline/stable miss — i.e. a base committed to a maintainer tree
# that linux-next hasn't merged yet (the narrow window between commit and
# next's daily pull). They're ordered for this corpus's skew (Qualcomm /
# arm64 / DT / clk): the arm-soc aggregation (soc) and the Qualcomm tree
# (qcom) first, then arm64 core, then the clk / pinctrl subsystems.
# net-next and tip stay for networking / x86-core bases.
#
# Adding a tree costs a probe per UNVERIFIABLE base (FOUND short-circuits
# at linux-next) and a potential refrepo fetch source, so the set is kept
# to high-yield trees; deployments tune it with HONE_CGIT_TREES. Shared
# with review's refrepo remote list so the two can't drift.
DEFAULT_TREES = (
    _tree("linux-next", "next/linux-next.git"),
    _tree("mainline",   "torvalds/linux.git"),
    _tree("stable",     "stable/linux.git"),
    _tree("net-next",   "netdev/net-next.git"),
    _tree("tip",        "tip/tip.git"),
    _tree("soc",        "soc/soc.git"),
    _tree("qcom",       "qcom/linux.git"),
    _tree("arm64",      "arm64/linux.git"),
    _tree("clk",        "clk/linux.git"),
    _tree("pinctrl",    "linusw/linux-pinctrl.git"),
)


def _derive_git_url(cgit_url):
    """git fetch URL for a cgit URL — kernel.org serves the same path
       over both schemes, so swap https→git://. Non-https URLs pass
       through unchanged (git can often fetch over https anyway)."""
    return cgit_url.replace("https://", "git://", 1)


def parse_trees_env(value):
    """Parse a HONE_CGIT_TREES override into an ordered list of Tree.
       Format is comma-separated `name=cgit_url`, order = priority:

           linux-next=https://…/linux-next.git,net-next=https://…

       The git fetch URL is derived from the cgit URL (https→git://).
       Empty / unset → DEFAULT_TREES. A malformed entry raises
       ValueError so a typo'd deployment fails fast at startup rather
       than silently probing nothing."""
    if not value or not value.strip():
        return list(DEFAULT_TREES)
    trees = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")     # first '=' only; URLs
        name, url = name.strip(), url.strip()     # have none before it
        if not sep or not name or not url:
            raise ValueError(
                f"bad HONE_CGIT_TREES entry {item!r} — expected name=cgit_url")
        trees.append(Tree(name, url, _derive_git_url(url)))
    return trees or list(DEFAULT_TREES)


# resolve_base outcomes. FOUND carries the resolving Tree + its client;
# ABSENT means every probed tree returned a definite 404; UNKNOWN means
# no hit AND at least one tree was indeterminate (network error /
# timeout) — so the base might still exist somewhere we couldn't reach.
BASE_FOUND   = "found"
BASE_ABSENT  = "absent"
BASE_UNKNOWN = "unknown"

# `tree` is the resolving Tree on FOUND (carries name + git_url for the
# caller to persist / hand to refrepo), None otherwise.
BaseLookup = namedtuple("BaseLookup", ["state", "tree", "client"])


class CgitClient:
    """Point-lookup client for one cgit-hosted tree. Construct once and
       reuse — it holds an httpx connection pool and the per-process
       lookup cache. `http` is injectable for tests (any object with
       `.head(url, follow_redirects=...)` and `.get(...)` returning a
       response carrying `.status_code` and `.text`)."""

    def __init__(self, base_url, *, timeout=30.0, http=None):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._http = http                       # injected, or lazy below
        self._exists_cache: dict[str, bool] = {}
        self._file_cache: dict[tuple[str, str], str] = {}

    def _client(self):
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout,
                                       follow_redirects=True)
        return self._http

    def base_in_tree(self, sha):
        """True if `sha` is a commit in the tree, False if cgit says it
           isn't (404), None if we couldn't determine (network error,
           timeout, unexpected status). The tri-state matters: False is
           real data (`tree_state.base_in_tree = false`), None means
           heuristic-mode-for-this-field.

           Definite True/False are cached; None is not (so a retry can
           still resolve it)."""
        if not sha:
            return None
        if sha in self._exists_cache:
            return self._exists_cache[sha]
        url = f"{self._base_url}/commit/?id={sha}"
        try:
            resp = self._client().head(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            log.warning("cgit base_in_tree(%s) network error: %s", sha, exc)
            return None
        if resp.status_code == 200:
            self._exists_cache[sha] = True
            return True
        if resp.status_code == 404:
            self._exists_cache[sha] = False
            return False
        log.warning("cgit base_in_tree(%s) unexpected status %d",
                     sha, resp.status_code)
        return None

    def fetch_file_at(self, path, sha):
        """The text of `path` at commit `sha`, or None if the file/commit
           isn't there (404) or we couldn't fetch it (network error,
           timeout, unexpected status). Missing-vs-unfetchable collapse
           to None on purpose — both degrade the deterministic phase the
           same way. Successful fetches are cached by (sha, path)."""
        if not sha or not path:
            return None
        key = (sha, path)
        if key in self._file_cache:
            return self._file_cache[key]
        url = f"{self._base_url}/plain/{path}?id={sha}"
        try:
            resp = self._client().get(url, follow_redirects=True)
        except httpx.HTTPError as exc:
            log.warning("cgit fetch_file_at(%s@%s) network error: %s",
                         path, sha, exc)
            return None
        if resp.status_code == 200:
            self._file_cache[key] = resp.text
            return resp.text
        if resp.status_code != 404:
            log.warning("cgit fetch_file_at(%s@%s) unexpected status %d",
                         path, sha, resp.status_code)
        return None

    def close(self):
        """Close the underlying httpx client if we created one."""
        if self._http is not None:
            self._http.close()
            self._http = None


class KernelTrees:
    """An ordered set of named cgit trees the deterministic phase probes
       for a declared base commit. Wraps one CgitClient per tree; keeps
       each client single-tree (CgitClient stays simple) while the set
       owns the cross-tree resolution + its tri-state aggregation.

       Construct from a spec via `from_spec([(name, url), …])`, or
       directly with pre-built `(name, CgitClient)` pairs (tests)."""

    def __init__(self, entries):
        self._entries = list(entries)             # [(Tree, CgitClient), …]
        self._by_name = {t.name: t for t, _c in self._entries}
        self._cache: dict[str, BaseLookup] = {}

    @classmethod
    def from_registry(cls, registry, *, timeout=30.0, http=None):
        """Build from an ordered iterable of Tree (e.g. cfg.cgit_trees /
           DEFAULT_TREES). One CgitClient per tree, probing its
           cgit_url. `http` is an optional shared injected client for
           tests."""
        return cls([(t, CgitClient(t.cgit_url, timeout=timeout, http=http))
                    for t in registry])

    def tree(self, name):
        """The registry Tree for a canonical name, or None — lets a
           caller (refrepo) map a recorded `base_tree` back to its
           git_url."""
        return self._by_name.get(name)

    def resolve_base(self, sha):
        """Probe the trees in priority order for commit `sha`. Returns a
           BaseLookup:

             - (FOUND, Tree, client)   first tree that has it; the rest
                                       are skipped (short-circuit).
             - (ABSENT, None, None)    every tree returned a definite 404.
             - (UNKNOWN, None, None)   no hit, and at least one tree was
                                       indeterminate — the base may exist
                                       somewhere we couldn't reach, so we
                                       must NOT report it absent.

           The resolving Tree carries the canonical name (persist as
           `tree_state.base_tree`) and its git_url (review's fetch
           hint); the `client` is returned so the caller can fetch
           MAINTAINERS@<sha> from the same tree — safe because a commit
           SHA is content-addressed, so the file is byte-identical in
           any tree that contains the commit.

           FOUND and ABSENT are definite and cached; UNKNOWN is not, so a
           later attempt can still resolve once the unreachable tree
           recovers."""
        if not sha:
            return BaseLookup(BASE_UNKNOWN, None, None)
        if sha in self._cache:
            return self._cache[sha]
        saw_indeterminate = False
        for tree, client in self._entries:
            present = client.base_in_tree(sha)
            if present is True:
                hit = BaseLookup(BASE_FOUND, tree, client)
                self._cache[sha] = hit
                return hit
            if present is None:
                saw_indeterminate = True
            # present is False → this tree definitively lacks it; continue
        result = BaseLookup(
            BASE_UNKNOWN if saw_indeterminate else BASE_ABSENT, None, None)
        if result.state == BASE_ABSENT:
            self._cache[sha] = result            # definite — cache it
        return result

    def close(self):
        for _tree, client in self._entries:
            client.close()
