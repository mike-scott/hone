"""upload.py — parse user-uploaded patch series for the web UI.

Accepts the artifacts `git format-patch` produces — one RFC-822 message
per patch (.patch / .eml), an optional 0/N cover letter — either as
individual files or concatenated into a single mbox (optionally
gzipped: lore.kernel.org's t.mbox.gz drops straight in), plus a bare
pasted diff as the no-tooling fallback. Produces a structured series preview
the upload page renders for confirmation; on confirm the same structure
drives ingest through the gather primitives (core/ui.py →
upload_confirm).

Self-contained on stdlib `email`. The Subject / series / trailer
conventions deliberately mirror core/gather-modules/lore.py (classify,
series totals, the `base-commit:` trailer) — an upload is the same
artifact lore gathers, minus the mailing-list threading.
"""
import email
import gzip
import io
import re
import uuid
from email import policy
from email.utils import parseaddr, parsedate_to_datetime

_PATCH_RE   = re.compile(r'\[[^\]]*\bPATCH\b[^\]]*\]', re.I)
_NUM_RE     = re.compile(r'\b(\d+)\s*/\s*(\d+)\b')
_VERSION_RE = re.compile(r'\[[^\]]*\bv(\d+)\b[^\]]*\bPATCH\b[^\]]*\]'
                         r'|\[[^\]]*\bPATCH\b[^\]]*\bv(\d+)\b[^\]]*\]', re.I)
_BASE_RE    = re.compile(r'^base-commit:\s*([0-9a-f]{12,40})', re.I | re.M)
# b4 / gerrit revision-tracking trailer — when present it's the precise
# signal that two uploads are iterations of the same work.
_CHANGEID_RE = re.compile(r'^Change-Id:\s*(\S+)', re.I | re.M)
# Leading [bracket] tags on a Subject — [PATCH v3 1/2], [RFC PATCH], …
_TAGS_RE    = re.compile(r'^\s*(\[[^\]]*\]\s*)+')
# A bare diff (no mail headers): what `git diff` prints, or a unified
# diff's ---/+++ headers near the top.
_DIFF_RE    = re.compile(r'^(diff --git |--- |Index: )', re.M)

# Per-message and per-upload caps — patch series are text; multi-megabyte
# uploads are mistakes (a tarball, a kernel image) we reject before the
# email parser sees them.
MAX_UPLOAD_BYTES  = 8 * 1024 * 1024
MAX_MESSAGES      = 65


def _synth_msgid():
    return f"<upload-{uuid.uuid4().hex}@hone>"


def _split_mbox(raw):
    """Split an mbox blob into per-message blobs on the `From ` separator
       lines. A blob that doesn't start with `From ` is one message."""
    if not raw.startswith(b"From "):
        return [raw]
    out, current = [], []
    for line in raw.splitlines(keepends=True):
        if line.startswith(b"From ") and current:
            out.append(b"".join(current))
            current = []
        current.append(line)
    if current:
        out.append(b"".join(current))
    # Drop the `From ` envelope line itself; the email parser wants the
    # RFC-822 headers that follow it.
    return [b.split(b"\n", 1)[1] if b.startswith(b"From ") and b"\n" in b
            else b for b in out]


def _looks_like_email(raw):
    """RFC-822-ish: a Subject: or From: header before the first blank
       line. A bare diff has neither."""
    head = raw[:4096]
    for line in head.splitlines():
        if not line.strip():
            return False
        if line[:8].lower() in (b"subject:", ) or \
           line[:5].lower() == b"from:":
            return True
    return False


def _classify(subject):
    """(kind, part, total) from a Subject — kind ∈ 'cover' | 'patch' |
       'other'. Mirrors lore.classify + _series_total."""
    s = (subject or "").lstrip()
    if s[:3].lower() == "re:" or not _PATCH_RE.search(s):
        return ("other", None, None)
    nm = _NUM_RE.search(s)
    if nm is None:
        return ("patch", None, None)
    n, m = int(nm.group(1)), int(nm.group(2))
    return ("cover", 0, m) if n == 0 else ("patch", n, m)


def _series_version(subject):
    m = _VERSION_RE.search(subject or "")
    if not m:
        return 1
    return int(m.group(1) or m.group(2))


def series_title(subject):
    """The iteration-matching key: the Subject with every leading
       [bracket] tag stripped, case-folded — so `[PATCH v2 0/3] net: x`,
       `[PATCH v3 0/3] net: x` and `[PATCH 0/3] net: x` all read as the
       same series. Iterating uploaders rarely bump the v marker (that's
       the point of pre-list review), so the title is the stable part."""
    return _TAGS_RE.sub("", subject or "").strip().lower()


