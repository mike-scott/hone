"""Tests for node/cgit.py — the cgit point-lookup client the prepare
task's deterministic phase uses. Exercises the tri-state base-existence
check, the file fetch, caching, and graceful degradation on network
errors, all through an injected fake HTTP client (no network)."""
import httpx
import pytest

from node import cgit


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class _FakeHttp:
    """Duck-typed stand-in for httpx.Client. `head` / `get` may each be a
       single _Resp, a single Exception (raised), or a list of those
       consumed one per call (for retry-after-error tests). Records the
       URLs it was called with."""

    def __init__(self, head=None, get=None):
        self._head = head
        self._get = get
        self.head_urls = []
        self.get_urls = []

    @staticmethod
    def _take(slot):
        item = slot.pop(0) if isinstance(slot, list) else slot
        if isinstance(item, Exception):
            raise item
        return item

    def head(self, url, follow_redirects=False):
        self.head_urls.append(url)
        return self._take(self._head)

    def get(self, url, follow_redirects=False):
        self.get_urls.append(url)
        return self._take(self._get)


def _client(http, base="https://x/repo.git"):
    return cgit.CgitClient(base, http=http)


# --- base_in_tree: tri-state -----------------------------------------------

def test_base_in_tree_true_on_200():
    http = _FakeHttp(head=_Resp(200))
    assert _client(http).base_in_tree("abc") is True
    assert http.head_urls == ["https://x/repo.git/commit/?id=abc"]


def test_base_in_tree_false_on_404():
    assert _client(_FakeHttp(head=_Resp(404))).base_in_tree("abc") is False


def test_base_in_tree_none_on_unexpected_status():
    """A 5xx / anything-not-200-or-404 is indeterminate, not a definite
       'not in tree' — return None so the field degrades to heuristic
       rather than asserting a false negative."""
    assert _client(_FakeHttp(head=_Resp(503))).base_in_tree("abc") is None


def test_base_in_tree_none_on_network_error():
    http = _FakeHttp(head=httpx.ConnectError("boom"))
    assert _client(http).base_in_tree("abc") is None


def test_base_in_tree_none_on_empty_sha_skips_http():
    http = _FakeHttp(head=_Resp(200))
    assert _client(http).base_in_tree("") is None
    assert http.head_urls == []          # short-circuit, no request


def test_base_in_tree_caches_definite_results():
    http = _FakeHttp(head=_Resp(200))
    c = _client(http)
    assert c.base_in_tree("abc") is True
    assert c.base_in_tree("abc") is True
    assert len(http.head_urls) == 1      # second call served from cache


def test_base_in_tree_does_not_cache_none():
    """A network error returns None and is NOT cached, so a later attempt
       can still resolve the commit (the node retries via its backoff)."""
    http = _FakeHttp(head=[httpx.ReadTimeout("t"), _Resp(200)])
    c = _client(http)
    assert c.base_in_tree("abc") is None
    assert c.base_in_tree("abc") is True
    assert len(http.head_urls) == 2


# --- fetch_file_at ---------------------------------------------------------

def test_fetch_file_at_returns_text_on_200():
    http = _FakeHttp(get=_Resp(200, "SCRIPT BODY"))
    c = _client(http)
    assert c.fetch_file_at("scripts/get_maintainer.pl", "abc") == "SCRIPT BODY"
    assert http.get_urls == [
        "https://x/repo.git/plain/scripts/get_maintainer.pl?id=abc"]


def test_fetch_file_at_none_on_404():
    http = _FakeHttp(get=_Resp(404))
    assert _client(http).fetch_file_at("MAINTAINERS", "abc") is None


def test_fetch_file_at_none_on_network_error():
    http = _FakeHttp(get=httpx.ConnectError("boom"))
    assert _client(http).fetch_file_at("MAINTAINERS", "abc") is None


def test_fetch_file_at_caches_success():
    http = _FakeHttp(get=_Resp(200, "BODY"))
    c = _client(http)
    assert c.fetch_file_at("MAINTAINERS", "abc") == "BODY"
    assert c.fetch_file_at("MAINTAINERS", "abc") == "BODY"
    assert len(http.get_urls) == 1


def test_fetch_file_at_none_on_empty_args_skips_http():
    http = _FakeHttp(get=_Resp(200, "B"))
    c = _client(http)
    assert c.fetch_file_at("", "abc") is None
    assert c.fetch_file_at("MAINTAINERS", "") is None
    assert http.get_urls == []


# --- URL shape -------------------------------------------------------------

def test_base_url_trailing_slash_is_stripped():
    http = _FakeHttp(head=_Resp(200))
    cgit.CgitClient("https://x/repo.git/", http=http).base_in_tree("abc")
    assert http.head_urls == ["https://x/repo.git/commit/?id=abc"]


# --- registry: DEFAULT_TREES + parse_trees_env -----------------------------

