#!/usr/bin/env python3
"""core_db.py - the hone-core database: schema, migrations, and data layer.

A SQLite store (WAL mode) holding the system's three data tiers:

  1. corpus       patchsets gathered from the data sources, their patch
                  archives, and the external review signal on them - gathered
                  once, shared by every client.
  2. methodology  the versioned methodology document, the candidate practices
                  on trial (with their self-honing counters), and the
                  merge-gate proposal queue.
  3. results      each client's reviews of patchsets, and the claim queue
                  that hands that work to nodes.

Schema changes are versioned: PRAGMA user_version records the file's schema
version and connect() applies any newer migration. Harness machinery; NOT the
methodology itself.

Stdlib only, except bootstrap_methodology() which needs pyyaml + jsonschema.

CLI:
  init                        create / upgrade the schema (idempotent)
  bootstrap <meth> [<schema>]  import a methodology YAML as version 1
  seed-mailmap <path>         merge reviewer identities from a kernel .mailmap
  stats                       row counts, per table
"""
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid

# The database file: $HONE_DB when set (containerized hone-core), else the
# repo-root hone.db. core/ is one level below the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("HONE_DB") or os.path.join(_REPO_ROOT, "hone.db")


# ===========================================================================
# Schema  -  migration 1 creates the whole multi-tenant schema
# ===========================================================================