def find_prior_iteration(candidates, *, subject, change_id=None):
    """The candidate this upload looks like a new iteration of, or None.
       `candidates` are the developer's un-superseded chain heads,
       newest first (core_db.unsuperseded_user_series). A matching b4 Change-Id
       wins outright (the precise signal); otherwise the first candidate
       with the same series title. Heuristic — the preview offers the
       link as an opt-out, never silently."""
    if change_id:
        for c in candidates:
            if c.get("change_id") == change_id:
                return c
    title = series_title(subject)
    if not title:
        return None
    for c in candidates:
        if series_title(c.get("subject")) == title:
            return c
    return None


def _body_text(msg):
    """The plain-text body of a parsed email message. format-patch mail
       is single-part text/plain; fall back to the first text part."""
    if msg.is_multipart():
        part = msg.get_body(preferencelist=("plain",))
        return part.get_content() if part is not None else ""
    try:
        return msg.get_content()
    except Exception:
        payload = msg.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if payload else ""


def _parse_email(raw, warnings):
    """One RFC-822 blob → a message dict, or None if unparseable."""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return None
    subject = (msg.get("Subject", "") or "").replace("\n", " ").strip()
    if not subject and not msg.get("From"):
        return None
    msgid = (msg.get("Message-ID", "") or "").strip()
    if not msgid:
        msgid = _synth_msgid()
        warnings.append(
            f"{subject or '(no subject)'!s}: no Message-ID header — "
            f"a synthetic one was generated")
    name, addr = parseaddr(msg.get("From", "") or "")
    try:
        sent = int(parsedate_to_datetime(msg.get("Date", "")).timestamp())
    except Exception:
        sent = None
    return {"message_id":   msgid,
            "subject":      subject,
            "author_name":  name or "",
            "author_email": (addr or "").strip().lower(),
            "sent":         sent,
            "body":         _body_text(msg)}


