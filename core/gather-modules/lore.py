#!/usr/bin/env python3
"""lore.py - hone gather module for lore.kernel.org public-inbox archives.

Walks a configured public-inbox git archive in commit (= arrival) order and
yields refs for every new patchset, patch message, and review comment. The
cursor is the last git commit SHA processed, so the same module finds both
new patchsets and late review comments on old patchsets — exactly what makes
lore the canonical source of truth.

Operator setup: clone a public-inbox archive into HONE_ARCHIVE_DIR/lore.
The shortest path is the bundled CLI helper, which reads URL + since_date
from this module so there's one canonical place for the cutoff:

    python3 core/gather-modules/lore.py clone

That runs a `--shallow-since=<since_date> --filter=blob:none` partial clone
of `HONE_LORE_URL` (default `https://lore.kernel.org/all/0`) into
`HONE_ARCHIVE_DIR/lore`. `--shallow-since` bounds the download to messages
posted on or after the floor — a few hundred MB for a recent floor,
instead of multi-GB for the full archive history.

Set `HONE_LORE_AUTOCLONE=1` to have hone-core kick off the same clone in
the background on first start (the service comes up clean and the lore
gather is paused until the clone finishes).

Lowering `since_date` later means re-cloning (or
`git fetch --shallow-since=<earlier-date>` to deepen): the shallow
boundary, like the gather cursor, is forward-only.
"""
import email
import logging
import os
import re
import subprocess
import sys
from email import policy
from email.utils import parsedate_to_datetime, parseaddr

from gather_api import (GatherModule, PatchsetRef, MessageRef,
                        run_cli)

log = logging.getLogger("hone.gather.lore")

# The public-inbox archive lives under $HONE_ARCHIVE_DIR (hone-core's
# gathered-source store); the repo-root archive/ is the precursor fallback.
_ARCHIVE_DIR = (os.environ.get("HONE_ARCHIVE_DIR")
                or os.path.normpath(os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "archive")))
ARCHIVE = os.path.join(_ARCHIVE_DIR, "lore")

# Upstream archive URL — overridable so a deployment can point at a single
# list (e.g. https://lore.kernel.org/linux-arm-msm/0) instead of all-of-lore.
DEFAULT_URL = "https://lore.kernel.org/all/0"

# Per-cycle patchset cap. A cold-start cycle could otherwise be months of
# all-of-lore in one pass — blocking the source's slot for hours, brushing
# the supervisor's 30-min stall cancel, and dumping a huge spike of
# work-items into the queue all at once. Capping on PATCHSETS (not commits)
# guarantees each cycle stops on a clean boundary instead of mid-series:
# the cap fires when we've already emitted N full patchsets and the next
# patchset is about to start, so patchsets land whole. The framework
# persists the cursor as it goes; the next supervisor tick picks up the
# next batch. Incremental catch-up across many ordinary cycles.
MAX_PATCHSETS_PER_CYCLE = 200

# Safety bound on the cold-start boundary skip (below). If a cold-start
# slice's leading commits are mid-series (the cover predates since_date),
# `list()` skips them until it finds a real cover or standalone patch.
# This caps that search so a pathological misaligned start can never spin
# forever — at the limit we give up and accept the next commit as a
# best-effort boundary, logging a warning.
_MAX_BOUNDARY_SKIP = 10000

# messages.type small-int values — must match core_db's MSG_TYPE_*.
# Duplicated here to keep this module loadable without core_db on sys.path
# (the gather-modules dir lives outside the core package).
_TYPE_COVER, _TYPE_PATCH, _TYPE_COMMENT = 1, 2, 3

_PATCH_RE = re.compile(r'\[[^\]]*\bPATCH\b[^\]]*\]', re.I)
_NUM_RE   = re.compile(r'\b(\d+)\s*/\s*(\d+)\b')
_BASE_RE  = re.compile(r'^base-commit:\s*([0-9a-f]{12,40})', re.I | re.M)
_MSGID_RE = re.compile(r'<([^>]+)>')


def _norm_msgid(m):
    return (m or "").replace("<", "").replace(">", "").strip().lower()


def _norm_email(e):
    return (e or "").strip().lower()


def classify(subject):
    """Classify a message by its Subject. Returns (type, part_index):

        (TYPE_COVER,    0)      [PATCH 0/M]
        (TYPE_PATCH,    N)      [PATCH N/M] with N > 0
        (TYPE_PATCH,    None)   [PATCH ...] with no N/M (single patch)
        (TYPE_COMMENT,  None)   anything else (replies, list mail)"""
    s = (subject or "").lstrip()
    if s[:3].lower() == "re:":          # any reply is a comment, regardless
        return (_TYPE_COMMENT, None)    # of whether its quoted subject has
                                        # `[PATCH ...]` in it
    if not _PATCH_RE.search(s):
        return (_TYPE_COMMENT, None)
    nm = _NUM_RE.search(s)
    if nm is None:
        return (_TYPE_PATCH, None)      # `[PATCH ...]` without N/M
    n = int(nm.group(1))
    if n == 0:
        return (_TYPE_COVER, 0)
    return (_TYPE_PATCH, n)