_SCHEMA_V1 = """
-- ---- Tier 1: global corpus ------------------------------------------------

-- A gathered patchset = a thread, identified by its root Message-ID. That id
-- is the cross-source dedup key: the same submission seen via two sources is
-- one row. `state` is the corpus state, distinct from any client's review.
CREATE TABLE patchsets (
    root_message_id  TEXT PRIMARY KEY,
    subject          TEXT,
    series_version   INTEGER NOT NULL DEFAULT 1,
    change_id        TEXT,                -- b4 Change-Id; links revisions
    submitter_email  TEXT,
    source           TEXT,                -- gather module that surfaced it
    sent             INTEGER,             -- unix time
    n_patches        INTEGER,
    base_commit      TEXT,                -- declared base, recorded at GATHER
    origin           TEXT NOT NULL DEFAULT 'gathered',  -- 'gathered' | 'manual'
    state            TEXT NOT NULL DEFAULT 'gathered',  -- 'gathered' | 'skipped'
    skip_reason      TEXT,
    gathered_at      INTEGER
) WITHOUT ROWID;
CREATE INDEX idx_patchsets_changeid ON patchsets(change_id);

-- The patchset's patch archive (a .tar.zst of patch0.patch..patchN.patch),
-- a row apart so patchset queries do not drag the blob along.
CREATE TABLE patch_blobs (
    root_message_id  TEXT PRIMARY KEY REFERENCES patchsets(root_message_id),
    blob             BLOB NOT NULL,
    n_bytes          INTEGER NOT NULL,
    format           TEXT NOT NULL DEFAULT 'tar.zst'
) WITHOUT ROWID;

-- Which data source(s) surfaced a patchset, with that source's native id.
CREATE TABLE patchset_sources (
    root_message_id  TEXT NOT NULL REFERENCES patchsets(root_message_id),
    source           TEXT NOT NULL,
    source_ref       TEXT,
    PRIMARY KEY (root_message_id, source)
) WITHOUT ROWID;

-- The external review signal on a patchset - an AI bot's findings, or a human
-- reviewer's reply. This is the 'source review' a node fetches for comparison
-- AFTER its own blind review. `ref` is the per-source dedup token (a reply
-- Message-ID for human findings, an ordinal for AI findings), so re-ingestion
-- is idempotent.
CREATE TABLE source_findings (
    id               INTEGER PRIMARY KEY,
    root_message_id  TEXT NOT NULL REFERENCES patchsets(root_message_id),
    source           TEXT NOT NULL,
    ref              TEXT NOT NULL,
    kind             TEXT,                -- 'ai' | 'human'
    reviewer         TEXT,
    reviewer_email   TEXT,
    text             TEXT,
    severity         TEXT,
    preexisting      INTEGER NOT NULL DEFAULT 0,
    sent             INTEGER,
    UNIQUE (root_message_id, source, ref)
);
CREATE INDEX idx_source_findings_root ON source_findings(root_message_id);

-- A human reviewer: one person, one row, however many emails.
CREATE TABLE reviewers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name  TEXT,
    created_at      INTEGER
);

-- A reviewer's email addresses. PK(email) => exact-email is the merge key;
-- an email belongs to exactly one reviewer.
CREATE TABLE reviewer_emails (
    email        TEXT PRIMARY KEY,        -- normalized
    reviewer_id  INTEGER NOT NULL REFERENCES reviewers(id),
    via          TEXT                     -- 'observed' | 'mailmap' | 'manual'
) WITHOUT ROWID;

-- ---- Tier 2: global methodology -------------------------------------------

-- The methodology, versioned. The whole document is stored as JSON: it is
-- versioned, fetched and distilled wholesale, so relational decomposition
-- would buy nothing. Exactly one row is 'active' at a time.
CREATE TABLE methodology_versions (
    version     INTEGER PRIMARY KEY,
    document    TEXT NOT NULL,            -- JSON: the full methodology
    state       TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'superseded'
    note        TEXT,
    created_at  INTEGER
);

-- Candidate practices on trial. Relational, not folded into the document,
-- because self-honing mutates the counters far more often than the
-- methodology versions change: applied = times a node applied the candidate,
-- catches = times it caught something, confidence = the sample-size-gated
-- score (see SCORING.md).
CREATE TABLE methodology_candidates (
    id             TEXT PRIMARY KEY,      -- slug
    description    TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'trial',  -- trial|graduated|pruned
    applied_count  INTEGER NOT NULL DEFAULT 0,
    catches_count  INTEGER NOT NULL DEFAULT 0,
    confidence     REAL NOT NULL DEFAULT 0.0,
    origin         TEXT,
    created_at     INTEGER,
    updated_at     INTEGER
) WITHOUT ROWID;

-- The merge-gate queue: methodology changes a maintenance task has proposed,
-- awaiting a human decision (see ARCHITECTURE.md -> The merge gate).
CREATE TABLE methodology_proposals (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,         -- graduate | prune-redundant |
                                          -- prune-ineffective | consolidate |
                                          -- revise
    payload        TEXT NOT NULL,         -- JSON
    state          TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted |
                                          -- deferred | rejected | returned
    redraft_count  INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    created_at     INTEGER,
    decided_at     INTEGER,
    decided_by     TEXT
);

-- ---- Tier 3: per-client review results ------------------------------------

-- A tenant. The client key authenticates a node as belonging to this client.
CREATE TABLE clients (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key  TEXT NOT NULL UNIQUE,
    name        TEXT,
    state       TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'disabled'
    created_at  INTEGER
);

-- One client's review of one patchset - the per-tenant work item AND the
-- claim queue. State lifecycle:
--   claimable -> claimed -> { reviewed | unappliable | deferred }
-- claimable: enqueued, free to take. claimed: a worker holds a lease. The
-- three terminal states are the node's verdict (see API.md).
CREATE TABLE reviews (
    client_id            INTEGER NOT NULL REFERENCES clients(id),
    root_message_id      TEXT NOT NULL REFERENCES patchsets(root_message_id),
    state                TEXT NOT NULL DEFAULT 'claimable',
    -- the claim / lease (set while state = 'claimed')
    claim_id             TEXT,            -- unique id of the live claim
    claimed_by           TEXT,            -- worker id
    claimed_at           INTEGER,
    lease_expires        INTEGER,         -- past this, the claim is reclaimable
    heartbeat_at         INTEGER,
    -- completion
    methodology_version  INTEGER REFERENCES methodology_versions(version),
    record               TEXT,            -- JSON: the review completion record
    completed_at         INTEGER,
    enqueued_at          INTEGER,
    PRIMARY KEY (client_id, root_message_id)
) WITHOUT ROWID;
CREATE INDEX idx_reviews_claimable ON reviews(client_id, state, enqueued_at);
CREATE INDEX idx_reviews_claim ON reviews(claim_id);

-- The maintenance-task claim queue: holistic candidate evaluation, or a
-- redraft of a returned proposal. Same claim/lease shape as reviews.
CREATE TABLE maintenance_tasks (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,         -- 'holistic' | 'redraft'
    payload        TEXT,                  -- JSON
    state          TEXT NOT NULL DEFAULT 'claimable',
    claim_id       TEXT,
    claimed_by     TEXT,
    claimed_at     INTEGER,
    lease_expires  INTEGER,
    heartbeat_at   INTEGER,
    result         TEXT,                  -- JSON
    completed_at   INTEGER,
    created_at     INTEGER
);
CREATE INDEX idx_maint_claimable ON maintenance_tasks(state, created_at);
"""

