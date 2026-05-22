#!/usr/bin/env python3
"""core_db.py - hone dedup ledger & human-reviewer database.

A lightweight SQLite store (stdlib sqlite3, zero dependencies) serving two
roles at once:

  1. Cross-source dedup ledger. Keyed on RFC-5322 Message-IDs so the same
     submission seen via different data sources is counted once. Every table
     recording a message/finding has a PRIMARY KEY or UNIQUE message key, so
     re-ingestion (loop re-runs, `git fetch`, list crossposts) is an
     idempotent no-op.

  2. Human-reviewer tracker. Reviewer identities (one person, many emails),
     review activity, finding accuracy, and a sample-size-gated confidence
     score.

Harness machinery for hone; NOT part of the methodology.

CLI:
  init                        create/upgrade the schema (idempotent)
  seed-mailmap <path>         merge reviewer identities from a kernel .mailmap
  reviewer "<name>" <email>   resolve (create if new) and print a reviewer id
  stats [<reviewer_id>]       activity / accuracy / confidence per reviewer
  extract <root-msgid> <dir>  re-materialize a stored patchset (.tar.zst blob)
"""
import sqlite3, sys, os, time, re, math

# The database file: $HONE_DB when set (containerized hone-core), else the
# repo-root hone.db (the precursor single-host ledger, one level up from core/).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("HONE_DB") or os.path.join(_REPO_ROOT, "hone.db")

