#!/usr/bin/env python3
"""linux-arm-msm.py - hone gather module  (kind: human).

The linux-arm-msm kernel mailing list, via its lore.kernel.org public-inbox
git archive. A `human` gather module: the review signal is the reviewer reply
messages humans post on each patch thread. Implements the GatherModule API
(gather_api.py); see ../../SOURCES.md.

The archive lives at $HONE_ARCHIVE_DIR/linux-arm-msm - a full clone (lore's git
server does not honour --filter), kept current with `git fetch` (append-only).
Only messages since START are walked, bounding the work to the human-source
start date. A patchset id IS the thread's root Message-ID, so here
PatchsetRef.id == PatchsetRef.root_message_id.
"""
import email
import os
import re
import subprocess
from email import policy
from email.utils import parsedate_to_datetime, parseaddr

from gather_api import GatherModule, PatchsetRef, Finding, run_cli

START = "2026-01-01"          # human-source start date (see SOURCES.md)
# The public-inbox archive lives under $HONE_ARCHIVE_DIR (hone-core's
# gathered-source store); the repo-root archive/ is the precursor fallback.
_ARCHIVE_DIR = os.environ.get("HONE_ARCHIVE_DIR") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "archive"))
ARCHIVE = os.path.join(_ARCHIVE_DIR, "linux-arm-msm")

PATCH_RE = re.compile(r'\[[^\]]*\bPATCH\b', re.I)
NUM_RE = re.compile(r'\b(\d+)\s*/\s*(\d+)\b')
BASE_RE = re.compile(r'^base-commit:\s*([0-9a-f]{12,40})', re.I | re.M)


def norm_msgid(m):
    return (m or "").replace("<", "").replace(">", "").strip().lower()


def load_messages():
    """Walk the archive since START -> {msgid: msg dict}. Each public-inbox
       commit holds one raw RFC-822 message at path `m`."""
    commits = subprocess.run(
        ["git", "-C", ARCHIVE, "log", "--since=" + START, "--format=%H"],
        capture_output=True).stdout.decode().split()
    if not commits:
        return {}
    spec = b"".join((c + ":m\n").encode() for c in commits)
    data = subprocess.run(["git", "-C", ARCHIVE, "cat-file", "--batch"],
                          input=spec, capture_output=True).stdout
    msgs, i = {}, 0
    while i < len(data):
        nl = data.find(b"\n", i)
        if nl < 0:
            break
        head = data[i:nl].split()
        i = nl + 1
        if len(head) < 3:                      # 'missing' object
            continue
        size = int(head[2])
        raw, i = data[i:i + size], i + size + 1
        try:
            m = email.message_from_bytes(raw, policy=policy.default)
        except Exception:
            continue
        mid = norm_msgid(m.get("Message-ID", ""))
        if not mid:
            continue
        subj = (m.get("Subject", "") or "").replace("\n", " ").strip()
        name, addr = parseaddr(m.get("From", ""))
        refs = [norm_msgid(r) for r in
                re.findall(r'<([^>]+)>', m.get("References", "") or "")]
        irt = re.findall(r'<([^>]+)>', m.get("In-Reply-To", "") or "")
        try:
            sent = int(parsedate_to_datetime(m.get("Date", "")).timestamp())
            date_ok = True
        except Exception:
            sent, date_ok = 0, False     # no resolvable Date -> epoch-0 sentinel
        msgs[mid] = {
            "message_id": mid, "subject": subj, "from_name": name,
            "from_email": addr.lower(), "refs": refs,
            "in_reply_to": norm_msgid(irt[0]) if irt else None,
            "sent": sent, "date_ok": date_ok, "raw": raw,
            # a patch *posting* — has [PATCH...] and is not a reply
            "is_patch": bool(PATCH_RE.search(subj)) and subj[:3].lower() != "re:",
        }
    return msgs


def thread_root(mid, msgs):
    """Walk up the parent chain (In-Reply-To, else nearest ancestor in
       References) to the topmost message. If the chain leaves our window,
       the root id is the out-of-window ancestor (its thread predates START
       and is correctly skipped by callers)."""
    cur, seen = mid, set()
    while cur in msgs and cur not in seen:
        seen.add(cur)
        m = msgs[cur]
        parent = m["in_reply_to"] or (m["refs"][-1] if m["refs"] else None)
        if not parent:
            return cur
        cur = parent
    return cur