# index i (0-based) => schema version i+1. Append a new migration to upgrade.
_MIGRATIONS = [_SCHEMA_V1]


def connect(path=None):
    """Open the database (creating the file if absent), apply any pending
       schema migrations, and return the connection.

       check_same_thread is off so one connection can be shared by the
       lifespan and, for now, the route handlers; once the handlers do real
       concurrent work the right model is a per-request connection or a pool.
       SQLite's WAL mode gives concurrent readers + a serialized writer."""
    db = sqlite3.connect(path or DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    migrate(db)
    return db


def migrate(db):
    """Apply every migration newer than the file's PRAGMA user_version, in
       order. Idempotent: a current database is left untouched. Returns the
       resulting schema version."""
    have = db.execute("PRAGMA user_version").fetchone()[0]
    for version, ddl in enumerate(_MIGRATIONS, start=1):
        if version > have:
            db.executescript(ddl)
            db.execute(f"PRAGMA user_version={version}")
            db.commit()
    return db.execute("PRAGMA user_version").fetchone()[0]


# ===========================================================================
# Normalization - the dedup keys
# ===========================================================================

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


def _new_claim_id():
    """A claim id unique across the reviews and maintenance_tasks queues."""
    return uuid.uuid4().hex


# ===========================================================================
# Tier 1 - corpus
# ===========================================================================

def upsert_patchset(db, root_message_id, *, subject=None, series_version=1,
                    change_id=None, submitter_email=None, source=None,
                    sent=None, n_patches=None, base_commit=None,
                    origin="gathered"):
    """Insert a gathered patchset into the corpus (idempotent on the root id);
       refresh the mutable fields if it is already known. Does not touch
       `state` / `skip_reason` - a re-gather never un-skips a patchset.
       Returns the normalized root_message_id."""
    root = norm_msgid(root_message_id)
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,series_version,"
        "change_id,submitter_email,source,sent,n_patches,base_commit,origin,"
        "state,gathered_at) VALUES (?,?,?,?,?,?,?,?,?,?,'gathered',?) "
        "ON CONFLICT(root_message_id) DO UPDATE SET "
        "subject=excluded.subject, series_version=excluded.series_version, "
        "change_id=excluded.change_id, "
        "submitter_email=excluded.submitter_email, source=excluded.source, "
        "sent=excluded.sent, n_patches=excluded.n_patches, "
        "base_commit=excluded.base_commit",
        (root, subject, series_version, change_id,
         norm_email(submitter_email) if submitter_email else None,
         source, sent, n_patches, base_commit, origin, int(time.time())))
    db.commit()
    return root


def get_patchset(db, root_message_id):
    """The patchset row as a dict, or None."""
    row = db.execute("SELECT * FROM patchsets WHERE root_message_id=?",
                     (norm_msgid(root_message_id),)).fetchone()
    return dict(row) if row else None


