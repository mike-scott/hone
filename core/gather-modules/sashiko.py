#!/usr/bin/env python3
"""sashiko.py - hone gather module  (kind: ai).

sashiko-bot (sashiko.dev) is an AI kernel-patch review bot. An `ai` gather
module: the review signal is sashiko's machine-generated findings. Implements
the GatherModule API (gather_api.py); see ../../SOURCES.md.

sashiko identifies a patchset by its own integer id (PatchsetRef.id); the
patchset's root Message-ID (PatchsetRef.root_message_id, the hone.db dedup key)
is the part-0 patch's Message-ID.
"""
import json
import os
import time
import urllib.parse
import urllib.request

from gather_api import GatherModule, PatchsetRef, Finding, run_cli

API = "https://sashiko.dev/api"


def _get(path):
    with urllib.request.urlopen(API + path, timeout=30) as f:
        return json.load(f)


def _repair_diff(text):
    """Inside @@ hunks, an empty line is a context line that lost its space."""
    out, in_hunk, ol, nl = [], False, 0, 0
    for ln in text.split("\n"):
        if ln.startswith("@@ "):
            try:
                op, npp = ln.split("@@")[1].strip().split(" ")

                def c(p):
                    p = p[1:]
                    return int(p.split(",")[1]) if "," in p else 1
                ol, nl, in_hunk = c(op), c(npp), True
            except Exception:
                in_hunk = False
            out.append(ln)
            continue
        if in_hunk:
            if ln == "":
                out.append(" "); ol -= 1; nl -= 1
            elif ln[0] == " ":
                out.append(ln); ol -= 1; nl -= 1
            elif ln[0] == "-":
                out.append(ln); ol -= 1
            elif ln[0] == "+":
                out.append(ln); nl -= 1
            elif ln[0] == "\\":
                out.append(ln)
            else:
                in_hunk = False; out.append(ln)
            if in_hunk and ol <= 0 and nl <= 0:
                in_hunk = False
            continue
        out.append(ln)
    return "\n".join(out)


class Sashiko(GatherModule):
    """sashiko-bot (sashiko.dev) — an AI review source."""

    name = "sashiko"
    kind = "ai"

    # -- helpers --------------------------------------------------------------

    def _patchset(self, pid):
        return _get(f"/patchset?id={pid}")

    def _review_obj(self, pid):
        """A patchset can carry several review objects; reviews[0] is not
           always the real one (seen on 23046). Pick the review with the most
           findings, then a non-empty inline_review, then the latest."""
        rv = self._patchset(pid).get("reviews") or []
        if not rv:
            return None

        def score(r):
            n = 0
            out = r.get("output")
            if out:
                try:
                    n = len(json.loads(out).get("findings") or [])
                except Exception:
                    n = 0
            return (n, 1 if (r.get("inline_review") or "").strip() else 0,
                    r.get("created_at") or 0)

        return max(rv, key=score)

    def _root_msgid(self, pid):
        """The part-0 patch's Message-ID — the patchset's dedup key."""
        patches = sorted(self._patchset(pid).get("patches", []),
                         key=lambda x: x.get("part_index", 0))
        return patches[0].get("message_id", "") if patches else ""

    # -- GatherModule API -----------------------------------------------------

    def list(self):
        out = []
        for r in _get("/patchsets").get("items", []):
            if r.get("status") != "Reviewed":
                continue
            sev = ((r.get("findings_critical") or 0)
                   + (r.get("findings_high") or 0)
                   + (r.get("findings_medium") or 0))
            if sev <= 0:
                continue
            pid = r["id"]
            # root Message-ID: from the list record if present, else one
            # extra /patchset fetch (the hone.db dedup key must be known
            # before the gather phase decides whether to pull).
            rmid = (r.get("message_id") or r.get("root_message_id")
                    or self._root_msgid(pid))
            out.append(PatchsetRef(
                id=str(pid),
                root_message_id=(rmid or "").strip("<>").lower(),
                subject=r.get("subject", ""), sent=r.get("date"),
                extra={"severity_counts": {
                    "critical": r.get("findings_critical"),
                    "high": r.get("findings_high"),
                    "medium": r.get("findings_medium"),
                    "low": r.get("findings_low")}}))
        return out

    def pull(self, patchset_id, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        ps = self._patchset(int(patchset_id))
        written = []
        for i, p in enumerate(sorted(ps.get("patches", []),
                                     key=lambda x: x.get("part_index", 0))):
            m = _get("/message?id="
                     + urllib.parse.quote(p["message_id"], safe=""))
            body = _repair_diff(m.get("body") or "")
            datestr = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                                    time.gmtime(m.get("date") or 0))
            mbox = ("From %s Mon Sep 17 00:00:00 2001\n" % ("0" * 40)
                    + "From: %s\n" % m.get("author", "")
                    + "Date: %s\n" % datestr
                    + "Subject: %s\n\n%s" % (m.get("subject", ""), body))
            if not mbox.endswith("\n"):
                mbox += "\n"
            fn = os.path.join(dest_dir,
                              "patch%d.patch" % p.get("part_index", i))
            with open(fn, "w") as f:
                f.write(mbox)
            written.append(fn)
        return written

    def findings(self, patchset_id):
        rv = self._review_obj(int(patchset_id))
        out = rv.get("output") if rv else None
        if not out:
            return []
        try:
            raw = json.loads(out).get("findings") or []
        except Exception:
            return []
        items = []
        for fd in raw:
            if not isinstance(fd, dict):
                fd = {"text": str(fd)}
            items.append(Finding(
                reviewer="sashiko", type="ai",
                text=(fd.get("message") or fd.get("description")
                      or fd.get("text") or json.dumps(fd)),
                severity=fd.get("severity"),
                preexisting=bool(fd.get("preexisting")
                                 or fd.get("pre_existing")),
                extra=fd))                     # full raw finding, nothing lost
        return items

    def base(self, patchset_id):
        rv = self._review_obj(int(patchset_id))
        b = (rv or {}).get("baseline") or {}
        if isinstance(b, str):
            return b or None
        return (b.get("commit") or b.get("sha")
                or b.get("base_commit") or b.get("baseline")) or None

    def inline(self, patchset_id):
        rv = self._review_obj(int(patchset_id))
        return (rv.get("inline_review") or None) if rv else None


if __name__ == "__main__":
    run_cli(Sashiko())