def test_default_trees_lead_with_linux_next_and_carry_both_urls():
    names = [t.name for t in cgit.DEFAULT_TREES]
    assert names == ["linux-next", "mainline", "stable", "net-next", "tip"]
    ln = cgit.DEFAULT_TREES[0]
    assert "linux-next.git" in ln.cgit_url
    assert ln.cgit_url.startswith("https://")
    assert ln.git_url.startswith("git://")
    assert ln.cgit_url.split("://", 1)[1] == ln.git_url.split("://", 1)[1]


def test_parse_trees_env_empty_returns_defaults():
    assert cgit.parse_trees_env(None) == list(cgit.DEFAULT_TREES)
    assert cgit.parse_trees_env("   ") == list(cgit.DEFAULT_TREES)


def test_parse_trees_env_parses_pairs_in_order_and_derives_git_url():
    spec = cgit.parse_trees_env(
        "linux-next=https://a/next.git,net-next=https://b/net.git")
    assert spec == [
        cgit.Tree("linux-next", "https://a/next.git", "git://a/next.git"),
        cgit.Tree("net-next", "https://b/net.git", "git://b/net.git")]


def test_parse_trees_env_keeps_equals_in_url():
    # partition on the FIRST '=' only — a query-string '=' in the URL
    # survives (cgit URLs are path-based, but be robust anyway).
    spec = cgit.parse_trees_env("t=https://h/r.git?x=y")
    assert spec == [cgit.Tree("t", "https://h/r.git?x=y",
                               "git://h/r.git?x=y")]


def test_parse_trees_env_rejects_malformed_entry():
    with pytest.raises(ValueError, match="expected name=cgit_url"):
        cgit.parse_trees_env("linux-next")          # no '='


# --- KernelTrees.resolve_base: tri-state aggregation -----------------------

class _FakeTreeClient:
    """Minimal stand-in for a CgitClient — KernelTrees only calls
       base_in_tree(). Returns a programmed value and counts calls so we
       can assert short-circuit behaviour."""

    def __init__(self, present):
        self._present = present
        self.calls = 0

    def base_in_tree(self, sha):
        self.calls += 1
        return self._present

    def close(self):
        pass


def _entry(name, present):
    """A (Tree, fake-client) registry entry for resolver tests."""
    return (cgit.Tree(name, f"https://h/{name}.git", f"git://h/{name}.git"),
            _FakeTreeClient(present))


def test_resolve_base_found_short_circuits_at_first_hit():
    a, b, c = _entry("a", True), _entry("b", True), _entry("c", False)
    r = cgit.KernelTrees([a, b, c]).resolve_base("sha")
    assert r.state == cgit.BASE_FOUND
    assert r.tree.name == "a" and r.client is a[1]
    assert a[1].calls == 1 and b[1].calls == 0      # b/c never probed


def test_resolve_base_found_exposes_resolving_tree_git_url():
    r = cgit.KernelTrees([_entry("mainline", True)]).resolve_base("sha")
    assert r.tree.git_url == "git://h/mainline.git"   # for refrepo's hint


def test_resolve_base_falls_through_404_to_next_tree():
    a, b = _entry("a", False), _entry("b", True)
    r = cgit.KernelTrees([a, b]).resolve_base("sha")
    assert r.state == cgit.BASE_FOUND and r.tree.name == "b"
    assert a[1].calls == 1 and b[1].calls == 1


def test_resolve_base_absent_only_when_all_trees_say_404():
    r = cgit.KernelTrees([_entry("a", False),
                           _entry("b", False)]).resolve_base("sha")
    assert r.state == cgit.BASE_ABSENT
    assert r.tree is None and r.client is None


def test_resolve_base_unknown_when_no_hit_and_some_tree_errored():
    """404 from one tree + indeterminate (None) from another must NOT
       conclude absent — the base could live in the unreachable tree."""
    r = cgit.KernelTrees([_entry("a", False),
                           _entry("b", None)]).resolve_base("sha")
    assert r.state == cgit.BASE_UNKNOWN


def test_resolve_base_empty_sha_is_unknown():
    a = _entry("a", True)
    r = cgit.KernelTrees([a]).resolve_base("")
    assert r.state == cgit.BASE_UNKNOWN and a[1].calls == 0


def test_tree_lookup_maps_name_to_registry_entry():
    trees = cgit.KernelTrees([_entry("mainline", True)])
    assert trees.tree("mainline").git_url == "git://h/mainline.git"
    assert trees.tree("nope") is None


def test_resolve_base_caches_found_and_absent_not_unknown():
    found = _entry("a", True)
    kt = cgit.KernelTrees([found])
    kt.resolve_base("s"); kt.resolve_base("s")
    assert found[1].calls == 1                       # FOUND cached

    absent = _entry("a", False)
    kt = cgit.KernelTrees([absent])
    kt.resolve_base("s"); kt.resolve_base("s")
    assert absent[1].calls == 1                      # ABSENT cached

    err = _entry("a", None)
    kt = cgit.KernelTrees([err])
    kt.resolve_base("s"); kt.resolve_base("s")
    assert err[1].calls == 2                          # UNKNOWN re-probed