def record_patchset_source(db, root_message_id, source, source_ref):
    """Note that `source` surfaced this patchset, with its native id."""
    db.execute("INSERT OR IGNORE INTO patchset_sources "
               "(root_message_id,source,source_ref) VALUES (?,?,?)",
               (norm_msgid(root_message_id), source, str(source_ref)))
    db.commit()


def set_patch_blob(db, root_message_id, blob, fmt="tar.zst"):
    """Store (or replace) the patchset's patch archive. Returns the byte size."""
    db.execute("INSERT INTO patch_blobs (root_message_id,blob,n_bytes,format) "
               "VALUES (?,?,?,?) ON CONFLICT(root_message_id) DO UPDATE SET "
               "blob=excluded.blob, n_bytes=excluded.n_bytes, "
               "format=excluded.format",
               (norm_msgid(root_message_id), blob, len(blob), fmt))
    db.commit()
    return len(blob)


def get_patch_blob(db, root_message_id):
    """The patchset's patch archive as bytes, or None if none is stored."""
    row = db.execute("SELECT blob FROM patch_blobs WHERE root_message_id=?",
                     (norm_msgid(root_message_id),)).fetchone()
    return row["blob"] if row else None


def record_source_finding(db, root_message_id, source, ref, *, kind=None,
                          reviewer=None, reviewer_email=None, text=None,
                          severity=None, preexisting=False, sent=None):
    """Record one external-review item. Idempotent on (patchset, source, ref)."""
    db.execute(
        "INSERT OR IGNORE INTO source_findings (root_message_id,source,ref,"
        "kind,reviewer,reviewer_email,text,severity,preexisting,sent) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (norm_msgid(root_message_id), source, str(ref), kind, reviewer,
         norm_email(reviewer_email) if reviewer_email else None, text,
         severity, int(bool(preexisting)), sent))
    db.commit()


def source_findings(db, root_message_id):
    """Every external-review item on a patchset, as a list of dicts."""
    return [dict(r) for r in db.execute(
        "SELECT * FROM source_findings WHERE root_message_id=? ORDER BY id",
        (norm_msgid(root_message_id),))]


def mark_skipped(db, root_message_id, reason, subject=None):
    """Flag a patchset the corpus must skip (e.g. an unresolvable Date) -
       never blob-stored, never fanned out for review. Creates the row if
       absent."""
    root = norm_msgid(root_message_id)
    db.execute("INSERT OR IGNORE INTO patchsets (root_message_id,subject) "
               "VALUES (?,?)", (root, subject))
    db.execute("UPDATE patchsets SET state='skipped', skip_reason=? "
               "WHERE root_message_id=?", (reason, root))
    db.commit()


def is_handled(db, root_message_id):
    """GATHER dedup gate: True once the corpus already has this patchset, in
       any state. The gather phase must not re-pull a handled patchset."""
    return db.execute("SELECT 1 FROM patchsets WHERE root_message_id=?",
                      (norm_msgid(root_message_id),)).fetchone() is not None


# ===========================================================================
# Reviewers - the human-reviewer identity tracker
# ===========================================================================

def resolve_reviewer(db, name, email):
    """Reviewer id for (name,email). Auto-merge ONLY on exact email - never on
       name (distinct people share names). Creates a reviewer if unknown."""
    email = norm_email(email)
    if not email:
        return None
    row = db.execute("SELECT reviewer_id FROM reviewer_emails WHERE email=?",
                     (email,)).fetchone()
    if row:
        return row["reviewer_id"]
    rid = db.execute("INSERT INTO reviewers (canonical_name,created_at) "
                     "VALUES (?,?)", (name or email, int(time.time()))).lastrowid
    db.execute("INSERT INTO reviewer_emails (email,reviewer_id,via) "
               "VALUES (?,?,'observed')", (email, rid))
    db.commit()
    return rid