def _series_total(subject):
    """Extract M from a `[PATCH N/M]` Subject, or None."""
    nm = _NUM_RE.search(subject or "")
    return int(nm.group(2)) if nm else None


def _extract_base(raw):
    """The `base-commit:` trailer in a patch body, or None."""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    m = _BASE_RE.search(text)
    return m.group(1) if m else None


def parse_message(raw):
    """Parse the relevant headers out of a raw RFC-822 message blob. Returns
       a dict, or None on parse failure / missing Message-ID."""
    try:
        m = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return None
    msgid = _norm_msgid(m.get("Message-ID", ""))
    if not msgid:
        return None
    subj = (m.get("Subject", "") or "").replace("\n", " ").strip()
    name, addr = parseaddr(m.get("From", ""))
    irt = _MSGID_RE.findall(m.get("In-Reply-To", "") or "")
    refs = _MSGID_RE.findall(m.get("References", "") or "")
    list_ids = []
    for value in m.get_all("List-Id") or []:
        list_ids += _MSGID_RE.findall(value)
    try:
        sent = int(parsedate_to_datetime(m.get("Date", "")).timestamp())
        date_ok = True
    except Exception:
        sent, date_ok = 0, False
    return {
        "message_id":   msgid,
        "subject":      subj,
        "author_name":  name or "",
        "author_email": _norm_email(addr),
        "in_reply_to":  _norm_msgid(irt[0]) if irt else None,
        "references":   [_norm_msgid(r) for r in refs],
        "list_tags":    [_norm_msgid(t) for t in list_ids],
        "sent":         sent if date_ok else None,
        "date_ok":      date_ok,
        "raw":          raw,
    }


def resolve_root(db, msg, thread_cache):
    """Find a message's (parent_message_id, root_message_id) by walking its
       In-Reply-To / References chain — the in-cycle thread_cache first, then
       the corpus's `messages` table for cross-cycle threading. Returns
       (None, msgid) if no known parent exists (the message starts a new
       thread)."""
    candidates = []
    if msg["in_reply_to"]:
        candidates.append(msg["in_reply_to"])
    candidates += list(reversed(msg["references"]))    # newest-first
    for parent in candidates:
        if parent in thread_cache:
            return parent, thread_cache[parent]
        if db is not None:
            row = db.execute(
                "SELECT root_message_id FROM messages WHERE message_id=?",
                (parent,)).fetchone()
            if row:
                return parent, row["root_message_id"]
    return None, msg["message_id"]


# git's progress emit, e.g.:
#   "Enumerating objects: 152318, done."
#   "Counting objects: 100% (152318/152318), done."
#   "Receiving objects:  47% (71590/152318), 273.41 MiB | 8.43 MiB/s"
#   "Resolving deltas:  41% (60221/146790)"
# The dynamic lines (Receiving / Resolving) write with \r; the once-and-done
# ones use \n. We split on either so the reader picks up updates promptly.
_PROGRESS_RE = re.compile(
    r"(Enumerating objects|Counting objects|Compressing objects|"
    r"Receiving objects|Resolving deltas|Updating files)"
    r":\s*(\d+)%?")


def _stream_progress(proc, on_progress=None):
    """Read `proc.stderr` chunk-by-chunk, split on \\r and \\n, parse each
       git progress line, echo it through, and call `on_progress(phase,
       percent, line)` per parsed update. Pure I/O; runs in the calling
       thread until the subprocess closes stderr."""
    buf = b""
    while True:
        chunk = proc.stderr.read(256)
        if not chunk:
            if buf:
                _emit_line(buf, on_progress)
            return
        buf += chunk
        # split on both \r and \n so dynamic progress updates flush
        while True:
            i = -1
            for sep in (b"\r", b"\n"):
                j = buf.find(sep)
                if j != -1 and (i == -1 or j < i):
                    i = j
            if i == -1:
                break
            line, buf = buf[:i], buf[i + 1:]
            _emit_line(line, on_progress)


def _emit_line(raw, on_progress):
    """Echo one line through to stderr (so the terminal / docker-logs view
       stays live) and, if it parses as a git progress line, hand the
       (phase, percent, text) update to `on_progress`."""
    line = raw.decode("utf-8", "replace").rstrip()
    if not line:
        return
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
    if on_progress is None:
        return
    m = _PROGRESS_RE.search(line)
    if m is None:
        return
    try:
        on_progress(m.group(1), int(m.group(2)), line)
    except Exception:                        # never let a UI bug kill the clone
        log.exception("progress callback raised")