def threads(msgs):
    t = {}
    for mid in msgs:
        t.setdefault(thread_root(mid, msgs), []).append(mid)
    return t


def body_text(raw):
    try:
        m = email.message_from_bytes(raw, policy=policy.default)
        b = m.get_body(preferencelist=("plain",))
        return b.get_content() if b else ""
    except Exception:
        return ""


class LinuxArmMsm(GatherModule):
    """linux-arm-msm public-inbox archive — a human review source."""

    name = "linux-arm-msm"
    kind = "human"

    def __init__(self):
        self._msgs = None

    def _messages(self):
        """The archive walk, cached for the lifetime of this instance so a
           list/pull/findings sequence in one process pays for it once."""
        if not os.path.isdir(ARCHIVE):
            raise SystemExit(f"archive missing: {ARCHIVE} — clone it "
                             f"first (see SOURCES.md)")
        if self._msgs is None:
            self._msgs = load_messages()
        return self._msgs

    def list(self):
        msgs = self._messages()
        rows, skip = [], []
        for root, members in threads(msgs).items():
            rm = msgs.get(root)
            if not rm or not rm["is_patch"]:
                continue
            # the root must be a cover letter (0/N) or a single patch (no
            # N/M); a numbered N/M root is a flat series that did not thread
            # to its cover — skip the fragment, don't count it as a patchset.
            nm = NUM_RE.search(rm["subject"])
            if nm and int(nm.group(1)) != 0:
                continue
            replies = [x for x in members
                       if x != root and not msgs[x]["is_patch"]]
            if not replies:                    # no review signal to compare
                continue
            # a root with no resolvable Date has an undeterminable place in
            # the chronological sweep — flag it for the gather phase to
            # skip-flag, not pull.
            if not rm["date_ok"]:
                skip.append((root, rm["subject"]))
                continue
            rows.append((rm["sent"], root, len(replies), rm["subject"]))
        # oldest-first (chronological; Message-ID breaks ties); skip-rows last
        out = [PatchsetRef(id=root, root_message_id=root, subject=subj,
                           sent=sent, n_replies=nrep)
               for sent, root, nrep, subj in sorted(rows)]
        out += [PatchsetRef(id=root, root_message_id=root, subject=subj,
                            skip_reason="unresolved-date")
                for root, subj in sorted(skip)]
        return out

    def pull(self, patchset_id, dest_dir):
        msgs = self._messages()
        os.makedirs(dest_dir, exist_ok=True)
        patches = []
        for mid in threads(msgs).get(norm_msgid(patchset_id), []):
            m = msgs[mid]
            if not m["is_patch"]:
                continue
            nm = NUM_RE.search(m["subject"])
            patches.append((int(nm.group(1)) if nm else 1, m))
        written = []
        for n, m in sorted(patches, key=lambda x: x[0]):
            fn = os.path.join(dest_dir, f"patch{n}.patch")
            with open(fn, "wb") as f:
                f.write(m["raw"])              # pristine archive copy
            written.append(fn)
        return written

    def findings(self, patchset_id):
        msgs = self._messages()
        root = norm_msgid(patchset_id)
        out = []
        for mid in threads(msgs).get(root, []):
            m = msgs[mid]
            if m["is_patch"] or mid == root:
                continue
            out.append(Finding(
                reviewer=m["from_name"], type="human",
                text=body_text(m["raw"]), severity=None,
                reviewer_email=m["from_email"], message_id=mid,
                date=m["sent"], date_ok=m["date_ok"],
                extra={"in_reply_to": m["in_reply_to"]}))
        out.sort(key=lambda f: f.date or 0)
        return out

    def base(self, patchset_id):
        msgs = self._messages()
        rm = msgs.get(norm_msgid(patchset_id))
        if not rm:
            return None
        mt = BASE_RE.search(body_text(rm["raw"]))
        return mt.group(1) if mt else None


if __name__ == "__main__":
    run_cli(LinuxArmMsm())