_MAILMAP_ID = re.compile(r'([^<]*)<([^>]+)>')


def seed_mailmap(db, path):
    """Pre-merge identities from a kernel .mailmap: on each line the first
       'Name <email>' is canonical, every other <email> is the same person.
       Returns the number of alias emails merged."""
    merged = 0
    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.split("#", 1)[0].strip()
        ids = [(n.strip(), norm_email(e)) for n, e in _MAILMAP_ID.findall(line)]
        ids = [(n, e) for n, e in ids if e]
        if len(ids) < 2:
            continue                       # 1-identity line: nothing to merge
        rid = resolve_reviewer(db, ids[0][0], ids[0][1])
        for _, e in ids[1:]:
            if db.execute("SELECT 1 FROM reviewer_emails WHERE email=?",
                          (e,)).fetchone() is None:
                db.execute("INSERT INTO reviewer_emails (email,reviewer_id,via)"
                           " VALUES (?,?,'mailmap')", (e, rid))
                merged += 1
    db.commit()
    return merged


def wilson_lower(k, n, z=1.96):
    """95% Wilson score lower bound on k/n - sample-size-gated by construction
       (2/2 does not outrank 95/100). The confidence score (see SCORING.md)."""
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom)


# ===========================================================================
# Tier 3 - clients
# ===========================================================================

def register_client(db, client_key, name=None):
    """Pre-authorize a client key (the admin POST /v1/clients). Idempotent on
       the key, and fans the existing corpus out to the client. Returns the id."""
    db.execute("INSERT OR IGNORE INTO clients (client_key,name,created_at) "
               "VALUES (?,?,?)", (client_key, name, int(time.time())))
    db.commit()
    cid = db.execute("SELECT id FROM clients WHERE client_key=?",
                     (client_key,)).fetchone()["id"]
    enqueue_reviews_for_client(db, cid)
    return cid


def get_client(db, client_key):
    """The client row for a key, as a dict, or None. Disabled clients are
       included - the caller checks `state`."""
    row = db.execute("SELECT * FROM clients WHERE client_key=?",
                     (client_key,)).fetchone()
    return dict(row) if row else None


def list_clients(db):
    return [dict(r) for r in db.execute("SELECT * FROM clients ORDER BY id")]


# ===========================================================================
# Tier 3 - the review queue and the claim protocol
# ===========================================================================

def enqueue_reviews_for_patchset(db, root_message_id):
    """Fan a gathered patchset out to every active client as a claimable
       review (idempotent; a skipped patchset is not fanned out). Returns the
       number of rows created."""
    root = norm_msgid(root_message_id)
    ps = db.execute("SELECT state FROM patchsets WHERE root_message_id=?",
                    (root,)).fetchone()
    if not ps or ps["state"] != "gathered":
        return 0
    now = int(time.time())
    client_ids = [r["id"] for r in db.execute(
        "SELECT id FROM clients WHERE state='active'")]
    n = 0
    for cid in client_ids:
        n += db.execute("INSERT OR IGNORE INTO reviews "
                        "(client_id,root_message_id,state,enqueued_at) "
                        "VALUES (?,?,'claimable',?)",
                        (cid, root, now)).rowcount
    db.commit()
    return n


def enqueue_reviews_for_client(db, client_id):
    """Give a (newly registered) client a claimable review for every gathered
       patchset already in the corpus. Returns the number of rows created."""
    now = int(time.time())
    roots = [r["root_message_id"] for r in db.execute(
        "SELECT root_message_id FROM patchsets WHERE state='gathered'")]
    n = 0
    for root in roots:
        n += db.execute("INSERT OR IGNORE INTO reviews "
                        "(client_id,root_message_id,state,enqueued_at) "
                        "VALUES (?,?,'claimable',?)",
                        (client_id, root, now)).rowcount
    db.commit()
    return n