class Lore(GatherModule):
    """lore.kernel.org public-inbox archive — the canonical source for
       kernel patchsets and the follow-up review comments on them.

       One pass walks every commit (i.e. message) added since the cursor,
       parses each, classifies it (cover|patch|comment), threads it to its
       patchset, and yields a PatchsetRef (when the message creates a new
       patchset) and a MessageRef for the message itself."""

    name = "lore"
    since_date = "2026-03-08"           # cold-start floor
    archive_dir = ARCHIVE               # the on-disk public-inbox clone; let
                                        # callers (core/main.py status / UI)
                                        # read it without reaching into module
                                        # globals.

    @classmethod
    def clone(cls, target=ARCHIVE, *, url=None, since_date=None,
              progress=None):
        """Clone the lore archive into `target` — a `--shallow-since` +
           `--filter=blob:none` partial clone bounded by `since_date`
           (default `cls.since_date`, the cold-start floor). `url` defaults
           to `$HONE_LORE_URL` then `DEFAULT_URL`. Idempotent: a no-op if
           `target` already looks like a git checkout (returns False);
           returns True on a real clone. Raises subprocess.CalledProcessError
           if git fails.

           `--progress` is forced so git emits its dynamic progress lines
           even when stderr isn't a TTY — for the background autoclone path
           where the alternative is silent minutes in `docker logs`. If
           `progress` is given, it is called with each parsed
           `(phase, percent, line)` update from git's stderr; the
           background autoclone uses it to refresh the Settings-page status
           panel (see core/main.py `_autoclone_lore`). Every line is also
           echoed to stderr so the terminal / docker-logs view stays live.

           Run interactively (`python3 core/gather-modules/lore.py clone`)
           or from `HONE_LORE_AUTOCLONE`'s background-clone path."""
        if os.path.isdir(os.path.join(target, ".git")) \
                or os.path.isdir(os.path.join(target, "objects")):
            log.info("archive already present at %s - clone skipped", target)
            return False
        url = url or os.environ.get("HONE_LORE_URL") or DEFAULT_URL
        since = since_date or cls.since_date
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        cmd = ["git", "clone",
               "--progress",                # force progress on non-TTY stderr
               "--filter=blob:none",
               f"--shallow-since={since}",
               "--no-tags", "--single-branch",
               url, target]
        log.info("cloning %s into %s (since %s; partial + shallow)",
                 url, target, since)
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            bufsize=0)                       # unbuffered so \r updates flush
        try:
            _stream_progress(proc, progress)
        finally:
            rc = proc.wait()
        if rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
        log.info("clone complete at %s", target)
        return True

    def list(self, state=None, db=None):
        # The archive is operator-provisioned (clone-it-first; see SOURCES.md).
        # When it's missing — first-run, or this hone-core deployment doesn't
        # use lore — degrade to a clean no-op cycle: log it and yield nothing
        # so the rest of hone-core (the web UI, the other sources) keeps
        # running. The operator clones the archive in and the next tick picks
        # it up without a restart.
        if not os.path.isdir(ARCHIVE):
            log.warning("archive missing at %s - clone it first "
                        "(see SOURCES.md); gather is a no-op for lore "
                        "until the archive exists", ARCHIVE)
            return
        commits = self._new_commits(state.cursor if state else None)
        thread_cache = {}                  # message_id -> root_message_id
        patchsets_emitted = 0
        # Cold-start boundary skip: when the cursor is empty AND the
        # since_date floor lands mid-series (cover predates the floor), the
        # first commits in the slice would otherwise be patches whose root
        # `resolve_root` can't find — they'd each be emitted as a
        # one-patch ghost patchset with the wrong root, and patches 4..N
        # of the real series would scatter into more ghosts. Skip forward
        # to the first commit that clearly introduces a patchset (a cover
        # or a standalone single-patch) so the cycle starts clean.
        awaiting_boundary = not (state and state.cursor)
        skipped = 0
        for sha in commits:
            blob = self._blob(sha)
            if not blob:
                continue
            msg = parse_message(blob)
            if msg is None:
                continue
            mtype, part_index = classify(msg["subject"])
            parent, root = resolve_root(db, msg, thread_cache)
            clean_boundary = (mtype == _TYPE_COVER or
                              (mtype == _TYPE_PATCH and part_index is None))
            if awaiting_boundary:
                if not clean_boundary:
                    skipped += 1
                    if skipped >= _MAX_BOUNDARY_SKIP:
                        log.warning("cold-start boundary not found in %d "
                                    "commits — accepting the next commit "
                                    "as a best-effort patchset start; some "
                                    "leading patches may end up wrong-rooted",
                                    skipped)
                        awaiting_boundary = False
                    continue
                awaiting_boundary = False
                if skipped:
                    log.info("cold-start: skipped %d leading commits to land "
                             "on a clean patchset boundary", skipped)
            # Count patchset *introductions* against the cap; stop BEFORE
            # the (cap+1)th so patchsets 1..cap land whole and the next
            # cycle picks up the next introduction.
            introduces_patchset = (mtype != _TYPE_COMMENT
                                   and root == msg["message_id"])
            if introduces_patchset:
                if patchsets_emitted >= MAX_PATCHSETS_PER_CYCLE:
                    log.info("gathered %d patchsets this cycle (cap), the "
                             "rest on later cycles", MAX_PATCHSETS_PER_CYCLE)
                    return
                patchsets_emitted += 1
            # Only cache messages we're committing to emit AND ingest —
            # otherwise a later reply could resolve to a parent that was
            # never written to `messages`, tripping the FK. This covers
            # both the boundary-skip case (a leading discussion-thread
            # root that isn't a patchset) and the orphan-comment case
            # (a comment whose parent isn't anywhere — `_build_refs`
            # returns without yielding).
            will_emit = (mtype != _TYPE_COMMENT) or (parent is not None)
            if will_emit:
                thread_cache[msg["message_id"]] = root
                yield from self._build_refs(msg, sha, mtype, part_index,
                                            parent, root)

    def _build_refs(self, msg, sha, mtype, part_index, parent, root):
        """Yield the PatchsetRef (when this message introduces the patchset)
           and the MessageRef for the message itself. Comments yield only
           the MessageRef and are skipped when no parent can be resolved
           (orphan list mail). The classification + threading happens in
           `list()` because the patchset-boundary check needs them too."""
        if mtype == _TYPE_COMMENT:
            if parent is None:
                return                              # orphan — skip
            yield MessageRef(
                message_id=msg["message_id"], root_message_id=root,
                type=_TYPE_COMMENT, parent_message_id=parent,
                author_name=msg["author_name"],
                author_email=msg["author_email"],
                subject=msg["subject"], sent=msg["sent"],
                body=msg["raw"].decode("utf-8", "replace"),
                cursor=sha)
            return
        # Patch or cover. If this message is its own root, it introduces a
        # patchset — emit a PatchsetRef first.
        if root == msg["message_id"]:
            yield PatchsetRef(
                root_message_id=root,
                subject=msg["subject"],
                submitter_email=msg["author_email"],
                sent=msg["sent"],
                n_patches=_series_total(msg["subject"]) or 1,
                base_commit=_extract_base(msg["raw"]),
                list_tags=msg["list_tags"],
                skip_reason=("unresolved-date"
                             if not msg["date_ok"] else None),
                cursor=sha)
        yield MessageRef(
            message_id=msg["message_id"], root_message_id=root,
            type=mtype, part_index=part_index,
            author_name=msg["author_name"],
            author_email=msg["author_email"],
            subject=msg["subject"], sent=msg["sent"],
            body=msg["raw"].decode("utf-8", "replace"),
            cursor=sha)

    def _new_commits(self, cursor):
        """Git commits to process, oldest-first. `cursor` (a SHA) bounds
           the left; without it, falls back to --since=`since_date`. The
           per-cycle cap is applied in `list()` on PATCHSET boundaries, not
           here on commits — see MAX_PATCHSETS_PER_CYCLE."""
        args = ["log", "--reverse", "--format=%H"]
        if cursor and cursor.strip():
            args.append(f"{cursor.strip()}..HEAD")
        elif self.since_date:
            args.append(f"--since={self.since_date}")
        result = subprocess.run(["git", "-C", ARCHIVE, *args],
                                capture_output=True, check=False)
        return result.stdout.decode().split() if result.returncode == 0 else []

    def _blob(self, sha):
        """The message blob at path `m` in commit `sha`. public-inbox stores
           one raw RFC-822 message per commit at that path."""
        result = subprocess.run(
            ["git", "-C", ARCHIVE, "show", f"{sha}:m"],
            capture_output=True, check=False)
        return result.stdout if result.returncode == 0 else None

if __name__ == "__main__":
    # `clone` is a lore-specific verb on top of the shared `list` shim:
    # the lore archive is operator-provisioned, so we ship the helper here
    # to keep URL + since_date in one place.
    if len(sys.argv) >= 2 and sys.argv[1] == "clone":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        Lore.clone()
    else:
        run_cli(Lore())