def parse_upload(blobs, *, pasted=None):
    """Parse an upload into a series preview.

       `blobs` is [(filename, bytes), …] from the multi-file input;
       `pasted` is the textarea's content (a bare diff, one patch email,
       or a whole mbox). Returns a dict:

         ok          — False when `errors` is non-empty; nothing ingests
         errors      — fatal problems (nothing parsed, holes in the series)
         warnings    — ingestable but flagged (no base-commit trailer,
                       ignored replies, synthetic Message-IDs)
         root_message_id · subject · submitter_* · sent · n_patches ·
         base_commit · change_id · series_version · cover · patches

       `cover` is None or a message dict; `patches` are message dicts in
       series order, each carrying `part_index` (None for a standalone
       single patch). The shape feeds both the preview template and the
       confirm-time ingest."""
    errors, warnings = [], []
    total_bytes = sum(len(b) for _, b in blobs) + len(pasted or "")
    if total_bytes > MAX_UPLOAD_BYTES:
        return {"ok": False, "errors":
                [f"upload exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MiB "
                 f"cap"], "warnings": []}

    # Transparently decompress gzipped members — lore.kernel.org serves
    # thread mboxes as t.mbox.gz, so they should drop straight in. The
    # read is bounded to the cap + 1 byte: a crafted bomb can't expand
    # past it, and anything that does is rejected on its DECOMPRESSED
    # size (the cap is about parseable text, not wire bytes).
    expanded = []
    for fname, data in blobs:
        if data[:2] == b"\x1f\x8b":                  # gzip magic
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                    data = gz.read(MAX_UPLOAD_BYTES + 1)
            except (OSError, EOFError):
                errors.append(f"{fname}: not a valid gzip file")
                continue
        expanded.append((fname, data))
    blobs = expanded
    total_bytes = sum(len(b) for _, b in blobs) + len(pasted or "")
    if total_bytes > MAX_UPLOAD_BYTES:
        return {"ok": False, "errors":
                [f"upload exceeds the {MAX_UPLOAD_BYTES // (1024*1024)} MiB "
                 f"cap once decompressed"], "warnings": []}

    raw_messages = []                       # (origin_label, bytes)
    for fname, data in blobs:
        for piece in _split_mbox(data):
            raw_messages.append((fname, piece))
    if pasted and pasted.strip():
        data = pasted.encode("utf-8")
        if _looks_like_email(data) or data.startswith(b"From "):
            for piece in _split_mbox(data):
                raw_messages.append(("pasted text", piece))
        elif _DIFF_RE.search(pasted):
            # A bare diff: wrap it as a synthetic single-patch message.
            raw_messages.append(("pasted diff", None))
        else:
            errors.append("pasted text is neither a patch email, an mbox, "
                          "nor a recognisable diff")
    if len(raw_messages) > MAX_MESSAGES:
        return {"ok": False, "errors":
                [f"upload contains {len(raw_messages)} messages — the cap "
                 f"is {MAX_MESSAGES}"], "warnings": []}

    cover, patches, ignored = None, [], 0
    for label, piece in raw_messages:
        if piece is None:                   # the pasted bare diff
            patches.append({"message_id":   _synth_msgid(),
                            "subject":      "[PATCH] pasted diff",
                            "author_name":  "",
                            "author_email": "",
                            "sent":         None,
                            "body":         pasted,
                            "kind":         "patch",
                            "part":         None,
                            "total":        None})
            warnings.append("pasted diff has no mail headers — subject and "
                            "submitter are placeholders")
            continue
        m = _parse_email(piece, warnings)
        if m is None:
            errors.append(f"{label}: not parseable as a patch email")
            continue
        kind, part, total = _classify(m["subject"])
        m.update(kind=kind, part=part, total=total)
        if kind == "other":
            ignored += 1                    # replies in a thread mbox
        elif kind == "cover":
            if cover is not None:
                errors.append("more than one cover letter (two 0/N "
                              "subjects)")
            cover = m
        else:
            patches.append(m)
    if ignored:
        warnings.append(f"{ignored} non-patch message"
                        f"{'' if ignored == 1 else 's'} (replies / list "
                        f"mail) ignored")

    if not patches and not errors:
        errors.append("no patches found in the upload")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    # Series ordering + completeness. Numbered patches sort by N; a
    # single un-numbered patch stands alone; mixing the two is an error.
    numbered = [p for p in patches if p["part"] is not None]
    if numbered and len(numbered) != len(patches):
        errors.append("mix of numbered ([PATCH N/M]) and un-numbered "
                      "([PATCH]) patches")
    elif numbered:
        patches.sort(key=lambda p: p["part"])
        totals = {p["total"] for p in patches} | (
            {cover["total"]} if cover else set())
        total = max(totals)
        if len(totals) > 1:
            errors.append(f"inconsistent series totals in subjects: "
                          f"{sorted(totals)}")
        seen = [p["part"] for p in patches]
        expect = list(range(1, total + 1))
        if seen != expect:
            missing = sorted(set(expect) - set(seen))
            dupes = sorted({n for n in seen if seen.count(n) > 1})
            if missing:
                errors.append(f"incomplete series: missing patch"
                              f"{'' if len(missing) == 1 else 'es'} "
                              f"{', '.join(f'{n}/{total}' for n in missing)}")
            if dupes:
                errors.append(f"duplicate patch number"
                              f"{'' if len(dupes) == 1 else 's'}: "
                              f"{dupes}")
    elif len(patches) > 1:
        errors.append("multiple un-numbered [PATCH] messages — a series "
                      "needs [PATCH N/M] subjects (git format-patch "
                      "produces them)")
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    # The root: the cover letter when there is one, else the (single or
    # first) patch — the same identity rule the lore threading resolves to.
    head = cover or patches[0]
    base, change_id = None, None
    for m in ([cover] if cover else []) + patches:
        body = m["body"] or ""
        if base is None and (found := _BASE_RE.search(body)):
            base = found.group(1)
        if change_id is None and (found := _CHANGEID_RE.search(body)):
            change_id = found.group(1)
        if base and change_id:
            break
    if base is None:
        warnings.append("no base-commit trailer — prepare and review will "
                        "run in heuristic mode (generate the series with "
                        "`git format-patch --base=<commit>`)")
    for p in patches:
        p["part_index"] = p["part"] if numbered else None

    return {
        "ok":               True,
        "errors":           [],
        "warnings":         warnings,
        "root_message_id":  head["message_id"],
        "subject":          head["subject"],
        "submitter_name":   head["author_name"],
        "submitter_email":  head["author_email"],
        "sent":             head["sent"],
        "n_patches":        len(patches),
        "base_commit":      base,
        "change_id":        change_id,
        "series_version":   _series_version(head["subject"]),
        "cover":            cover,
        "patches":          patches,
    }