def claim_review(db, client_id, worker_id, lease_seconds=1800):
    """Atomically claim the oldest reviewable patchset for a client: one that
       is 'claimable', or a 'claimed' one whose lease has expired (its worker
       died). Marks it 'claimed' under a fresh claim_id + lease. Returns a dict
       {claim_id, client_id, root_message_id}, or None if the queue is empty.

       SQLite serializes writers, so two workers cannot claim the same row; a
       crashed worker's claim is reclaimed once its lease elapses."""
    now = int(time.time())
    claim_id = _new_claim_id()
    row = db.execute(
        "UPDATE reviews SET state='claimed', claim_id=?, claimed_by=?, "
        "claimed_at=?, lease_expires=?, heartbeat_at=? "
        "WHERE client_id=? AND root_message_id=("
        "  SELECT root_message_id FROM reviews "
        "  WHERE client_id=? AND (state='claimable' "
        "        OR (state='claimed' AND lease_expires<=?)) "
        "  ORDER BY enqueued_at, root_message_id LIMIT 1) "
        "RETURNING claim_id, client_id, root_message_id",
        (claim_id, worker_id, now, now + lease_seconds, now,
         client_id, client_id, now)).fetchone()
    db.commit()
    return dict(row) if row else None


def heartbeat(db, claim_id, lease_seconds=1800):
    """Extend a live claim's lease (works for a review or a maintenance
       claim). Returns True if the claim is still valid, False if it has
       lapsed or completed - the worker should then stop and re-claim."""
    now = int(time.time())
    for table in ("reviews", "maintenance_tasks"):
        cur = db.execute(
            f"UPDATE {table} SET lease_expires=?, heartbeat_at=? "
            f"WHERE claim_id=? AND state='claimed'",
            (now + lease_seconds, now, claim_id))
        if cur.rowcount:
            db.commit()
            return True
    db.commit()
    return False


def complete_review(db, claim_id, state, record, methodology_version=None):
    """Record a node's verdict on a claimed review and close it out. `state`
       is 'reviewed' | 'unappliable' | 'deferred'; `record` (a dict) is stored
       as JSON. Returns:
         'ok'      recorded (or a no-op re-submit of the same claim)
         'lapsed'  the claim was reclaimed - the node must discard the result"""
    if state not in ("reviewed", "unappliable", "deferred"):
        raise ValueError(f"bad review state: {state!r}")
    row = db.execute("SELECT state FROM reviews WHERE claim_id=?",
                     (claim_id,)).fetchone()
    if row is None:
        return "lapsed"                    # reclaimed (or never issued)
    if row["state"] != "claimed":
        return "ok"                        # already recorded - idempotent no-op
    db.execute("UPDATE reviews SET state=?, record=?, methodology_version=?, "
               "completed_at=? WHERE claim_id=?",
               (state, json.dumps(record), methodology_version,
                int(time.time()), claim_id))
    db.commit()
    return "ok"


def reclaim_expired(db):
    """Crash recovery: return lease-expired claims to their queues. Returns
       (reviews_reclaimed, maintenance_reclaimed)."""
    now = int(time.time())
    r = db.execute(
        "UPDATE reviews SET state='claimable', claim_id=NULL, claimed_by=NULL, "
        "claimed_at=NULL, lease_expires=NULL, heartbeat_at=NULL "
        "WHERE state='claimed' AND lease_expires<=?", (now,)).rowcount
    m = db.execute(
        "UPDATE maintenance_tasks SET state='claimable', claim_id=NULL, "
        "claimed_by=NULL, claimed_at=NULL, lease_expires=NULL, "
        "heartbeat_at=NULL WHERE state='claimed' AND lease_expires<=?",
        (now,)).rowcount
    db.commit()
    return r, m


# ===========================================================================
# Maintenance-task queue
# ===========================================================================