SCHEMA = """
-- Every email ingested, from any source. Message-ID is the atomic dedup key
-- (globally unique by RFC 5322). PK => re-ingesting a known message no-ops.
CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,   -- normalized: <> stripped, lowercased
    list         TEXT,
    source       TEXT,
    from_name    TEXT,
    from_email   TEXT,               -- normalized
    subject      TEXT,
    sent         INTEGER,            -- unix time
    in_reply_to  TEXT,
    thread_root  TEXT,               -- message_id of the thread root
    is_patch     INTEGER DEFAULT 0,
    ingested_at  INTEGER
) WITHOUT ROWID;          -- keyed on message_id; no duplicate autoindex

-- A patchset (submission) = a thread; identity = the root Message-ID. This is
-- the cross-source dedup key: a sashiko patchset and a lore thread with the
-- same root_message_id are the SAME submission, reviewed once.
CREATE TABLE IF NOT EXISTS patchsets (
    root_message_id   TEXT PRIMARY KEY,
    subject           TEXT,
    series_version    INTEGER DEFAULT 1,
    change_id         TEXT,          -- b4 Change-Id / X-B4-Tracking; links revisions
    submitter_email   TEXT,
    sent              INTEGER,
    n_patch_files     INTEGER,
    reviewed_at       INTEGER,       -- when OUR loop reviewed it (NULL = not yet)
    reviewed_verdict  TEXT,
    review_tokens     INTEGER,       -- measured cost of our blind review
    review_tool_uses  INTEGER,
    review_duration_ms INTEGER,
    -- The reviewed patchset, saved so a re-evaluation run can re-materialize
    -- it without re-pulling from the source. Format: a .tar.zst archive whose
    -- members are patch0.patch (the cover letter, if the series has one),
    -- patch1.patch, ... patchN.patch. Every member is .patch-formatted text —
    -- a git-am-able patch email (mail headers, commit message, '---',
    -- diffstat, unified diff). Fidelity is source-dependent: linux-arm-msm
    -- members are the pristine original RFC-822 messages from the archive;
    -- sashiko members are our hunk-whitespace-repaired reconstruction (they
    -- apply cleanly but are not byte-identical to the original posting).
    -- NULL until the patchset is reviewed.
    patch_blob        BLOB,
    patch_blob_bytes  INTEGER,
    -- Workflow state — the single status flag driving the staged pipeline
    -- (GATHER -> STAGE -> DISPATCH; see ARCHITECTURE.md):
    --   pulled    gathered; awaiting base staging
    --   staged    base tree located + worktree staged; awaiting a worker
    --   claimed   a worker claimed it for review (claimed_by/_at = the lease)
    --   reviewed  our review recorded
    --   deferred  staging could not locate a base tree — retryable
    --   skip      never to process (e.g. an unresolvable Date)
    -- NULL only transiently, mid-gather.
    status            TEXT,
    pulled_at         INTEGER,       -- unix time the GATHER stage pulled it
    skip_reason       TEXT,          -- why a 'skip' / 'deferred' patchset is parked
    base_commit       TEXT,          -- base commit, resolved by the STAGE stage
    staged_worktree   TEXT,          -- path to the staged base-tree worktree
    claimed_by        TEXT,          -- id of the worker that claimed it
    claimed_at        INTEGER        -- unix time of the claim (the lease stamp)
) WITHOUT ROWID;          -- keyed on root_message_id; no duplicate autoindex
CREATE INDEX IF NOT EXISTS idx_patchsets_changeid ON patchsets(change_id);

-- Per-patch-file metadata. Token cost is measured per patchset review (one
-- holistic session); a per-file cost is only ever DERIVED from this size
-- data, never stored as a measurement.
CREATE TABLE IF NOT EXISTS patch_files (
    root_message_id  TEXT REFERENCES patchsets(root_message_id),
    filename         TEXT,
    lines_changed    INTEGER,
    PRIMARY KEY (root_message_id, filename)
) WITHOUT ROWID;

-- Which data source(s) surfaced a patchset, with that source's native id.
CREATE TABLE IF NOT EXISTS patchset_sources (
    root_message_id  TEXT,
    source           TEXT,
    source_ref       TEXT,
    PRIMARY KEY (root_message_id, source)
) WITHOUT ROWID;

-- A human reviewer: one person, one row, however many emails.
CREATE TABLE IF NOT EXISTS reviewers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT,
    created_at     INTEGER
);

-- A reviewer's email addresses. PK(email) => exact-email is the auto-merge
-- key; an email belongs to exactly one reviewer. via = provenance.
CREATE TABLE IF NOT EXISTS reviewer_emails (
    email       TEXT PRIMARY KEY,    -- normalized
    reviewer_id INTEGER NOT NULL REFERENCES reviewers(id),
    via         TEXT                 -- 'observed' | 'mailmap' | 'manual'
) WITHOUT ROWID;          -- keyed on email; no duplicate autoindex

-- A review act: one message that is a human review reply on a patchset.
-- PK(message_id) => re-ingesting the same reply never double-counts activity.
CREATE TABLE IF NOT EXISTS reviews (
    message_id      TEXT PRIMARY KEY REFERENCES messages(message_id),
    reviewer_id     INTEGER NOT NULL REFERENCES reviewers(id),
    root_message_id TEXT NOT NULL,
    source          TEXT,
    sent            INTEGER
) WITHOUT ROWID;          -- keyed on message_id; no duplicate autoindex
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_id);

-- An individual finding from a review, plus our verdict. UNIQUE => re-extraction
-- is idempotent. verdict NULL = not yet verified against the code.
CREATE TABLE IF NOT EXISTS findings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    review_message_id TEXT REFERENCES reviews(message_id),
    seq               INTEGER,
    severity          TEXT,
    text              TEXT,
    verdict           TEXT,          -- 'match'|'miss'|'source-FP'|NULL
    UNIQUE (review_message_id, seq)
);
"""


def connect():
    """Open hone.db — creating the file and creating/upgrading its schema if
       needed (init() is idempotent), so any entry point gets a ready, current
       database with no separate init step."""
    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys=ON")
    init(db)
    return db


def _add_column(db, table, coldef):
    """Idempotent ALTER TABLE ADD COLUMN — a no-op if the column already
       exists, so init() can upgrade a db created before the column existed."""
    col = coldef.split()[0]
    existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    if col not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")