def enqueue_maintenance_task(db, kind, payload=None):
    """Add a maintenance task. kind: 'holistic' | 'redraft'. Returns its id."""
    if kind not in ("holistic", "redraft"):
        raise ValueError(f"bad maintenance kind: {kind!r}")
    cur = db.execute("INSERT INTO maintenance_tasks (kind,payload,state,"
                     "created_at) VALUES (?,?,'claimable',?)",
                     (kind, json.dumps(payload) if payload is not None
                      else None, int(time.time())))
    db.commit()
    return cur.lastrowid


def claim_maintenance_task(db, worker_id, lease_seconds=1800):
    """Atomically claim the oldest claimable maintenance task (or a
       lease-expired one). Returns a dict {claim_id, id, kind, payload} or
       None."""
    now = int(time.time())
    claim_id = _new_claim_id()
    row = db.execute(
        "UPDATE maintenance_tasks SET state='claimed', claim_id=?, "
        "claimed_by=?, claimed_at=?, lease_expires=?, heartbeat_at=? "
        "WHERE id=("
        "  SELECT id FROM maintenance_tasks "
        "  WHERE state='claimable' "
        "        OR (state='claimed' AND lease_expires<=?) "
        "  ORDER BY created_at, id LIMIT 1) "
        "RETURNING claim_id, id, kind, payload",
        (claim_id, worker_id, now, now + lease_seconds, now, now)).fetchone()
    db.commit()
    if not row:
        return None
    task = dict(row)
    task["payload"] = json.loads(task["payload"]) if task["payload"] else None
    return task


def complete_maintenance_task(db, claim_id, result):
    """Record a maintenance task's result. Returns 'ok' | 'lapsed'."""
    row = db.execute("SELECT state FROM maintenance_tasks WHERE claim_id=?",
                     (claim_id,)).fetchone()
    if row is None:
        return "lapsed"
    if row["state"] != "claimed":
        return "ok"
    db.execute("UPDATE maintenance_tasks SET state='done', result=?, "
               "completed_at=? WHERE claim_id=?",
               (json.dumps(result), int(time.time()), claim_id))
    db.commit()
    return "ok"


# ===========================================================================
# Tier 2 - methodology
# ===========================================================================

def bootstrap_methodology(db, methodology_path, schema_path=None):
    """First-run seed: import a methodology YAML as version 1 if the
       methodology store is empty. Validates it against a JSON Schema YAML
       first when schema_path is given. A no-op once a version exists.
       Returns the active methodology version."""
    have = active_methodology(db)
    if have is not None:
        return have[0]
    import yaml
    with open(methodology_path, encoding="utf-8") as f:
        document = yaml.safe_load(f)
    if schema_path:
        import jsonschema
        with open(schema_path, encoding="utf-8") as f:
            jsonschema.validate(document, yaml.safe_load(f))
    return add_methodology_version(db, document, note="bootstrap seed")


def add_methodology_version(db, document, note=None):
    """Store `document` (a dict) as the next methodology version and make it
       active, superseding the previous active version. Returns the version."""
    nxt = db.execute("SELECT COALESCE(MAX(version),0)+1 "
                     "FROM methodology_versions").fetchone()[0]
    db.execute("UPDATE methodology_versions SET state='superseded' "
               "WHERE state='active'")
    db.execute("INSERT INTO methodology_versions "
               "(version,document,state,note,created_at) "
               "VALUES (?,?,'active',?,?)",
               (nxt, json.dumps(document), note, int(time.time())))
    db.commit()
    return nxt


def active_methodology(db):
    """The active methodology as (version, document_dict), or None if the
       store has not been bootstrapped."""
    row = db.execute("SELECT version, document FROM methodology_versions "
                     "WHERE state='active'").fetchone()
    return (row["version"], json.loads(row["document"])) if row else None


def add_candidate(db, candidate_id, description, origin=None):
    """Register a candidate practice on trial. Idempotent on the id."""
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO methodology_candidates "
               "(id,description,origin,created_at,updated_at) "
               "VALUES (?,?,?,?,?)", (candidate_id, description, origin,
                                      now, now))
    db.commit()