def _migrate_status(db):
    """One-time data migration: fold the legacy skip_review flag and the
       reviewed_at timestamp into the unified `status` flag, then retire
       skip_review. Guarded — once skip_review is gone this is a no-op."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(patchsets)")}
    if "skip_review" not in cols:
        return
    db.execute("UPDATE patchsets SET status='skip' "
               "WHERE skip_review=1 AND status IS NULL")
    db.execute("UPDATE patchsets SET status='reviewed' "
               "WHERE reviewed_at IS NOT NULL AND status IS NULL")
    db.execute("UPDATE patchsets SET status='pulled' "
               "WHERE patch_blob IS NOT NULL AND status IS NULL")
    db.execute("ALTER TABLE patchsets DROP COLUMN skip_review")


def init(db):
    """Create the schema, and idempotently upgrade an older db — add any
       columns introduced after it was created, then migrate legacy data."""
    db.executescript(SCHEMA)
    for coldef in ("patch_blob BLOB", "patch_blob_bytes INTEGER",
                   "skip_reason TEXT", "status TEXT", "pulled_at INTEGER",
                   "base_commit TEXT", "staged_worktree TEXT",
                   "claimed_by TEXT", "claimed_at INTEGER"):
        _add_column(db, "patchsets", coldef)
    _migrate_status(db)
    db.commit()


# --- normalization (the dedup keys) ----------------------------------------

def norm_email(e):
    e = (e or "").strip().lower()
    if e.startswith("<") and e.endswith(">"):
        e = e[1:-1]
    return e.strip()


def norm_msgid(m):
    m = (m or "").strip()
    if m.startswith("<") and m.endswith(">"):
        m = m[1:-1]
    return m.strip().lower()


# --- reviewer identity ------------------------------------------------------

def resolve_reviewer(db, name, email):
    """Reviewer id for (name,email). Auto-merge ONLY on exact email; never on
       name (distinct people share names). Creates a new reviewer if unknown."""
    email = norm_email(email)
    if not email:
        return None
    row = db.execute("SELECT reviewer_id FROM reviewer_emails WHERE email=?",
                     (email,)).fetchone()
    if row:
        return row[0]
    rid = db.execute("INSERT INTO reviewers(canonical_name, created_at) VALUES(?,?)",
                     (name or email, int(time.time()))).lastrowid
    db.execute("INSERT INTO reviewer_emails(email,reviewer_id,via) VALUES(?,?,?)",
               (email, rid, "observed"))
    db.commit()
    return rid


_MAILMAP_ID = re.compile(r'([^<]*)<([^>]+)>')


def seed_mailmap(db, path):
    """Pre-merge identities from a kernel .mailmap. On each line the first
       'Name <email>' is canonical; every other <email> is the same person."""
    merged = 0
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        ids = [(n.strip(), norm_email(e)) for n, e in _MAILMAP_ID.findall(line)]
        ids = [(n, e) for n, e in ids if e]
        if len(ids) < 2:
            continue            # 1-identity line: nothing to merge
        rid = resolve_reviewer(db, ids[0][0], ids[0][1])
        for _, e in ids[1:]:
            if db.execute("SELECT 1 FROM reviewer_emails WHERE email=?",
                          (e,)).fetchone() is None:
                db.execute("INSERT INTO reviewer_emails(email,reviewer_id,via) "
                           "VALUES(?,?,?)", (e, rid, "mailmap"))
                merged += 1
            # already mapped elsewhere => leave it; a real conflict needs manual review
    db.commit()
    return merged


# --- ingest / record helpers (importable by source modules & the loop) -----

def record_message(db, **m):
    db.execute(
        "INSERT OR IGNORE INTO messages(message_id,list,source,from_name,"
        "from_email,subject,sent,in_reply_to,thread_root,is_patch,ingested_at) "
        "VALUES(:message_id,:list,:source,:from_name,:from_email,:subject,"
        ":sent,:in_reply_to,:thread_root,:is_patch,:ingested_at)",
        {**{"list": None, "source": None, "from_name": None, "from_email": None,
            "subject": None, "sent": None, "in_reply_to": None,
            "thread_root": None, "is_patch": 0,
            "ingested_at": int(time.time())}, **m})


def mark_pulled(db, root_message_id, subject=None):
    """GATHER stage: record that a patchset has been pulled — its blob and
       patch files stored — and is awaiting base staging (status='pulled').
       Creates the patchsets row if absent. The STAGE stage later locates a
       base tree and advances it to 'staged' via mark_staged()."""
    root = norm_msgid(root_message_id)
    db.execute("INSERT OR IGNORE INTO patchsets(root_message_id,subject) "
               "VALUES(?,?)", (root, subject))
    db.execute("UPDATE patchsets SET status='pulled', pulled_at=? "
               "WHERE root_message_id=?", (int(time.time()), root))
    db.commit()


def mark_staged(db, root_message_id, base_commit, staged_worktree):
    """STAGE stage: a linux-tree carrying this patchset's base commit was
       located and a worktree staged at `staged_worktree`. status -> 'staged'
       — the patchset enters the dispatch queue for a worker to claim."""
    db.execute("UPDATE patchsets SET status='staged', base_commit=?, "
               "staged_worktree=? WHERE root_message_id=?",
               (base_commit, staged_worktree, norm_msgid(root_message_id)))
    db.commit()


def mark_deferred(db, root_message_id, reason):
    """STAGE stage: no linux-tree carrying this patchset's base commit could
       be located. status -> 'deferred' — retryable (a later STAGE run
       reconsiders it, unlike terminal 'skip'); `reason` goes to skip_reason."""
    db.execute("UPDATE patchsets SET status='deferred', skip_reason=? "
               "WHERE root_message_id=?",
               (reason, norm_msgid(root_message_id)))
    db.commit()


def mark_reviewed(db, root_message_id, verdict,
                  tokens=None, tool_uses=None, duration_ms=None):
    """DISPATCH stage: record OUR blind review of a (claimed) patchset with
       its measured token cost, and advance status -> 'reviewed'. One row per
       patchset — no double-count across sources."""
    db.execute(
        "UPDATE patchsets SET status='reviewed', reviewed_at=?, reviewed_verdict=?,"
        " review_tokens=?, review_tool_uses=?, review_duration_ms=?"
        " WHERE root_message_id=?",
        (int(time.time()), verdict, tokens, tool_uses, duration_ms,
         norm_msgid(root_message_id)))
    db.commit()


def already_reviewed(db, root_message_id):
    r = db.execute("SELECT status FROM patchsets WHERE root_message_id=?",
                   (norm_msgid(root_message_id),)).fetchone()
    return bool(r and r[0] == "reviewed")


def is_handled(db, root_message_id):
    """GATHER-phase dedup gate: True once a patchset carries any workflow
       status — 'pulled', 'reviewed', or 'skip'. The gather phase must not
       re-pull a handled patchset. (status NULL = a crashed/partial gather;
       deliberately NOT handled, so it is retried on the next gather run.)"""
    r = db.execute("SELECT status FROM patchsets WHERE root_message_id=?",
                   (norm_msgid(root_message_id),)).fetchone()
    return bool(r and r[0])


def stage_queue(db):
    """STAGE-stage queue: gathered patchsets awaiting base staging — status
       'pulled', plus 'deferred' ones to retry — oldest submission first."""
    return [r[0] for r in db.execute(
        "SELECT root_message_id FROM patchsets "
        "WHERE status IN ('pulled','deferred') ORDER BY sent, root_message_id")]


def staged_patchsets(db):
    """DISPATCH-stage queue (read-only snapshot): patchsets staged and awaiting
       a worker (status='staged'), oldest first. Workers take work via
       claim_patchset() — this is just the queue view."""
    return [r[0] for r in db.execute(
        "SELECT root_message_id FROM patchsets WHERE status='staged' "
        "ORDER BY sent, root_message_id")]


def claim_patchset(db, worker_id, lease_seconds=1800):
    """DISPATCH stage — the durable work-claim. Atomically claim the oldest
       dispatchable patchset for a worker: one with status 'staged', or a
       'claimed' one whose lease has expired (its worker died). Sets status
       'claimed' with claimed_by / claimed_at. Returns a dict
       {root_message_id, base_commit, staged_worktree, subject}, or None if the
       dispatch queue is empty. SQLite serialises writers, so two workers can
       never claim the same patchset; a crashed worker's claim is reclaimed
       once `lease_seconds` elapse."""
    now = int(time.time())
    row = db.execute(
        "UPDATE patchsets SET status='claimed', claimed_by=?, claimed_at=? "
        "WHERE root_message_id=("
        " SELECT root_message_id FROM patchsets"
        " WHERE status='staged' OR (status='claimed' AND claimed_at<=?)"
        " ORDER BY sent, root_message_id LIMIT 1)"
        " RETURNING root_message_id, base_commit, staged_worktree, subject",
        (worker_id, now, now - lease_seconds)).fetchone()
    db.commit()
    if not row:
        return None
    return {"root_message_id": row[0], "base_commit": row[1],
            "staged_worktree": row[2], "subject": row[3]}


def mark_skip(db, root_message_id, reason, subject=None):
    """Flag a patchset the loop must SKIP — never pull, never review
       (status='skip'). e.g. no resolvable Date on the thread root, so its
       place in the chronological sweep is undeterminable; skip_reason records
       why. Creates the patchsets row if absent; `sent` stays NULL by design —
       that undeterminable date is the whole reason for the skip."""
    root = norm_msgid(root_message_id)
    db.execute("INSERT OR IGNORE INTO patchsets(root_message_id,subject) "
               "VALUES(?,?)", (root, subject))
    db.execute("UPDATE patchsets SET status='skip', skip_reason=? "
               "WHERE root_message_id=?", (reason, root))
    db.commit()


def upsert_patchset(db, root_message_id, subject=None, series_version=1,
                    change_id=None, submitter_email=None, sent=None,
                    n_patch_files=None):
    db.execute(
        "INSERT OR IGNORE INTO patchsets(root_message_id,subject,series_version,"
        "change_id,submitter_email,sent,n_patch_files) VALUES(?,?,?,?,?,?,?)",
        (norm_msgid(root_message_id), subject, series_version, change_id,
         norm_email(submitter_email) if submitter_email else None,
         sent, n_patch_files))
    db.commit()


def record_patchset_source(db, root_message_id, source, source_ref):
    db.execute(
        "INSERT OR IGNORE INTO patchset_sources(root_message_id,source,source_ref)"
        " VALUES(?,?,?)", (norm_msgid(root_message_id), source, str(source_ref)))
    db.commit()


def record_patch_files(db, root_message_id, files):
    """files: iterable of (filename, lines_changed). Also refreshes the
       denormalized patchsets.n_patch_files count from the patch_files table."""
    root = norm_msgid(root_message_id)
    for fn, lines in files:
        db.execute("INSERT OR IGNORE INTO patch_files(root_message_id,filename,"
                   "lines_changed) VALUES(?,?,?)", (root, fn, lines))
    db.execute("UPDATE patchsets SET n_patch_files="
               "(SELECT COUNT(*) FROM patch_files WHERE root_message_id=?) "
               "WHERE root_message_id=?", (root, root))
    db.commit()


def record_review(db, message_id, reviewer_id, root_message_id,
                  source=None, sent=None):
    db.execute(
        "INSERT OR IGNORE INTO reviews(message_id,reviewer_id,root_message_id,"
        "source,sent) VALUES(?,?,?,?,?)",
        (norm_msgid(message_id), reviewer_id, norm_msgid(root_message_id),
         source, sent))
    db.commit()


def record_finding(db, review_message_id, seq, severity, text, verdict=None):
    """A classified human-reviewer finding. INSERT OR REPLACE on
       (review_message_id, seq) => re-classification is idempotent."""
    db.execute(
        "INSERT OR REPLACE INTO findings(review_message_id,seq,severity,text,"
        "verdict) VALUES(?,?,?,?,?)",
        (norm_msgid(review_message_id), seq, severity, text, verdict))
    db.commit()


# --- patchset blob storage (re-evaluation without re-pulling) --------------

def store_patch_blob(db, root_message_id, patch_dir):
    """Save the patchset's patch files (patch0.patch..patchN.patch in
       patch_dir) as a single .tar.zst blob on the patchset row, so a later
       re-evaluation run can re-materialize the patchset without re-pulling
       from its source. Returns the compressed byte size (0 if no files)."""
    import glob, subprocess, tempfile
    files = sorted(os.path.basename(f)
                   for f in glob.glob(os.path.join(patch_dir, "patch*.patch")))
    if not files:
        return 0
    fd, tmp = tempfile.mkstemp(suffix=".tar.zst")
    os.close(fd)
    try:
        subprocess.run(["tar", "--zstd", "-cf", tmp, "-C", patch_dir, *files],
                       check=True, capture_output=True)
        with open(tmp, "rb") as f:
            blob = f.read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    db.execute("UPDATE patchsets SET patch_blob=?, patch_blob_bytes=? "
               "WHERE root_message_id=?",
               (blob, len(blob), norm_msgid(root_message_id)))
    db.commit()
    return len(blob)


def extract_patch_blob(db, root_message_id, dest_dir):
    """Re-materialize a stored patchset into dest_dir/patchN.patch — for a
       re-evaluation run, with no source pull. Returns True if a blob was
       stored and extracted, False if the patchset has no blob."""
    import subprocess, tempfile
    row = db.execute("SELECT patch_blob FROM patchsets WHERE root_message_id=?",
                      (norm_msgid(root_message_id),)).fetchone()
    if not row or row[0] is None:
        return False
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".tar.zst")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(row[0])
        subprocess.run(["tar", "--zstd", "-xf", tmp, "-C", dest_dir],
                       check=True, capture_output=True)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return True


# --- stats / confidence -----------------------------------------------------

def wilson_lower(k, n, z=1.96):
    """95% Wilson score lower bound on k/n. Sample-size-gated by construction:
       2/2 does not outrank 95/100. This is the reviewer confidence score."""
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


def reviewer_stats(db, rid):
    name = db.execute("SELECT canonical_name FROM reviewers WHERE id=?",
                      (rid,)).fetchone()
    if not name:
        return None
    emails = [r[0] for r in db.execute(
        "SELECT email FROM reviewer_emails WHERE reviewer_id=?", (rid,))]
    nrev = db.execute("SELECT COUNT(*) FROM reviews WHERE reviewer_id=?",
                      (rid,)).fetchone()[0]
    lo, hi = db.execute("SELECT MIN(sent),MAX(sent) FROM reviews WHERE reviewer_id=?",
                        (rid,)).fetchone()
    months = max(1.0, (hi - lo) / (30 * 86400.0)) if lo and hi else 1.0
    verified = db.execute(
        "SELECT COUNT(*) FROM findings f JOIN reviews r "
        "ON f.review_message_id=r.message_id "
        "WHERE r.reviewer_id=? AND f.verdict IS NOT NULL", (rid,)).fetchone()[0]
    real = db.execute(
        "SELECT COUNT(*) FROM findings f JOIN reviews r "
        "ON f.review_message_id=r.message_id "
        "WHERE r.reviewer_id=? AND f.verdict IN ('match','miss')",
        (rid,)).fetchone()[0]
    return {
        "id": rid, "name": name[0], "emails": emails,
        "activity_reviews": nrev,
        "activity_per_month": round(nrev / months, 2),
        "findings_verified": verified, "findings_real": real,
        "accuracy": round(real / verified, 3) if verified else None,
        "confidence": round(wilson_lower(real, verified), 3),
        "confidence_note": "Wilson 95% lower bound; ~0 until enough verified findings",
    }


def main():
    a = sys.argv
    if len(a) < 2:
        print(__doc__); return
    db = connect()
    if a[1] == "init":
        init(db); print("schema ready:", DB)
    elif a[1] == "seed-mailmap":
        n = seed_mailmap(db, a[2])
        print(f"seeded {n} alias email(s) from {a[2]}")
    elif a[1] == "reviewer":
        print("reviewer id:", resolve_reviewer(db, a[2], a[3]))
    elif a[1] == "stats":
        import json
        if len(a) > 2:
            print(json.dumps(reviewer_stats(db, int(a[2])), indent=2))
        else:
            rows = db.execute("SELECT id FROM reviewers ORDER BY id").fetchall()
            print(f"{len(rows)} reviewer(s). Pass an id for detail.")
    elif a[1] == "extract":
        print("extracted" if extract_patch_blob(db, a[2], a[3])
              else "no blob stored for that patchset")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