def bump_candidate(db, candidate_id, applied=0, catches=0):
    """Add to a candidate's self-honing counters and recompute its confidence
       (the Wilson lower bound of catches/applied)."""
    row = db.execute("SELECT applied_count,catches_count "
                     "FROM methodology_candidates WHERE id=?",
                     (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(candidate_id)
    a = row["applied_count"] + applied
    c = row["catches_count"] + catches
    db.execute("UPDATE methodology_candidates SET applied_count=?, "
               "catches_count=?, confidence=?, updated_at=? WHERE id=?",
               (a, c, wilson_lower(c, a), int(time.time()), candidate_id))
    db.commit()


def set_candidate_state(db, candidate_id, state):
    """Move a candidate to 'trial' | 'graduated' | 'pruned'."""
    if state not in ("trial", "graduated", "pruned"):
        raise ValueError(f"bad candidate state: {state!r}")
    db.execute("UPDATE methodology_candidates SET state=?, updated_at=? "
               "WHERE id=?", (state, int(time.time()), candidate_id))
    db.commit()


def list_candidates(db, state=None):
    """Candidate practices, all or filtered by state, as a list of dicts."""
    if state:
        rows = db.execute("SELECT * FROM methodology_candidates WHERE state=? "
                          "ORDER BY id", (state,))
    else:
        rows = db.execute("SELECT * FROM methodology_candidates ORDER BY id")
    return [dict(r) for r in rows]


def add_proposal(db, kind, payload):
    """Queue a merge-gate proposal. Returns its id."""
    valid = ("graduate", "prune-redundant", "prune-ineffective",
             "consolidate", "revise")
    if kind not in valid:
        raise ValueError(f"bad proposal kind: {kind!r}")
    cur = db.execute("INSERT INTO methodology_proposals "
                     "(kind,payload,state,created_at) VALUES (?,?,'pending',?)",
                     (kind, json.dumps(payload), int(time.time())))
    db.commit()
    return cur.lastrowid


def list_proposals(db, state="pending"):
    """Merge-gate proposals in a given state, as a list of dicts."""
    return [dict(r) for r in db.execute(
        "SELECT * FROM methodology_proposals WHERE state=? ORDER BY id",
        (state,))]


def decide_proposal(db, proposal_id, decision, decided_by=None, note=None):
    """Record a human decision on a proposal. decision: 'accepted' |
       'deferred' | 'rejected' | 'returned' ('returned' bumps redraft_count)."""
    if decision not in ("accepted", "deferred", "rejected", "returned"):
        raise ValueError(f"bad decision: {decision!r}")
    db.execute("UPDATE methodology_proposals SET state=?, decided_by=?, "
               "note=?, decided_at=?, redraft_count=redraft_count+? "
               "WHERE id=?",
               (decision, decided_by, note, int(time.time()),
                1 if decision == "returned" else 0, proposal_id))
    db.commit()


# ===========================================================================
# CLI
# ===========================================================================

def main():
    a = sys.argv
    if len(a) < 2:
        print(__doc__)
        return
    db = connect()
    cmd = a[1]
    if cmd == "init":
        print("schema ready at version", migrate(db), "-", DB)
    elif cmd == "bootstrap" and len(a) >= 3:
        v = bootstrap_methodology(db, a[2], a[3] if len(a) > 3 else None)
        print("methodology active at version", v)
    elif cmd == "seed-mailmap" and len(a) >= 3:
        print("seeded", seed_mailmap(db, a[2]), "alias email(s)")
    elif cmd == "stats":
        for t in ("patchsets", "patch_blobs", "patchset_sources",
                  "source_findings", "reviewers", "clients", "reviews",
                  "maintenance_tasks", "methodology_versions",
                  "methodology_candidates", "methodology_proposals"):
            n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:24} {n}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
