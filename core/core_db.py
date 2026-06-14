#!/usr/bin/env python3
"""core_db.py - the hone-core database: schema and data layer.

A SQLite store (WAL mode) holding the system's data tiers:

  - corpus       patchsets gathered from lore, their patch messages and
                 follow-up review comments (one `messages` row per email).
  - ai_reviews   hone-node's structured `concerns` reviews of patchsets.
  - methodology  the versioned methodology document, the candidate practices
                 on trial, the merge-gate proposal queue.
  - work queue   review (per-patchset) and train (per-patch-message) work
                 items, with the claim/lease handoff to nodes.
  - list tags    lore mailing-list universe and per-patchset tags, for the
                 operator's gather filter.
  - nodes/auth   enrolled nodes (OAuth device-grant) and bearer tokens.
  - gather state per-source opaque resume cursor.

Schema changes are versioned: PRAGMA user_version records the file's schema
version and connect() applies any newer migration. Harness machinery; NOT
the methodology itself.

Stdlib only, except bootstrap_methodology() which needs pyyaml + jsonschema.

CLI:
  init                          create / upgrade the schema (idempotent)
  bootstrap <meth> [<schema>]   import a methodology YAML as version 1
  seed-list-tags <file.json>    populate the list_tags universe (e.g. from
                                lore's manifest) — JSON object {tag: desc}
  stats                         row counts, per table
"""
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from urllib.parse import quote as _urlquote

log = logging.getLogger("hone.core_db")

# The database file: $HONE_DB when set (containerized hone-core), else the
# repo-root hone.db. core/ is one level below the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("HONE_DB") or os.path.join(_REPO_ROOT, "hone.db")


# ===========================================================================
# Enum constants  (small ints; kept in sync with the DDL CHECK clauses)
# ===========================================================================

# ---- patchsets.state ----
PATCHSET_STATE_GATHERED = 1
PATCHSET_STATE_SKIPPED  = 2
PATCHSET_STATE_NAMES    = {PATCHSET_STATE_GATHERED: "gathered",
                           PATCHSET_STATE_SKIPPED:  "skipped"}

PATCHSET_ORIGIN_GATHERED = 1     # a gather module (lore, …) — the corpus
PATCHSET_ORIGIN_UPLOADED = 2     # the web UI's upload page — a submission
PATCHSET_ORIGIN_NAMES    = {PATCHSET_ORIGIN_GATHERED: "gathered",
                            PATCHSET_ORIGIN_UPLOADED: "uploaded"}

# ---- messages.type ----
MSG_TYPE_COVER   = 1
MSG_TYPE_PATCH   = 2
MSG_TYPE_COMMENT = 3
MSG_TYPE_NAMES   = {MSG_TYPE_COVER:   "cover",
                    MSG_TYPE_PATCH:   "patch",
                    MSG_TYPE_COMMENT: "comment"}

# ---- ai_reviews.source ----
AI_REVIEW_SOURCE_HONE_NODE = 1
AI_REVIEW_SOURCE_NAMES     = {AI_REVIEW_SOURCE_HONE_NODE: "hone-node"}

# ---- notifications.type ----
# In-app user notifications. The `_NAMES` slugs double as the per-user
# preference keys in users.notification_prefs (a JSON {slug: bool} map;
# a missing key defaults ON).
NOTIF_TYPE_REVIEW_READY     = 1
NOTIF_TYPE_REVIEW_FAILED    = 2
NOTIF_TYPE_PREPARE_FAILED   = 3
NOTIF_TYPE_NEW_COMMENT      = 4
NOTIF_TYPE_PATCHSET_SKIPPED = 5
NOTIF_TYPE_NODE_HEALTH      = 6
NOTIF_TYPE_USER_ACCESS      = 7
NOTIF_TYPE_NAMES = {NOTIF_TYPE_REVIEW_READY:     "review_ready",
                    NOTIF_TYPE_REVIEW_FAILED:    "review_failed",
                    NOTIF_TYPE_PREPARE_FAILED:   "prepare_failed",
                    NOTIF_TYPE_NEW_COMMENT:      "new_comment",
                    NOTIF_TYPE_PATCHSET_SKIPPED: "patchset_skipped",
                    NOTIF_TYPE_NODE_HEALTH:      "node_health_alert",
                    NOTIF_TYPE_USER_ACCESS:      "user_access_request"}

# ---- list_tags.origin ----
LIST_TAG_ORIGIN_MANIFEST = 1
LIST_TAG_ORIGIN_OBSERVED = 2
LIST_TAG_ORIGIN_NAMES    = {LIST_TAG_ORIGIN_MANIFEST: "manifest",
                            LIST_TAG_ORIGIN_OBSERVED: "observed"}

# ---- methodology_versions.state ----
METHODOLOGY_VERSION_STATE_ACTIVE     = 1
METHODOLOGY_VERSION_STATE_SUPERSEDED = 2
METHODOLOGY_VERSION_STATE_NAMES = {
    METHODOLOGY_VERSION_STATE_ACTIVE:     "active",
    METHODOLOGY_VERSION_STATE_SUPERSEDED: "superseded"}

# ---- methodology_candidates.state ----
METHODOLOGY_CANDIDATE_STATE_PROPOSED  = 1
METHODOLOGY_CANDIDATE_STATE_TRIAL     = 2
METHODOLOGY_CANDIDATE_STATE_GRADUATED = 3
METHODOLOGY_CANDIDATE_STATE_PRUNED    = 4
METHODOLOGY_CANDIDATE_STATE_NAMES = {
    METHODOLOGY_CANDIDATE_STATE_PROPOSED:  "proposed",
    METHODOLOGY_CANDIDATE_STATE_TRIAL:     "trial",
    METHODOLOGY_CANDIDATE_STATE_GRADUATED: "graduated",
    METHODOLOGY_CANDIDATE_STATE_PRUNED:    "pruned"}

# ---- methodology_proposals.type / .state ----
METHODOLOGY_PROPOSAL_TYPE_GRADUATE              = 1
METHODOLOGY_PROPOSAL_TYPE_PRUNE_REDUNDANT       = 2
METHODOLOGY_PROPOSAL_TYPE_PRUNE_INEFFECTIVE     = 3
METHODOLOGY_PROPOSAL_TYPE_CONSOLIDATE           = 4
METHODOLOGY_PROPOSAL_TYPE_REVISE                = 5
METHODOLOGY_PROPOSAL_TYPE_REVISE_SEVERITY_SCALE = 6
METHODOLOGY_PROPOSAL_TYPE_NAMES = {
    METHODOLOGY_PROPOSAL_TYPE_GRADUATE:              "graduate",
    METHODOLOGY_PROPOSAL_TYPE_PRUNE_REDUNDANT:       "prune-redundant",
    METHODOLOGY_PROPOSAL_TYPE_PRUNE_INEFFECTIVE:     "prune-ineffective",
    METHODOLOGY_PROPOSAL_TYPE_CONSOLIDATE:           "consolidate",
    METHODOLOGY_PROPOSAL_TYPE_REVISE:                "revise",
    METHODOLOGY_PROPOSAL_TYPE_REVISE_SEVERITY_SCALE: "revise-severity-scale"}
METHODOLOGY_PROPOSAL_TYPE_BY_NAME = {
    v: k for k, v in METHODOLOGY_PROPOSAL_TYPE_NAMES.items()}

METHODOLOGY_PROPOSAL_STATE_PENDING  = 1
METHODOLOGY_PROPOSAL_STATE_ACCEPTED = 2
METHODOLOGY_PROPOSAL_STATE_DEFERRED = 3
METHODOLOGY_PROPOSAL_STATE_REJECTED = 4
METHODOLOGY_PROPOSAL_STATE_RETURNED = 5
METHODOLOGY_PROPOSAL_STATE_NAMES = {
    METHODOLOGY_PROPOSAL_STATE_PENDING:  "pending",
    METHODOLOGY_PROPOSAL_STATE_ACCEPTED: "accepted",
    METHODOLOGY_PROPOSAL_STATE_DEFERRED: "deferred",
    METHODOLOGY_PROPOSAL_STATE_REJECTED: "rejected",
    METHODOLOGY_PROPOSAL_STATE_RETURNED: "returned"}

# ---- eligibility_flags.subject_kind / .kind ----
ELIGIBILITY_SUBJECT_KIND_CANDIDATE      = 1
ELIGIBILITY_SUBJECT_KIND_CHECK          = 2
ELIGIBILITY_SUBJECT_KIND_SEVERITY_SCALE = 3
ELIGIBILITY_SUBJECT_KIND_NAMES = {
    ELIGIBILITY_SUBJECT_KIND_CANDIDATE:      "candidate",
    ELIGIBILITY_SUBJECT_KIND_CHECK:          "check",
    ELIGIBILITY_SUBJECT_KIND_SEVERITY_SCALE: "severity_scale"}

# eligibility kinds line up 1:1 with methodology_proposals.type values.
ELIGIBILITY_KIND_GRADUATE              = 1
ELIGIBILITY_KIND_PRUNE_REDUNDANT       = 2
ELIGIBILITY_KIND_PRUNE_INEFFECTIVE     = 3
ELIGIBILITY_KIND_CONSOLIDATE           = 4
ELIGIBILITY_KIND_REVISE                = 5
ELIGIBILITY_KIND_SEVERITY_SCALE_REVISE = 6
ELIGIBILITY_KIND_NAMES = {
    ELIGIBILITY_KIND_GRADUATE:              "graduate",
    ELIGIBILITY_KIND_PRUNE_REDUNDANT:       "prune-redundant",
    ELIGIBILITY_KIND_PRUNE_INEFFECTIVE:     "prune-ineffective",
    ELIGIBILITY_KIND_CONSOLIDATE:           "consolidate",
    ELIGIBILITY_KIND_REVISE:                "revise",
    ELIGIBILITY_KIND_SEVERITY_SCALE_REVISE: "revise-severity-scale"}

# ---- draft_tasks.state ----
DRAFT_TASK_STATE_CLAIMABLE = 1
DRAFT_TASK_STATE_CLAIMED   = 2
DRAFT_TASK_STATE_COMPLETED = 3
DRAFT_TASK_STATE_NAMES = {DRAFT_TASK_STATE_CLAIMABLE: "claimable",
                          DRAFT_TASK_STATE_CLAIMED:   "claimed",
                          DRAFT_TASK_STATE_COMPLETED: "completed"}

# ---- training_sessions.state ----
SESSION_STATE_DRAFT       = 1
SESSION_STATE_READY       = 2
SESSION_STATE_IN_PROGRESS = 3
SESSION_STATE_COMPLETE    = 4
SESSION_STATE_ANALYZED    = 5
SESSION_STATE_NAMES = {SESSION_STATE_DRAFT:       "draft",
                       SESSION_STATE_READY:       "ready",
                       SESSION_STATE_IN_PROGRESS: "in_progress",
                       SESSION_STATE_COMPLETE:    "complete",
                       SESSION_STATE_ANALYZED:    "analyzed"}

# ---- training_session_patchsets.role / .completion_state ----
SESSION_ROLE_POOL    = 1
SESSION_ROLE_HOLDOUT = 2
SESSION_ROLE_NAMES   = {SESSION_ROLE_POOL: "pool",
                        SESSION_ROLE_HOLDOUT: "holdout"}

SESSION_PATCHSET_COMPLETION_PENDING  = 1
SESSION_PATCHSET_COMPLETION_PARTIAL  = 2
SESSION_PATCHSET_COMPLETION_COMPLETE = 3
SESSION_PATCHSET_COMPLETION_NAMES = {
    SESSION_PATCHSET_COMPLETION_PENDING:  "pending",
    SESSION_PATCHSET_COMPLETION_PARTIAL:  "partial",
    SESSION_PATCHSET_COMPLETION_COMPLETE: "complete"}

# ---- nodes.state ----
NODE_STATE_ACTIVE  = 1
NODE_STATE_REVOKED = 2
NODE_STATE_NAMES   = {NODE_STATE_ACTIVE:  "active",
                      NODE_STATE_REVOKED: "revoked"}

# ---- node_enrollments.state ----
NODE_ENROLLMENT_STATE_PENDING   = 1
NODE_ENROLLMENT_STATE_APPROVED  = 2
NODE_ENROLLMENT_STATE_COMPLETED = 3
NODE_ENROLLMENT_STATE_DENIED    = 4
NODE_ENROLLMENT_STATE_NAMES = {
    NODE_ENROLLMENT_STATE_PENDING:   "pending",
    NODE_ENROLLMENT_STATE_APPROVED:  "approved",
    NODE_ENROLLMENT_STATE_COMPLETED: "completed",
    NODE_ENROLLMENT_STATE_DENIED:    "denied"}

# ---- node_tokens.state ----
NODE_TOKEN_STATE_ACTIVE     = 1
NODE_TOKEN_STATE_SUPERSEDED = 2
NODE_TOKEN_STATE_REVOKED    = 3
NODE_TOKEN_STATE_NAMES      = {NODE_TOKEN_STATE_ACTIVE:     "active",
                               NODE_TOKEN_STATE_SUPERSEDED: "superseded",
                               NODE_TOKEN_STATE_REVOKED:    "revoked"}

# ---- work_items.type / .state ----
WORK_ITEM_TYPE_PREPARE = 1
WORK_ITEM_TYPE_REVIEW  = 2
WORK_ITEM_TYPE_TRAIN   = 3
WORK_ITEM_TYPE_NAMES   = {WORK_ITEM_TYPE_PREPARE: "prepare",
                          WORK_ITEM_TYPE_REVIEW:  "review",
                          WORK_ITEM_TYPE_TRAIN:   "train"}

# The state column is a lifecycle CLASS, not a task-type outcome. Each
# task type's completion record carries its own outcome value (prepared
# / reviewed / trained / uncharacterisable / unappliable / deferred);
# those collapse to one of these three terminal classes (COMPLETED on
# success, UNAPPLIABLE on structural failure, DEFERRED when held).
WORK_ITEM_STATE_CLAIMABLE   = 1
WORK_ITEM_STATE_CLAIMED     = 2
WORK_ITEM_STATE_COMPLETED   = 3
WORK_ITEM_STATE_UNAPPLIABLE = 4
WORK_ITEM_STATE_DEFERRED    = 5
WORK_ITEM_STATE_NAMES = {WORK_ITEM_STATE_CLAIMABLE:   "claimable",
                         WORK_ITEM_STATE_CLAIMED:     "claimed",
                         WORK_ITEM_STATE_COMPLETED:   "completed",
                         WORK_ITEM_STATE_UNAPPLIABLE: "unappliable",
                         WORK_ITEM_STATE_DEFERRED:    "deferred"}
_WORK_ITEM_STATE_TERMINAL = frozenset({WORK_ITEM_STATE_COMPLETED,
                                       WORK_ITEM_STATE_UNAPPLIABLE,
                                       WORK_ITEM_STATE_DEFERRED})

# ---- severity (shared scale; ascending — also stored as a string in JSON
# inside ai_reviews.concerns, the methodology doc, and completion records). ----
SEVERITY_NIT      = 1
SEVERITY_MINOR    = 2
SEVERITY_MODERATE = 3
SEVERITY_MAJOR    = 4
SEVERITY_CRITICAL = 5
SEVERITY_NAMES = {SEVERITY_NIT:      "nit",
                  SEVERITY_MINOR:    "minor",
                  SEVERITY_MODERATE: "moderate",
                  SEVERITY_MAJOR:    "major",
                  SEVERITY_CRITICAL: "critical"}
SEVERITY_BY_NAME = {v: k for k, v in SEVERITY_NAMES.items()}


# ===========================================================================
# Schema  -  one migration creates the whole schema
# ===========================================================================

_SCHEMA_V1 = """
-- ---- Corpus -------------------------------------------------------------

CREATE TABLE patchsets (
    root_message_id  TEXT PRIMARY KEY,
    subject          TEXT,
    submitter_email  TEXT,
    sent             INTEGER,
    n_patches        INTEGER,                          -- 1 for a single [PATCH]
    base_commit      TEXT,
    change_id        TEXT,                             -- b4 Change-Id; links revisions
    series_version   INTEGER NOT NULL DEFAULT 1,
    state            INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2)),                       -- PATCHSET_STATE_GATHERED|SKIPPED
    skip_reason      TEXT,
    gathered_at      INTEGER
) WITHOUT ROWID;
CREATE INDEX idx_patchsets_changeid ON patchsets(change_id);
CREATE INDEX idx_patchsets_state    ON patchsets(state, gathered_at);

CREATE TABLE messages (
    message_id        TEXT PRIMARY KEY,
    root_message_id   TEXT NOT NULL REFERENCES patchsets(root_message_id),
    type              INTEGER NOT NULL
        CHECK (type IN (1, 2, 3)),                     -- MSG_TYPE_COVER|PATCH|COMMENT
    part_index        INTEGER,                         -- 0 cover; 1..M series;
                                                       -- NULL for [PATCH] and comments
    parent_message_id TEXT REFERENCES messages(message_id),
                                                       -- comment: the patch/cover it
                                                       -- pertains to (resolved up the
                                                       -- In-Reply-To chain)
    author_name       TEXT,
    author_email      TEXT,
    subject           TEXT,
    sent              INTEGER,
    body              TEXT NOT NULL                    -- raw email; comments carry
                                                       -- the inline-annotated reply
) WITHOUT ROWID;
CREATE INDEX idx_messages_root      ON messages(root_message_id, type);
CREATE INDEX idx_messages_parent    ON messages(parent_message_id);
CREATE INDEX idx_messages_root_part ON messages(root_message_id, part_index);

CREATE TABLE ai_reviews (
    id                   INTEGER PRIMARY KEY,
    root_message_id      TEXT NOT NULL REFERENCES patchsets(root_message_id),
    source               INTEGER NOT NULL DEFAULT 1
        CHECK (source IN (1)),                         -- AI_REVIEW_SOURCE_HONE_NODE
    concerns             TEXT NOT NULL,                -- JSON [{...}, ...]
                                                       -- each concern:
                                                       --   { concern_id, stage_id,
                                                       --     candidate_or_check_id,
                                                       --     text, severity,
                                                       --     is_preexisting,
                                                       --     patch_scope:
                                                       --       { kind, patches,
                                                       --         spans_lines_in_diff },
                                                       --     locations:[{file,
                                                       --       function_symbol,
                                                       --       code_snippet}] }
    model                TEXT,
    input_tokens         INTEGER,
    output_tokens        INTEGER,
    reviewed_at          INTEGER,
    methodology_version  INTEGER REFERENCES methodology_versions(version),
    node_id              INTEGER REFERENCES nodes(id),
    meta                 TEXT,                         -- JSON catch-all
    recorded_at          INTEGER NOT NULL,
    UNIQUE (root_message_id, source)
);
CREATE INDEX idx_ai_reviews_root ON ai_reviews(root_message_id);

CREATE TABLE patchset_metadata (
    root_message_id      TEXT PRIMARY KEY REFERENCES patchsets(root_message_id),
    methodology_version  INTEGER REFERENCES methodology_versions(version),
    node_tree_revision   TEXT,                         -- node's kernel-tree HEAD at prep
    mode                 TEXT NOT NULL
        CHECK (mode IN ('authoritative', 'heuristic', 'mixed')),
    -- one column per top-level field of the prepare-task output, each JSON.
    -- shape documented in docs/ARCHITECTURE.md → The patchset-metadata layer
    -- and validated by common/schema/completion-record.schema.yaml.
    tree_state           TEXT NOT NULL,                -- JSON
    subsystem            TEXT NOT NULL,                -- JSON
    patch_size           TEXT NOT NULL,                -- JSON
    maintainer           TEXT NOT NULL,                -- JSON
    patch_type           TEXT NOT NULL,                -- JSON
    review_intensity     TEXT NOT NULL,                -- JSON
    preparation_notes    TEXT NOT NULL,                -- JSON
    prepared_at          INTEGER NOT NULL
) WITHOUT ROWID;

CREATE TABLE review_evaluations (
    -- One row per (patchset, ai_review, session) — each session that
    -- selects a patchset produces its own aggregation when that session's
    -- trains for the patchset terminate. See
    -- docs/ARCHITECTURE-WORK-LIFECYCLE.md → Review-level aggregation.
    root_message_id                  TEXT NOT NULL REFERENCES patchsets(root_message_id),
    ai_review_id                     INTEGER NOT NULL REFERENCES ai_reviews(id),
    session_id                       INTEGER NOT NULL REFERENCES training_sessions(id),
    evaluated_at                     INTEGER NOT NULL,
    trains_consumed                  INTEGER NOT NULL DEFAULT 0,
    coverage_rate                    REAL,
    severity_weighted_coverage_rate  REAL,
    fp_rate                          REAL,
    preexisting_unmatched_count      INTEGER NOT NULL DEFAULT 0,
    redundancy_pairs                 INTEGER NOT NULL DEFAULT 0,
    had_missed_critical              INTEGER NOT NULL DEFAULT 0,   -- bool
    had_missed_major                 INTEGER NOT NULL DEFAULT 0,   -- bool
    per_concern_verdicts             TEXT NOT NULL DEFAULT '[]',   -- JSON
    per_candidate_review_stats       TEXT NOT NULL DEFAULT '[]',   -- JSON
    notes                            TEXT,                         -- JSON
    PRIMARY KEY (root_message_id, ai_review_id, session_id)
) WITHOUT ROWID;
CREATE INDEX idx_review_evaluations_session ON review_evaluations(session_id);


-- ---- List-tag filter ----------------------------------------------------

CREATE TABLE list_tags (
    tag            TEXT PRIMARY KEY,                   -- the List-Id
    description    TEXT,                               -- from the manifest
    origin         INTEGER NOT NULL CHECK (origin IN (1, 2)),
                                                       -- LIST_TAG_ORIGIN_MANIFEST|OBSERVED
    enabled        INTEGER NOT NULL DEFAULT 0,         -- operator gather filter
    first_seen_at  INTEGER,
    last_seen_at   INTEGER
) WITHOUT ROWID;

CREATE TABLE patchset_tags (
    root_message_id  TEXT NOT NULL REFERENCES patchsets(root_message_id),
    tag              TEXT NOT NULL REFERENCES list_tags(tag),
    PRIMARY KEY (root_message_id, tag)
) WITHOUT ROWID;
CREATE INDEX idx_patchset_tags_tag ON patchset_tags(tag);


-- ---- Methodology & honing -----------------------------------------------

CREATE TABLE methodology_versions (
    version     INTEGER PRIMARY KEY,
    document    TEXT NOT NULL,                         -- JSON: core +
                                                       -- operations.{prepare,review,
                                                       -- train,draft}.{guidance, return}
    state       INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2)),                       -- METHODOLOGY_VERSION_STATE_ACTIVE|SUPERSEDED
    note        TEXT,
    created_at  INTEGER
);

CREATE TABLE methodology_candidates (
    id                            TEXT PRIMARY KEY,    -- slug
    body                          TEXT NOT NULL,       -- the candidate's prose
    state                         INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3, 4)),                 -- METHODOLOGY_CANDIDATE_STATE_PROPOSED|TRIAL|GRADUATED|PRUNED
    -- Pooled per-candidate counters. `applied` increments on a review where the
    -- candidate's pattern was present in the patch and actually evaluated;
    -- `catches` counts code-verified hits; `unique_catches` is the subset of
    -- catches where the baseline review would have missed the issue.
    applied                       INTEGER NOT NULL DEFAULT 0,
    catches                       INTEGER NOT NULL DEFAULT 0,
    unique_catches                INTEGER NOT NULL DEFAULT 0,
    -- Two parallel per-tag histograms of the finding-severity tags this
    -- candidate has produced, split by whether the finding originated from
    -- patch-introduced code (introduced) or pre-existing code (preexisting).
    -- Each is JSON: {"critical": int, "major": int, "moderate": int,
    -- "minor": int, "nit": int}. Severity itself is per-finding, never per-
    -- candidate — there is no top_severity column.
    severity_witness_introduced   TEXT NOT NULL DEFAULT '{}',
    severity_witness_preexisting  TEXT NOT NULL DEFAULT '{}',
    origin                        TEXT,                -- the miss that originated it
    created_at                    INTEGER,
    updated_at                    INTEGER
) WITHOUT ROWID;

CREATE TABLE methodology_proposals (
    id             INTEGER PRIMARY KEY,
    type           INTEGER NOT NULL CHECK (type IN (1, 2, 3, 4, 5, 6)),
        -- METHODOLOGY_PROPOSAL_TYPE_GRADUATE|PRUNE_REDUNDANT|PRUNE_INEFFECTIVE|
        -- CONSOLIDATE|REVISE|REVISE_SEVERITY_SCALE
    payload        TEXT NOT NULL,                      -- JSON: { recommendation,
                                                       --   subject_ids[], rationale,
                                                       --   payload (the concrete
                                                       --   change, by recommendation),
                                                       --   base_methodology_version,
                                                       --   parent_id (redraft lineage),
                                                       --   evidence_snapshot,
                                                       --   session_lineage }
    state          INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3, 4, 5)),
        -- METHODOLOGY_PROPOSAL_STATE_PENDING|ACCEPTED|DEFERRED|REJECTED|RETURNED
    redraft_count  INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    created_at     INTEGER,
    decided_at     INTEGER,
    decided_by     TEXT
);

CREATE TABLE eligibility_flags (
    id                  INTEGER PRIMARY KEY,
    subject_kind        INTEGER NOT NULL CHECK (subject_kind IN (1, 2, 3)),
                                                       -- ELIGIBILITY_SUBJECT_KIND_
                                                       -- CANDIDATE|CHECK|SEVERITY_SCALE
    subject_id          TEXT NOT NULL,                 -- candidate/check id; or
                                                       -- 'severity_scale' for the rubric
    kind                INTEGER NOT NULL
        CHECK (kind IN (1, 2, 3, 4, 5, 6)),            -- ELIGIBILITY_KIND_GRADUATE|
                                                       -- PRUNE_REDUNDANT|PRUNE_INEFFECTIVE|
                                                       -- CONSOLIDATE|REVISE|
                                                       -- SEVERITY_SCALE_REVISE
    evidence_snapshot   TEXT NOT NULL,                 -- JSON; gate's verdict at set_at
    set_at              INTEGER NOT NULL,
    suppressed_at       INTEGER,                       -- NULL until Reject-filtered
    defer_watermark_at  INTEGER,                       -- NULL until Defer-filtered
    UNIQUE (subject_kind, subject_id, kind)
);
CREATE INDEX idx_eligibility_flags_subject ON eligibility_flags(subject_id, kind);
CREATE INDEX idx_eligibility_flags_actionable
    ON eligibility_flags(kind, set_at)
    WHERE suppressed_at IS NULL AND defer_watermark_at IS NULL;


-- ---- Work queue ---------------------------------------------------------

CREATE TABLE work_items (
    id                   INTEGER PRIMARY KEY,
    type                 INTEGER NOT NULL CHECK (type IN (1, 2, 3)),
                                                       -- WORK_ITEM_TYPE_PREPARE|REVIEW|TRAIN
    root_message_id      TEXT NOT NULL REFERENCES patchsets(root_message_id),
    message_id           TEXT REFERENCES messages(message_id),
                                                       -- train: the patch the comment
                                                       -- replies to; NULL for prepare/review
    comment_message_id   TEXT REFERENCES messages(message_id),
                                                       -- train: the specific maintainer
                                                       -- comment this train evaluates
                                                       -- (selected at session-materialise
                                                       -- time, per the comment
                                                       -- trainability filter); NULL for
                                                       -- prepare/review
    state                INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3, 4, 5)),
        -- WORK_ITEM_STATE_CLAIMABLE|CLAIMED|REVIEWED|UNAPPLIABLE|DEFERRED
    claim_id             TEXT,
    claimed_by           TEXT,
    claimed_at           INTEGER,
    lease_expires        INTEGER,
    heartbeat_at         INTEGER,
    methodology_version  INTEGER REFERENCES methodology_versions(version),
    -- Train work-items are created exclusively by the session orchestrator,
    -- so these four fields are required-when-train and forbidden-otherwise
    -- (see the trailing CHECK constraint).
    -- See docs/ARCHITECTURE-WORK-LIFECYCLE.md → *Session-linked trains*.
    training_session_id  INTEGER REFERENCES training_sessions(id),
    session_role         INTEGER CHECK (session_role IN (1, 2)),
                                                       -- SESSION_ROLE_POOL|HOLDOUT
    stratum_label        TEXT,
    record               TEXT,                         -- JSON completion record
    enqueued_at          INTEGER,
    completed_at         INTEGER,
    CHECK (
      (type IN (1, 2)                                  -- PREPARE | REVIEW
       AND training_session_id IS NULL
       AND session_role IS NULL
       AND stratum_label IS NULL
       AND comment_message_id IS NULL)
      OR
      (type = 3                                        -- TRAIN
       AND training_session_id IS NOT NULL
       AND session_role IS NOT NULL
       AND stratum_label IS NOT NULL
       AND comment_message_id IS NOT NULL
       AND message_id IS NOT NULL)
    )
);
CREATE INDEX idx_work_items_claimable ON work_items(type, state, enqueued_at);
CREATE INDEX idx_work_items_claim     ON work_items(claim_id);
CREATE INDEX idx_work_items_root      ON work_items(root_message_id, type);
CREATE INDEX idx_work_items_message   ON work_items(message_id);
CREATE INDEX idx_work_items_comment   ON work_items(comment_message_id);
CREATE INDEX idx_work_items_session   ON work_items(training_session_id);

CREATE TABLE draft_tasks (
    id                         INTEGER PRIMARY KEY,
    eligibility_flag_snapshot  TEXT NOT NULL,          -- JSON array of flag snapshots
                                                       -- as of enqueue
    parent_proposal_id         INTEGER REFERENCES methodology_proposals(id),
                                                       -- set on redraft tasks
    methodology_version        INTEGER REFERENCES methodology_versions(version),
    state                      INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3)),                    -- DRAFT_TASK_STATE_CLAIMABLE|CLAIMED|COMPLETED
    claim_id                   TEXT,
    claimed_by                 TEXT,
    claimed_at                 INTEGER,
    lease_expires              INTEGER,
    heartbeat_at               INTEGER,
    record                     TEXT,                   -- JSON draft completion record
    created_at                 INTEGER,
    completed_at               INTEGER
);
CREATE INDEX idx_draft_tasks_claimable ON draft_tasks(state, created_at);


-- ---- Training sessions --------------------------------------------------

CREATE TABLE training_sessions (
    id                   INTEGER PRIMARY KEY,
    created_at           INTEGER NOT NULL,
    created_by           TEXT,
    state                INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3, 4, 5)),
        -- SESSION_STATE_DRAFT|READY|IN_PROGRESS|COMPLETE|ANALYZED
    profile              TEXT NOT NULL,                -- 'standard', 'targeted_graduation',
                                                       -- 'targeted_prune', 'coverage_repair',
                                                       -- 'holdout_refresh', 'exploratory',
                                                       -- 'custom'
    target_pool_size     INTEGER,
    target_holdout_size  INTEGER,
    actual_pool_size     INTEGER,
    actual_holdout_size  INTEGER,
    stratification_spec  TEXT,                         -- JSON
    methodology_version  INTEGER REFERENCES methodology_versions(version),
    corpus_snapshot_at   INTEGER,
    completed_at         INTEGER,
    stats                TEXT                          -- JSON summary; written at analyze
);

CREATE TABLE training_session_patchsets (
    session_id              INTEGER NOT NULL REFERENCES training_sessions(id),
    root_message_id         TEXT NOT NULL REFERENCES patchsets(root_message_id),
    role                    INTEGER NOT NULL CHECK (role IN (1, 2)),
                                                       -- SESSION_ROLE_POOL|HOLDOUT
    stratum_label           TEXT NOT NULL,
    selected_at             INTEGER NOT NULL,
    completion_state        INTEGER NOT NULL DEFAULT 1
        CHECK (completion_state IN (1, 2, 3)),
        -- SESSION_PATCHSET_COMPLETION_PENDING|PARTIAL|COMPLETE
    train_work_items_total  INTEGER NOT NULL DEFAULT 0,
    train_work_items_done   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, root_message_id)
) WITHOUT ROWID;

-- Denormalised index over training_session_patchsets — "find me N patchsets
-- never used in any session in any/this role" is the session-draft solver's
-- hot path and benefits from a flat index rather than a join.
CREATE TABLE patchset_session_history (
    root_message_id  TEXT NOT NULL REFERENCES patchsets(root_message_id),
    session_id       INTEGER NOT NULL REFERENCES training_sessions(id),
    role             INTEGER NOT NULL CHECK (role IN (1, 2)),
    used_at          INTEGER NOT NULL,
    PRIMARY KEY (root_message_id, session_id)
) WITHOUT ROWID;
CREATE INDEX idx_patchset_session_role ON patchset_session_history(role);


-- ---- Nodes, enrollment, auth --------------------------------------------

CREATE TABLE nodes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    task_types   TEXT,                                 -- JSON array, e.g.
                                                       -- ["prepare","review","train","draft"];
                                                       -- NULL = every task type
    state        INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2)),                       -- NODE_STATE_ACTIVE|REVOKED
    enrolled_at  INTEGER,
    last_seen    INTEGER
);

CREATE TABLE node_enrollments (
    id                INTEGER PRIMARY KEY,
    device_code_hash  TEXT NOT NULL UNIQUE,
    user_code         TEXT NOT NULL UNIQUE,
    node_name         TEXT,
    task_types        TEXT,                            -- JSON array
    state             INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3, 4)),                 -- NODE_ENROLLMENT_STATE_PENDING|APPROVED|COMPLETED|DENIED
    node_id           INTEGER REFERENCES nodes(id),
    interval_seconds  INTEGER NOT NULL DEFAULT 5,
    created_at        INTEGER,
    expires_at        INTEGER,
    last_polled_at    INTEGER,
    decided_at        INTEGER,
    decided_by        TEXT
);

CREATE TABLE node_tokens (
    id                  INTEGER PRIMARY KEY,
    node_id             INTEGER NOT NULL REFERENCES nodes(id),
    access_token_hash   TEXT NOT NULL UNIQUE,
    access_expires_at   INTEGER NOT NULL,
    refresh_token_hash  TEXT NOT NULL UNIQUE,
    refresh_expires_at  INTEGER,                       -- NULL = no expiry
    state               INTEGER NOT NULL DEFAULT 1
        CHECK (state IN (1, 2, 3)),                    -- NODE_TOKEN_STATE_ACTIVE|SUPERSEDED|REVOKED
    created_at          INTEGER
);
CREATE INDEX idx_node_tokens_access  ON node_tokens(access_token_hash);
CREATE INDEX idx_node_tokens_refresh ON node_tokens(refresh_token_hash);


-- ---- Gather state -------------------------------------------------------

CREATE TABLE gather_state (
    source      TEXT PRIMARY KEY,                      -- gather module name
    cursor      TEXT NOT NULL DEFAULT '',              -- opaque module-defined token
    updated_at  INTEGER NOT NULL
) WITHOUT ROWID;
"""

# Migration v2 — node health snapshot. ALTERs the nodes table to add a
# JSON snapshot column + its timestamp; the node posts to
# /v1/nodes/me/health and the operator UI renders the latest snapshot
# next to the State badge. One row per node, latest-only — see
# update_node_health.
_SCHEMA_V2 = """
ALTER TABLE nodes ADD COLUMN health TEXT;
ALTER TABLE nodes ADD COLUMN health_at INTEGER;
"""

# Migration v3 — operator user accounts (self-registration + admin approval).
# state: 'pending' on signup, 'approved' after admin approves, 'revoked' to block.
# password_hash is NULL for Google-only users; google_sub is NULL for local users.
_SCHEMA_V3 = """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    display_name  TEXT,
    auth_provider TEXT NOT NULL DEFAULT 'local',
    google_sub    TEXT UNIQUE,
    state         TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'approved', 'revoked')),
    created_at    INTEGER NOT NULL,
    approved_at   INTEGER,
    last_login_at INTEGER
);
"""

# Migration v4 — the multi-user release:
#
#   - users.auth_provider gains a CHECK matching users.state's style.
#     SQLite has no ALTER TABLE … ADD CHECK, so rebuild the table —
#     and since we're rebuilding anyway, the per-user permission grants
#     (is_admin, is_maintainer) ride in the new shape directly.
#   - Node ownership (owner_user_id / handles_system) + enrollment
#     pairing + work-item origin — the per-user queue machinery.
#   - Patchset origin (gathered corpus vs web-UI upload).
#   - Work-item deferral bookkeeping (defer_count).
_SCHEMA_V4 = """
CREATE TABLE users_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    display_name  TEXT,
    auth_provider TEXT NOT NULL DEFAULT 'local'
        CHECK (auth_provider IN ('local', 'google')),
    google_sub    TEXT UNIQUE,
    state         TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'approved', 'revoked')),
    created_at    INTEGER NOT NULL,
    approved_at   INTEGER,
    last_login_at INTEGER,
    -- Per-user admin grant: an admin can promote a regular account from
    -- the Users screen. The config-token admin (HONE_ADMIN_TOKEN, no
    -- users row) remains the bootstrap / backstop admin. Re-derived from
    -- this column on every request (auth.current_session_user), so
    -- granting or demoting takes effect on the user's next request —
    -- same freshness as revoke.
    is_admin      INTEGER NOT NULL DEFAULT 0
        CHECK (is_admin IN (0, 1)),
    -- Per-user maintainer grant: maintainers (and admins) browse the
    -- gathered corpus and select patchsets for review; a regular account
    -- only sees its own uploads. Same per-request re-derivation.
    is_maintainer INTEGER NOT NULL DEFAULT 0
        CHECK (is_maintainer IN (0, 1))
);
INSERT INTO users_new
    (id, email, password_hash, display_name, auth_provider, google_sub,
     state, created_at, approved_at, last_login_at)
    SELECT id, email, password_hash, display_name, auth_provider, google_sub,
           state, created_at, approved_at, last_login_at FROM users;
DROP TABLE users;
ALTER TABLE users_new RENAME TO users;

-- Node ownership: every node is now optionally owned by a user. Legacy
-- nodes (rows present before this migration) get NULL — they keep working
-- as system-only workers (handles_system=1 default below). New nodes are
-- stamped with owner_user_id by approve_enrollment().
ALTER TABLE nodes ADD COLUMN owner_user_id INTEGER
    REFERENCES users(id) ON DELETE SET NULL;
-- Whether this node falls back to the system pool when its owner's user
-- queue is empty. Default 1 keeps legacy nodes claiming as they did pre-
-- migration; approve_enrollment() explicitly sets 0 for new nodes so a
-- freshly-paired node is strictly user-only until its owner opts in from
-- the /nodes/{id} detail page.
ALTER TABLE nodes ADD COLUMN handles_system INTEGER NOT NULL DEFAULT 1
    CHECK (handles_system IN (0, 1));
CREATE INDEX idx_nodes_owner ON nodes(owner_user_id);

-- Pending-enrollment ownership: stamped when a user first looks up the
-- user_code on /enroll, so the pending row appears only on that user's
-- /nodes page (first-lookup-wins pairing).
ALTER TABLE node_enrollments ADD COLUMN requested_by_user_id INTEGER
    REFERENCES users(id) ON DELETE SET NULL;

-- Work-item origin: NULL = system (gather / orchestrator), non-NULL =
-- user-requested. Drives the two-step claim in claim_work_item.
ALTER TABLE work_items ADD COLUMN requested_by_user_id INTEGER
    REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX idx_work_items_user_claimable
    ON work_items(requested_by_user_id, state, type, enqueued_at);

-- Deferral bookkeeping: how many times a work item has come back
-- `deferred` (base tree unobtainable). Drives the exponential re-offer
-- backoff and the park-after-cap rule in submit_work_result — without
-- it a permanently-unobtainable base retries every lease window,
-- forever. Reset to 0 by the admin's release-deferred re-arm.
ALTER TABLE work_items ADD COLUMN defer_count INTEGER NOT NULL DEFAULT 0;

-- Patchset origin: gathered from a list archive (the corpus — training
-- data, maintainer-selected reviews) vs uploaded through the web UI (a
-- submission: "review my series"). Uploaded patchsets are never training
-- data — the session selector and corpus stats exclude them by origin.
-- Legacy rows are all gathered.
ALTER TABLE patchsets ADD COLUMN origin INTEGER NOT NULL DEFAULT 1
    CHECK (origin IN (1, 2));        -- PATCHSET_ORIGIN_GATHERED|UPLOADED
-- The uploader, for the per-user "my patchsets" view and for stamping
-- the auto-chained review's work-item origin.
ALTER TABLE patchsets ADD COLUMN uploaded_by_user_id INTEGER
    REFERENCES users(id) ON DELETE SET NULL;
"""

def _migrate_v5_series_version(db):
    """Backfill patchsets.series_version from the stored subject. The lore
       gather module didn't parse `[PATCH vN ...]` until now, so every
       gathered row sits at the column default of 1; the subject the
       version lives in was stored all along. Re-uses the upload parser —
       the two subject grammars are deliberately identical."""
    from core.upload import _series_version
    for row in db.execute(
            "SELECT root_message_id, subject FROM patchsets "
            "WHERE subject IS NOT NULL").fetchall():
        v = _series_version(row["subject"])
        if v != 1:
            db.execute(
                "UPDATE patchsets SET series_version=? "
                "WHERE root_message_id=?", (v, row["root_message_id"]))


_SCHEMA_V6 = """
-- Iteration linking for uploaded patchsets: a re-uploaded series (fresh
-- format-patch Message-IDs, same work — the pre-list iterate-on-review
-- loop) points at the iteration it replaces. NULL for first iterations
-- and for every gathered row. The chain gives "iteration N" and the
-- /my-patchsets one-row-per-series grouping; linking is offered at
-- upload preview (opt-out), never inferred silently.
ALTER TABLE patchsets ADD COLUMN supersedes_root_message_id TEXT
    REFERENCES patchsets(root_message_id);
CREATE INDEX idx_patchsets_supersedes
    ON patchsets(supersedes_root_message_id);
"""

_SCHEMA_V7 = """
-- Daily operations rollup — one row per CLOSED UTC day, written once by
-- the lazy materializer (core/reports.py) and never recomputed: several
-- flows delete work_items rows (cancel, superseded-iteration retirement,
-- delete_review, delete_patchset), so live-table recomputation would
-- undercount history. These rows freeze each day's truth at day close.
-- Sums only, never averages (duration_ms_sum + duration_n) so weekly
-- rollups can weight correctly.
CREATE TABLE daily_stats (
    day                 TEXT PRIMARY KEY,      -- 'YYYY-MM-DD', UTC
    ops_prepare         INTEGER NOT NULL DEFAULT 0,  -- terminal, by type
    ops_review          INTEGER NOT NULL DEFAULT 0,
    ops_train           INTEGER NOT NULL DEFAULT 0,
    ops_completed       INTEGER NOT NULL DEFAULT 0,  -- terminal, by state
    ops_unappliable     INTEGER NOT NULL DEFAULT 0,
    ops_deferred        INTEGER NOT NULL DEFAULT 0,
    ops_user_origin     INTEGER NOT NULL DEFAULT 0,  -- requested_by_user_id set
    ops_system_origin   INTEGER NOT NULL DEFAULT 0,
    ops_enqueued        INTEGER NOT NULL DEFAULT 0,  -- by enqueued_at
    patchsets_gathered  INTEGER NOT NULL DEFAULT 0,  -- by gathered_at + origin
    patchsets_uploaded  INTEGER NOT NULL DEFAULT 0,
    active_users        INTEGER NOT NULL DEFAULT 0,  -- distinct upload/request/login
    nodes_active        INTEGER NOT NULL DEFAULT 0,  -- distinct claimed_by, terminal
    input_tokens        INTEGER NOT NULL DEFAULT 0,  -- work_items.record $.usage
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    duration_ms_sum     INTEGER NOT NULL DEFAULT 0,
    duration_n          INTEGER NOT NULL DEFAULT 0,  -- rows carrying duration_ms
    computed_at         INTEGER NOT NULL
) WITHOUT ROWID;

-- The materializer scans bare timestamp ranges; the existing composite
-- indexes (state/type-led) don't serve those.
CREATE INDEX idx_work_items_completed ON work_items(completed_at);
CREATE INDEX idx_work_items_enqueued  ON work_items(enqueued_at);
"""

_SCHEMA_V8 = """
-- Claiming a gathered series: the submitter's account takes ownership of
-- work that entered hone through the lore gather — "this is my series" —
-- so it can surface on /my-patchsets and grant the pipeline actions
-- (request prepare/review on the claimant's own nodes). An association
-- only: origin stays GATHERED (the row remains corpus / training data,
-- its bodies stay exactly what lore said) and it is deliberately NOT
-- uploaded_by_user_id — "I uploaded these bytes" keeps driving the
-- upload collision/invalidation logic, "this is my work" drives the
-- dashboard and action rights. NULL = unclaimed; one claimant per
-- series (claim_patchset is first-wins).
ALTER TABLE patchsets ADD COLUMN claimed_by_user_id INTEGER
    REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX idx_patchsets_claimed ON patchsets(claimed_by_user_id);
"""

_SCHEMA_V9 = """
-- Claims become cooperative: MANY developers may claim one gathered
-- series ("I'm working with this") — claiming asserts interest, not
-- authorship, so it carries no submitter-address gate. Each user's
-- dashboard blends only THEIR claims and uploads; the junction is the
-- per-(series, user) association the v8 single-claimant column can't
-- express. Existing claims are carried over, then the column goes.
CREATE TABLE patchset_claims (
    root_message_id TEXT NOT NULL
        REFERENCES patchsets(root_message_id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL
        REFERENCES users(id) ON DELETE CASCADE,
    claimed_at      INTEGER NOT NULL,
    PRIMARY KEY (root_message_id, user_id)
) WITHOUT ROWID;
CREATE INDEX idx_patchset_claims_user ON patchset_claims(user_id);

INSERT INTO patchset_claims (root_message_id, user_id, claimed_at)
    SELECT root_message_id, claimed_by_user_id, strftime('%s', 'now')
    FROM patchsets WHERE claimed_by_user_id IS NOT NULL;

DROP INDEX idx_patchsets_claimed;
ALTER TABLE patchsets DROP COLUMN claimed_by_user_id;
"""

_SCHEMA_V10 = """
-- Corpus-listing scale (the 100k-patchsets tier; see the two-phase
-- listing queries in count_patchsets / list_patchsets_page for the
-- O(page) tier below it).
--
-- 1. The default corpus view is WHERE origin ORDER BY sent LIMIT n:
--    without an index the planner scans every gathered row into the
--    sorter on every page-load (linear — ~33ms/1M rows in-memory,
--    worse on disk). With it, it walks 25 index entries and stops.
CREATE INDEX idx_patchsets_origin_sent ON patchsets(origin, sent DESC);

-- 2. The search box: LIKE '%term%' is categorically un-indexable
--    (leading wildcard), so every search scanned the corpus AND
--    probed messages once per row for the author fields. FTS5 with
--    the TRIGRAM tokenizer keeps today's substring semantics (plain
--    FTS5 matches word/prefix only — 'ohn' would silently stop
--    finding 'John') at index speed, O(matches) not O(corpus). The
--    index trades disk for that (~3 tokens per character).
--
--    FTS5 is addressed by integer rowid, and patchsets is a WITHOUT
--    ROWID table — patchset_fts_map is the bridge: a plain rowid
--    table keyed by root_message_id whose implicit rowid IS the FTS
--    docid, giving the per-upsert refresh an indexed delete (rowid
--    lookup) instead of an FTS-table scan. Gathered rows only: the
--    corpus listing excludes uploads unconditionally. Sync is in
--    code, not triggers (_refresh_patchset_fts from the upsert /
--    delete paths) — gather is the single writer, and the refresh
--    needs the patchsets ⋈ messages join anyway. Searches shorter
--    than 3 chars can't produce a trigram; they keep the LIKE scan
--    (_patchset_list_where).
CREATE TABLE patchset_fts_map (
    root_message_id TEXT PRIMARY KEY    -- implicit rowid = FTS docid
);

CREATE VIRTUAL TABLE patchset_fts USING fts5(
    subject, author_name, author_email, submitter_email,
    tokenize='trigram');

INSERT INTO patchset_fts_map (root_message_id)
    SELECT root_message_id FROM patchsets WHERE origin = 1;

INSERT INTO patchset_fts
    (rowid, subject, author_name, author_email, submitter_email)
    SELECT m.rowid, p.subject, rm.author_name, rm.author_email,
           p.submitter_email
    FROM patchset_fts_map m
    JOIN patchsets p ON p.root_message_id = m.root_message_id
    LEFT JOIN messages rm ON rm.message_id = p.root_message_id;
"""

_SCHEMA_V11 = """
-- Corpus-listing scale, final tier: denormalise the listing's
-- message-derived display fields onto patchsets, so NO corpus-listing
-- query path touches the messages table (1.2GB at 11k patchsets and
-- growing with the corpus). Before this, sorting by author / parts /
-- comments still computed those values for every gathered row on
-- every load (~183ms at 11k, seconds at 1M), and the sub-trigram
-- LIKE search fallback still probed messages once per row.
--
--   n_parts / n_comments    what the corpus actually holds: attached
--                           patch messages and thread comments.
--                           DISTINCT from n_patches, the [PATCH N/M]
--                           series total the submitter declared.
--   root_author_name/email  the root message's author — the listing's
--                           Author column (falls back to
--                           submitter_email when NULL).
--
-- Maintained by upsert_message (the single message-write path) per
-- landed message; whole-thread deletes go through delete_patchset
-- where the row itself goes. Backfilled here from messages.
ALTER TABLE patchsets ADD COLUMN n_parts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE patchsets ADD COLUMN n_comments INTEGER NOT NULL DEFAULT 0;
ALTER TABLE patchsets ADD COLUMN root_author_name TEXT;
ALTER TABLE patchsets ADD COLUMN root_author_email TEXT;

UPDATE patchsets SET
    n_parts = (SELECT count(*) FROM messages m
               WHERE m.root_message_id = patchsets.root_message_id
               AND m.type = 2),
    n_comments = (SELECT count(*) FROM messages m
                  WHERE m.root_message_id = patchsets.root_message_id
                  AND m.type = 3),
    root_author_name = (SELECT m.author_name FROM messages m
                        WHERE m.message_id = patchsets.root_message_id),
    root_author_email = (SELECT m.author_email FROM messages m
                         WHERE m.message_id = patchsets.root_message_id);
"""

# Migration v12 — per-check coverage on a review. A JSON column holding the
# denominator the "most used checks" metric needs: for each methodology check,
# whether it was APPLICABLE to the patch (derived deterministically from the
# diff + patch_type by core.check_gates) and whether it FIRED — not just the
# numerator already in `concerns`. Deterministic + recomputable, so old rows
# can be backfilled; latest-only per review, like the rest of ai_reviews.
_SCHEMA_V12 = """
ALTER TABLE ai_reviews ADD COLUMN check_coverage TEXT;
"""

# Migration v13 — in-app user notifications. A generic per-user feed: events
# (review ready/failed, prepare failed, new comment, patchset skipped, node
# health alert, user access request) fan out one row per interested user.
# Generic shape (title + link, nullable root_message_id) so patchset, node, and
# admin events share one table. UNIQUE(user_id, dedup_key) makes fan-out
# idempotent (a re-running gather is a no-op). Email-ready: payload JSON +
# emailed_at for a future delivery worker. users.notification_prefs is a JSON
# {type-slug: bool} opt-in/out map (missing key = ON).
_SCHEMA_V13 = """
CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type            INTEGER NOT NULL,
    title           TEXT NOT NULL,
    link            TEXT,
    root_message_id TEXT REFERENCES patchsets(root_message_id) ON DELETE CASCADE,
    dedup_key       TEXT NOT NULL,
    payload         TEXT,
    created_at      INTEGER NOT NULL,
    read_at         INTEGER,
    emailed_at      INTEGER,
    UNIQUE (user_id, dedup_key)
);
CREATE INDEX idx_notifications_unread
    ON notifications(user_id, created_at DESC) WHERE read_at IS NULL;
CREATE INDEX idx_notifications_user
    ON notifications(user_id, created_at DESC);
ALTER TABLE users ADD COLUMN notification_prefs TEXT;
"""

# A migration is either a DDL script (executescript) or a Python callable
# taking the connection — for data fixes SQL can't express (v5 needs a
# regex). Both run under the same user_version bookkeeping.
_MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4,
               _migrate_v5_series_version, _SCHEMA_V6, _SCHEMA_V7,
               _SCHEMA_V8, _SCHEMA_V9, _SCHEMA_V10, _SCHEMA_V11, _SCHEMA_V12,
               _SCHEMA_V13]


# How long a connection waits on another connection's write lock before
# raising "database is locked". WAL writers are serialized across
# connections; with sub-second writes throughout, 5s is generous.
_BUSY_TIMEOUT_MS = 5000


def connect(path=None):
    """Open the database (creating the file if absent), apply any pending
       schema migrations, and return the connection.

       This is the bootstrap / single-consumer entry point (lifespan
       startup, gather worker cycles, tests, the CLI). The route
       handlers share ThreadLocalDB below instead — one sqlite3
       connection must never be used by two threads at once.
       SQLite's WAL mode gives concurrent readers + a serialized writer."""
    db = sqlite3.connect(path or DB, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    migrate(db)                          # runs with foreign_keys off (default)
    db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _open(path):
    """One configured connection, no migration — ThreadLocalDB's per-thread
       opener. The caller guarantees the schema is already at head (connect()
       ran once at startup). check_same_thread is off only so close() can
       run from the shutdown thread; thread-locality is what prevents
       concurrent use."""
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA foreign_keys=ON")
    return db


class ThreadLocalDB:
    """One SQLite connection per thread, opened lazily, proxying the
       sqlite3.Connection API via __getattr__.

       Stored at app.state.db so every `request.app.state.db` consumer
       transparently gets a connection private to its thread. FastAPI
       runs sync dependencies and handlers in threadpool worker threads
       while async handlers run on the event loop thread; sharing one
       connection between them is a race — python's sqlite3 caches
       prepared statements per connection, and two threads hitting the
       same SQL string at the same instant collide on the cached
       statement (SQLITE_MISUSE: "bad parameter or other API misuse"),
       or worse, silently cross results. Per-thread connections remove
       the shared object; WAL gives the concurrent readers + serialized
       writer across them.

       Threadpool workers are NOT long-lived — anyio retires a worker
       after 10s idle — so the pool is bounded by sweeping, not by the
       threadpool size: each new open first closes the connections of
       threads that have since died. Without the sweep, every retired
       worker leaked an open connection (3 fds each under WAL: db +
       -wal + -shm) until the process hit EMFILE and sqlite3.connect
       failed with "unable to open database file". close() closes
       every remaining connection — call it only at shutdown, after
       the server has drained."""

    def __init__(self, path):
        self._path = path
        self._tlocal = threading.local()
        self._all = []                   # [(owner thread, conn), ...]
        self._lock = threading.Lock()

    def _conn(self):
        conn = getattr(self._tlocal, "conn", None)
        if conn is None:
            conn = _open(self._path)
            self._tlocal.conn = conn
            with self._lock:
                live, dead = [], []
                for t, c in self._all:
                    (live if t.is_alive() else dead).append((t, c))
                live.append((threading.current_thread(), conn))
                self._all = live
            # Outside the lock — close() takes sqlite-internal locks
            # and never needs to serialize against other opens. Safe
            # for the same reason as shutdown close(): the owner
            # thread is dead, so the connection is idle for good.
            for _, c in dead:
                c.close()
        return conn

    def __getattr__(self, name):
        return getattr(self._conn(), name)

    def close(self):
        """Close every per-thread connection (shutdown only). Closing
           another thread's idle connection is safe — sqlite3 only
           forbids *use* across threads, which check_same_thread guards
           per-connection; by shutdown the worker threads are idle."""
        with self._lock:
            entries, self._all = self._all, []
        for _, conn in entries:
            conn.close()


def migrate(db):
    """Apply every migration newer than the file's PRAGMA user_version, in
       order. Idempotent: a current database is left untouched. Returns the
       resulting schema version."""
    have = db.execute("PRAGMA user_version").fetchone()[0]
    for version, mig in enumerate(_MIGRATIONS, start=1):
        if version > have:
            if callable(mig):
                mig(db)
            else:
                db.executescript(mig)
            db.execute(f"PRAGMA user_version={version}")
            db.commit()
    return db.execute("PRAGMA user_version").fetchone()[0]


# ===========================================================================
# Normalization & internal helpers
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
    """A claim id unique across the work_items and draft_tasks queues."""
    return uuid.uuid4().hex


# --- OAuth credential generation / hashing ---------------------------------

_USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"   # 20 unambiguous consonants


def _token():
    """A high-entropy opaque secret - a bearer token or a device code."""
    return secrets.token_urlsafe(32)


def _user_code():
    """A short, human-typable enrollment code, grouped as 'XXXX-XXXX'."""
    c = "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(8))
    return f"{c[:4]}-{c[4:]}"


def _norm_user_code(s):
    """Normalize an operator-typed user code - upper-cased, hyphen-grouped."""
    s = (s or "").strip().upper().replace(" ", "")
    if len(s) == 8 and "-" not in s:
        s = f"{s[:4]}-{s[4:]}"
    return s


def _hash(secret):
    """The stored form of a token / device code - the secret is never kept."""
    return hashlib.sha256(secret.encode()).hexdigest()


# ===========================================================================
# Patchsets
# ===========================================================================

def _refresh_patchset_fts(db, root_message_id):
    """Mirror one GATHERED patchset's searchable fields (subject, root
       author, submitter) into patchset_fts — called wherever those
       fields are written: upsert_patchset / mark_skipped (subject,
       submitter) and upsert_message for the root message (author).
       Uploaded/unknown roots are a no-op: the corpus search never sees
       them. Delete-then-insert keyed on the patchset_fts_map rowid
       (patchsets is WITHOUT ROWID — the map row's implicit rowid is
       the FTS docid), inside the caller's transaction. The author
       fields are NULL until the root message lands — the next refresh
       fills them."""
    root = norm_msgid(root_message_id)
    row = db.execute(
        "SELECT p.subject, rm.author_name, rm.author_email, "
        "p.submitter_email "
        "FROM patchsets p "
        "LEFT JOIN messages rm ON rm.message_id = p.root_message_id "
        "WHERE p.root_message_id=? AND p.origin=?",
        (root, PATCHSET_ORIGIN_GATHERED)).fetchone()
    if row is None:
        return
    rid = db.execute("SELECT rowid FROM patchset_fts_map "
                     "WHERE root_message_id=?", (root,)).fetchone()
    if rid is not None:
        rid = rid[0]
        db.execute("DELETE FROM patchset_fts WHERE rowid=?", (rid,))
    else:
        rid = db.execute("INSERT INTO patchset_fts_map (root_message_id) "
                         "VALUES (?)", (root,)).lastrowid
    db.execute(
        "INSERT INTO patchset_fts "
        "(rowid, subject, author_name, author_email, submitter_email) "
        "VALUES (?,?,?,?,?)",
        (rid, row["subject"], row["author_name"],
         row["author_email"], row["submitter_email"]))


def upsert_patchset(db, root_message_id, *, subject=None, submitter_email=None,
                    sent=None, n_patches=None, base_commit=None,
                    change_id=None, series_version=1, gathered_at=None,
                    origin=None, uploaded_by_user_id=None,
                    supersedes_root_message_id=None):
    """Insert a gathered patchset (idempotent on the root id); refresh the
       mutable fields if it is already known. Does not touch state/skip_reason
       - a re-gather never un-skips a patchset. `origin` defaults to
       PATCHSET_ORIGIN_GATHERED; the upload ingress passes UPLOADED plus
       the uploader's id. Both are insert-only: a conflict (the same
       series arriving via a second path) keeps the FIRST origin and
       uploader, so a later lore gather of an uploaded series can't strip
       the uploader's attribution. `supersedes_root_message_id` (the
       iteration link) is sticky on conflict — a refresh passing None
       never severs an existing chain. Returns the normalized
       root_message_id."""
    root = norm_msgid(root_message_id)
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,sent,"
        "n_patches,base_commit,change_id,series_version,state,gathered_at,"
        "origin,uploaded_by_user_id,supersedes_root_message_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(root_message_id) DO UPDATE SET "
        "subject=excluded.subject, submitter_email=excluded.submitter_email, "
        "sent=excluded.sent, n_patches=excluded.n_patches, "
        "base_commit=excluded.base_commit, change_id=excluded.change_id, "
        "series_version=excluded.series_version, "
        "supersedes_root_message_id=COALESCE("
        "  excluded.supersedes_root_message_id, "
        "  patchsets.supersedes_root_message_id)",
        (root, subject,
         norm_email(submitter_email) if submitter_email else None,
         sent, n_patches, base_commit, change_id, series_version,
         PATCHSET_STATE_GATHERED,
         gathered_at if gathered_at is not None else int(time.time()),
         origin if origin is not None else PATCHSET_ORIGIN_GATHERED,
         uploaded_by_user_id,
         norm_msgid(supersedes_root_message_id)
         if supersedes_root_message_id else None))
    _refresh_patchset_fts(db, root)
    db.commit()
    return root


def get_patchset(db, root_message_id):
    """The patchset row as a dict, or None."""
    row = db.execute("SELECT * FROM patchsets WHERE root_message_id=?",
                     (norm_msgid(root_message_id),)).fetchone()
    return dict(row) if row else None


def is_handled(db, root_message_id):
    """GATHER dedup gate: True once the corpus already has this patchset, in
       any state. The gather phase must not re-pull a handled patchset."""
    return db.execute("SELECT 1 FROM patchsets WHERE root_message_id=?",
                      (norm_msgid(root_message_id),)).fetchone() is not None


def mark_skipped(db, root_message_id, reason, *, subject=None):
    """Flag a patchset the corpus must skip (e.g. an unresolvable Date, or
       a list-tag the operator did not enable) - never message-stored, never
       fanned out for review. Creates the row if absent."""
    root = norm_msgid(root_message_id)
    db.execute("INSERT OR IGNORE INTO patchsets "
               "(root_message_id,subject,state,gathered_at) VALUES (?,?,?,?)",
               (root, subject, PATCHSET_STATE_SKIPPED, int(time.time())))
    db.execute("UPDATE patchsets SET state=?, skip_reason=? "
               "WHERE root_message_id=?",
               (PATCHSET_STATE_SKIPPED, reason, root))
    _refresh_patchset_fts(db, root)   # the INSERT path may carry a subject
    db.commit()
    # Notify anyone tracking this series (no-op for the usual unowned skip).
    try:
        notify_patchset_users(
            db, root, type=NOTIF_TYPE_PATCHSET_SKIPPED,
            dedup_key=f"skipped:{root}",
            title=f"Patchset skipped: {reason}")
    except Exception:
        log.warning("skip notification failed (non-fatal)", exc_info=True)


def list_patchsets(db, *, state=None, limit=200):
    """Patchsets, most recently gathered first, optionally filtered to one
       state."""
    if state is None:
        rows = db.execute(
            "SELECT * FROM patchsets ORDER BY gathered_at DESC LIMIT ?",
            (limit,))
    else:
        rows = db.execute(
            "SELECT * FROM patchsets WHERE state=? "
            "ORDER BY gathered_at DESC LIMIT ?", (state, limit))
    return [dict(r) for r in rows]


# Sortable columns for the web-UI patchset listing → the ORDER BY expression.
# Whitelisted so the caller's sort key never reaches the SQL string directly.
# `author` and `n_comments` are SELECT aliases (resolvable in ORDER BY).
# State is deliberately absent — it's a multi-flag column, not a single
# sortable value.
# Every sort key is an expression over the patchsets row alone (the
# v11 denormalisation) — any sort can run in the listing's inner
# filter/sort/LIMIT query without touching messages.
PATCHSET_SORT_COLUMNS = {
    "date":     "p.sent",
    "subject":  "p.subject",
    "author":   ("COALESCE(p.root_author_name, p.root_author_email, "
                 "p.submitter_email)"),
    "parts":    "p.n_parts",
    "comments": "p.n_comments",
}

# Lifecycle "flag" predicates for a patchset, as correlated EXISTS subqueries
# over `p` (the patchsets row): has it been Prepared (metadata written),
# Reviewed (an ai_review exists), or used for Training (session history).
# Static SQL — no user input — so safe to interpolate; reused both to derive
# the listing's per-row flags and to back the same-named state filters.
_PATCHSET_PREPARED = ("EXISTS (SELECT 1 FROM patchset_metadata pm "
                      "WHERE pm.root_message_id = p.root_message_id)")
_PATCHSET_REVIEWED = ("EXISTS (SELECT 1 FROM ai_reviews ar "
                      "WHERE ar.root_message_id = p.root_message_id)")
_PATCHSET_TRAINING = ("EXISTS (SELECT 1 FROM patchset_session_history ph "
                      "WHERE ph.root_message_id = p.root_message_id)")

# Whether the patchset's thread drew at least one comment message — an
# independent filter axis from the lifecycle flags above. Reads the
# v11 denormalised counter, not the messages table.
_PATCHSET_HAS_COMMENTS = "p.n_comments > 0"

# State-filter key → its WHERE predicate. The lifecycle flags are not
# mutually exclusive (a patchset can be prepared AND reviewed AND trained);
# "skipped" is the one base-state filter. ("gathered" isn't offered — every
# patchset in the corpus has been gathered, so it carries no information.)
_PATCHSET_FILTER_CLAUSES = {
    "prepared": _PATCHSET_PREPARED,
    "reviewed": _PATCHSET_REVIEWED,
    "training": _PATCHSET_TRAINING,
    "skipped":  f"p.state = {PATCHSET_STATE_SKIPPED}",
}


def _patchset_list_where(q, state, comments, list_tag, patch_type):
    """Shared WHERE clause + params for the patchset listing and its count.
       Every argument is an independent, AND-composed filter axis:
         q          partial subject OR author (name/email, case-insensitive)
         state      a key from _PATCHSET_FILTER_CLAUSES (prepared / reviewed /
                    training / skipped) or None
         comments   "with" → thread carries ≥1 comment
         list_tag   patchset is tagged with this mailing list
         patch_type prepare-derived primary patch type (bugfix / feature / …)

       The listing is the CORPUS view, so uploaded patchsets are excluded
       unconditionally — they are submissions, browsed on /my-patchsets,
       not corpus rows to search / filter / train against.

       Every clause reads the patchsets row alone (author fields are
       the v11 denormalised columns). Search runs against patchset_fts
       (trigram FTS5 — substring semantics at index speed, O(matches)
       not O(corpus)); a term under 3 chars can't produce a trigram,
       so it keeps a LIKE scan over the same columns."""
    clauses = [f"p.origin = {PATCHSET_ORIGIN_GATHERED}"]
    params = []
    if q and len(q) >= 3:
        # The term is matched as one quoted FTS5 phrase — internal
        # quotes doubled — so user-typed syntax (*, OR, parens) is
        # literal text, never a query-parse error.
        clauses.append("p.root_message_id IN "
                       "(SELECT root_message_id FROM patchset_fts_map "
                       " WHERE rowid IN (SELECT rowid FROM patchset_fts "
                       "  WHERE patchset_fts MATCH ?))")
        params.append('"' + q.replace('"', '""') + '"')
    elif q:
        like = f"%{q}%"
        clauses.append("(p.subject LIKE ? OR p.root_author_name LIKE ? "
                       "OR p.root_author_email LIKE ? "
                       "OR p.submitter_email LIKE ?)")
        params += [like, like, like, like]
    if state in _PATCHSET_FILTER_CLAUSES:
        clauses.append(_PATCHSET_FILTER_CLAUSES[state])
    if comments == "with":
        clauses.append(_PATCHSET_HAS_COMMENTS)
    if list_tag:
        clauses.append("EXISTS (SELECT 1 FROM patchset_tags pt "
                       "WHERE pt.root_message_id = p.root_message_id "
                       "AND pt.tag = ?)")
        params.append(list_tag)
    if patch_type:
        clauses.append("EXISTS (SELECT 1 FROM patchset_metadata pmt "
                       "WHERE pmt.root_message_id = p.root_message_id "
                       "AND json_extract(pmt.patch_type, '$.primary') = ?)")
        params.append(patch_type)
    where = " WHERE " + " AND ".join(clauses)
    return where, params


def list_user_patchsets(db, *, user_id=None):
    """The /my-patchsets dashboard rows — the user's uploads BLENDED
       with the gathered series they claimed, newest first (None = the
       admin's everyone view: every upload plus every claimed series).
       Each row carries `origin` (the "from lore" badge) and the
       pipeline facts the status chip derives from: has_metadata /
       has_ai_review plus the latest prepare and review work-item
       states (NULL when none exists).

       Iteration chains collapse to one row: only chain HEADS (rows no
       other row supersedes) are returned, each carrying `iterations`
       (the chain length — 1 for an unchained row). Superseded
       iterations stay reachable through the head's detail page."""
    sql = ("SELECT p.root_message_id, p.subject, p.n_patches, "
           "p.series_version, p.base_commit, p.gathered_at, p.origin, "
           "p.uploaded_by_user_id, p.supersedes_root_message_id, "
           "(SELECT c.user_id FROM patchset_claims c "
           " WHERE c.root_message_id=p.root_message_id "
           " ORDER BY c.claimed_at, c.user_id LIMIT 1) "
           " AS first_claimant_user_id, "
           f"{_PATCHSET_PREPARED} AS has_metadata, "
           f"{_PATCHSET_REVIEWED} AS has_ai_review, "
           "(SELECT w.state FROM work_items w "
           " WHERE w.root_message_id=p.root_message_id AND w.type=? "
           " ORDER BY w.id DESC LIMIT 1) AS prepare_state, "
           "(SELECT w.state FROM work_items w "
           " WHERE w.root_message_id=p.root_message_id AND w.type=? "
           " ORDER BY w.id DESC LIMIT 1) AS review_state "
           "FROM patchsets p ")
    params = [WORK_ITEM_TYPE_PREPARE, WORK_ITEM_TYPE_REVIEW]
    if user_id is not None:
        sql += ("WHERE (p.origin=? AND p.uploaded_by_user_id=?) "
                "OR EXISTS (SELECT 1 FROM patchset_claims c "
                "  WHERE c.root_message_id=p.root_message_id "
                "  AND c.user_id=?) ")
        params += [PATCHSET_ORIGIN_UPLOADED, user_id, user_id]
    else:
        sql += ("WHERE p.origin=? OR EXISTS "
                "(SELECT 1 FROM patchset_claims c "
                "  WHERE c.root_message_id=p.root_message_id) ")
        params.append(PATCHSET_ORIGIN_UPLOADED)
    sql += "ORDER BY p.gathered_at DESC, p.root_message_id"
    rows = [dict(r) for r in db.execute(sql, params)]
    # Chains never cross owners (linking is offered only against the
    # uploader's own rows), so even the scoped view holds every member
    # of its chains and the walk below stays complete; a missing link
    # target simply ends the count early (the `nxt in by_root` guard).
    by_root = {r["root_message_id"]: r for r in rows}
    superseded = {r["supersedes_root_message_id"] for r in rows
                  if r["supersedes_root_message_id"]}
    heads = []
    for r in rows:
        if r["root_message_id"] in superseded:
            continue
        n, cur = 1, r
        while ((nxt := cur["supersedes_root_message_id"])
               and nxt in by_root and n <= len(rows)):  # cycle guard
            cur = by_root[nxt]
            n += 1
        r["iterations"] = n
        heads.append(r)
    return heads


# ===========================================================================
# Notifications  (per-user in-app feed; migration v13)
# ===========================================================================

def _notification_prefs(db, user_id):
    """A user's notification_prefs as a {type-slug: bool} dict, or {} when
       unset / unparseable. A missing slug means that type is ON (default)."""
    row = db.execute("SELECT notification_prefs FROM users WHERE id=?",
                     (user_id,)).fetchone()
    if row is None or row["notification_prefs"] is None:
        return {}
    try:
        prefs = json.loads(row["notification_prefs"])
        return prefs if isinstance(prefs, dict) else {}
    except (ValueError, TypeError):
        return {}


def get_notification_prefs(db, user_id):
    """Public read of a user's notification preference map (slug -> bool)."""
    return _notification_prefs(db, user_id)


def set_notification_prefs(db, user_id, prefs):
    """Persist a user's preference map. Only known type slugs are kept."""
    known = set(NOTIF_TYPE_NAMES.values())
    clean = {k: bool(v) for k, v in (prefs or {}).items() if k in known}
    db.execute("UPDATE users SET notification_prefs=? WHERE id=?",
               (json.dumps(clean), user_id))
    db.commit()


def _wants_notification(db, user_id, type):
    """True unless the user has explicitly opted out of `type`."""
    return _notification_prefs(db, user_id).get(NOTIF_TYPE_NAMES[type], True)


def insert_notification(db, user_id, *, type, dedup_key, title, link=None,
                        root_message_id=None, payload=None, commit=True):
    """Insert one notification for `user_id`, deduped on (user_id, dedup_key)
       and gated by the user's preferences. No-op (0) when user_id is None, the
       user opted out of `type`, or the (user, key) already exists. Returns the
       rows inserted (0 or 1). `commit=False` lets a fan-out batch then commit."""
    if user_id is None or not _wants_notification(db, user_id, type):
        return 0
    cur = db.execute(
        "INSERT OR IGNORE INTO notifications "
        "(user_id,type,title,link,root_message_id,dedup_key,payload,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, type, title, link,
         norm_msgid(root_message_id) if root_message_id else None,
         dedup_key, json.dumps(payload) if payload is not None else None,
         int(time.time())))
    if commit:
        db.commit()
    return cur.rowcount


def _patchset_user_ids(db, root):
    """The user_ids tracking a patchset: its uploader (origin=UPLOADED) ∪
       everyone who claimed it — the /my-patchsets blend."""
    rows = db.execute(
        "SELECT uploaded_by_user_id AS uid FROM patchsets "
        "  WHERE root_message_id=? AND origin=? AND uploaded_by_user_id IS NOT NULL "
        "UNION "
        "SELECT user_id AS uid FROM patchset_claims WHERE root_message_id=?",
        (root, PATCHSET_ORIGIN_UPLOADED, root)).fetchall()
    return {r["uid"] for r in rows if r["uid"] is not None}


def notify_patchset_users(db, root_message_id, *, type, dedup_key, title,
                          link=None, exclude_user_id=None):
    """Fan a patchset event out to its tracking users (uploader ∪ claimants),
       minus `exclude_user_id` (the actor). Deduped + pref-gated per user.
       No-op for an unowned patchset (the common case): one indexed UNION
       query, no inserts. Returns the count inserted."""
    root = norm_msgid(root_message_id)
    link = link or f"/patchsets/{_urlquote(root)}"   # patchset detail page
    n = 0
    for uid in _patchset_user_ids(db, root) - {exclude_user_id}:
        n += insert_notification(db, uid, type=type, dedup_key=dedup_key,
                                 title=title, link=link, root_message_id=root,
                                 commit=False)
    db.commit()
    return n


def notify_admins(db, *, type, dedup_key, title, link=None):
    """Fan an event out to every approved admin user. Deduped + pref-gated."""
    n = 0
    for r in db.execute("SELECT id FROM users WHERE is_admin=1 AND state=?",
                        ("approved",)).fetchall():
        n += insert_notification(db, r["id"], type=type, dedup_key=dedup_key,
                                 title=title, link=link, commit=False)
    db.commit()
    return n


def unread_notification_count(db, user_id):
    if user_id is None:
        return 0
    return db.execute(
        "SELECT count(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
        (user_id,)).fetchone()[0]


def list_notifications(db, user_id, *, limit=50, unread_only=False):
    """A user's notifications, newest first (type kept as int; payload decoded)."""
    if user_id is None:
        return []
    where = "WHERE user_id=?" + (" AND read_at IS NULL" if unread_only else "")
    out = []
    for row in db.execute(
            f"SELECT * FROM notifications {where} "
            f"ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit)).fetchall():
        d = dict(row)
        if d.get("payload") is not None:
            try:
                d["payload"] = json.loads(d["payload"])
            except (ValueError, TypeError):
                d["payload"] = None
        out.append(d)
    return out


def get_notification(db, user_id, notif_id):
    """One notification row (dict) scoped to its owner, or None."""
    if user_id is None:
        return None
    row = db.execute("SELECT * FROM notifications WHERE id=? AND user_id=?",
                     (notif_id, user_id)).fetchone()
    return dict(row) if row else None


def mark_notification_read(db, user_id, notif_id):
    """Mark one notification read — user-scoped so a user can't touch another's.
       Idempotent. Returns True when a row moved to read."""
    cur = db.execute(
        "UPDATE notifications SET read_at=? "
        "WHERE id=? AND user_id=? AND read_at IS NULL",
        (int(time.time()), notif_id, user_id))
    db.commit()
    return cur.rowcount > 0


def mark_all_notifications_read(db, user_id):
    cur = db.execute(
        "UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
        (int(time.time()), user_id))
    db.commit()
    return cur.rowcount


def prune_read_notifications(db, user_id, *, keep=200):
    """Delete a user's READ notifications beyond the newest `keep`; unread are
       always retained. Keeps the feed bounded — called from the mark-read
       paths."""
    db.execute(
        "DELETE FROM notifications WHERE user_id=? AND read_at IS NOT NULL "
        "AND id NOT IN (SELECT id FROM notifications "
        "  WHERE user_id=? AND read_at IS NOT NULL "
        "  ORDER BY created_at DESC, id DESC LIMIT ?)",
        (user_id, user_id, keep))
    db.commit()


def claim_patchset(db, root_message_id, user_id, *, supersedes=None):
    """Associate a GATHERED patchset with user_id — cooperative: any
       number of developers may hold a claim on the same series, so
       this is an idempotent insert, not a race. Returns True when THIS
       call added the claim (False: already held by this user, or the
       row is uploaded/unknown — uploads have an owner already).

       `supersedes` links the claimed series as the next iteration —
       stamped only while the shared pointer is still unset (the link
       is a fact about the series, first linker wins; per-user chains
       over a shared prior live on the CHILD rows instead)."""
    root = norm_msgid(root_message_id)
    cur = db.execute(
        "INSERT OR IGNORE INTO patchset_claims "
        "(root_message_id, user_id, claimed_at) "
        "SELECT root_message_id, ?, ? FROM patchsets "
        "WHERE root_message_id=? AND origin=?",
        (user_id, int(time.time()), root, PATCHSET_ORIGIN_GATHERED))
    took = cur.rowcount == 1
    if took and supersedes:
        db.execute(
            "UPDATE patchsets SET supersedes_root_message_id=? "
            "WHERE root_message_id=? AND supersedes_root_message_id "
            "IS NULL",
            (norm_msgid(supersedes), root))
    db.commit()
    return took


def unclaim_patchset(db, root_message_id, user_id=None):
    """Release claims: one user's own (the undo), or every claim on the
       series when user_id is None (the maintainer / admin revoke).
       Returns True when something was actually released."""
    sql = "DELETE FROM patchset_claims WHERE root_message_id=?"
    params = [norm_msgid(root_message_id)]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    cur = db.execute(sql, params)
    db.commit()
    return cur.rowcount > 0


def patchset_claimants(db, root_message_id):
    """Who holds claims on this series — (user_id, email, claimed_at),
       earliest first. Each viewer's UI shows only their own claim; the
       full list is for maintainers / admins."""
    return [dict(r) for r in db.execute(
        "SELECT c.user_id, u.email, c.claimed_at "
        "FROM patchset_claims c JOIN users u ON u.id=c.user_id "
        "WHERE c.root_message_id=? "
        "ORDER BY c.claimed_at, c.user_id",
        (norm_msgid(root_message_id),))]


def user_has_claim(db, root_message_id, user_id):
    """Whether user_id holds a claim on this series."""
    return db.execute(
        "SELECT 1 FROM patchset_claims "
        "WHERE root_message_id=? AND user_id=?",
        (norm_msgid(root_message_id), user_id)).fetchone() is not None


def claimable_patchsets(db, user_id, submitter_email, *, limit=10):
    """Gathered chain-head patchsets this user hasn't claimed whose
       submitter_email matches their account email (case-insensitive) —
       the /my-patchsets "series on lore that look like yours"
       suggestions, newest first. The address match is a SUGGESTION
       heuristic only, never a claim gate. A head here means no GATHERED
       successor — another developer's private upload chain must not
       hide a lore series from this user's suggestions."""
    if not submitter_email:
        return []
    return [dict(r) for r in db.execute(
        "SELECT root_message_id, subject, n_patches, series_version, "
        "change_id, gathered_at FROM patchsets p "
        "WHERE p.origin=? "
        "AND p.submitter_email=? COLLATE NOCASE "
        "AND NOT EXISTS (SELECT 1 FROM patchset_claims c "
        "  WHERE c.root_message_id=p.root_message_id AND c.user_id=?) "
        "AND NOT EXISTS (SELECT 1 FROM patchsets q "
        "  WHERE q.supersedes_root_message_id=p.root_message_id "
        "  AND q.origin=?) "
        "ORDER BY p.gathered_at DESC, p.root_message_id LIMIT ?",
        (PATCHSET_ORIGIN_GATHERED, submitter_email, user_id,
         PATCHSET_ORIGIN_GATHERED, limit))]


def unsuperseded_user_series(db, user_id):
    """This developer's chain heads — uploads AND claimed gathered
       series — newest first. The candidates an incoming series (a
       fresh upload, or a claim being linked) may be a new iteration of
       (upload.find_prior_iteration matches against these by Change-Id,
       then series title). Chains may cross the origin seam: an upload
       superseding the claimed lore copy it iterates on is exactly the
       post-review loop.

       "Head" is judged per user: a row stops being a candidate only
       when superseded by one of THIS user's own rows (their upload or
       a series they claimed) — another developer privately hanging
       their v2 off the same shared lore series must not consume it."""
    return [dict(r) for r in db.execute(
        "SELECT root_message_id, subject, change_id, gathered_at "
        "FROM patchsets p "
        "WHERE ((p.origin=? AND p.uploaded_by_user_id=?) "
        "  OR EXISTS (SELECT 1 FROM patchset_claims c "
        "    WHERE c.root_message_id=p.root_message_id "
        "    AND c.user_id=?)) "
        "AND NOT EXISTS (SELECT 1 FROM patchsets q "
        "  WHERE q.supersedes_root_message_id=p.root_message_id "
        "  AND ((q.origin=? AND q.uploaded_by_user_id=?) "
        "    OR EXISTS (SELECT 1 FROM patchset_claims c2 "
        "      WHERE c2.root_message_id=q.root_message_id "
        "      AND c2.user_id=?))) "
        "ORDER BY p.gathered_at DESC, p.root_message_id",
        (PATCHSET_ORIGIN_UPLOADED, user_id, user_id,
         PATCHSET_ORIGIN_UPLOADED, user_id, user_id))]


def cancel_unheld_pipeline_items(db, root_message_id):
    """Delete a superseded iteration's claimable / deferred prepare and
       review work-items, so nodes don't spend tokens on work the
       uploader has already replaced. Claimed in-flight items keep their
       lease and finish (their result lands on the old iteration as
       history); completed items are untouched. Returns the number
       removed."""
    root = norm_msgid(root_message_id)
    cur = db.execute(
        "DELETE FROM work_items WHERE root_message_id=? "
        "AND type IN (?,?) AND state IN (?,?)",
        (root, WORK_ITEM_TYPE_PREPARE, WORK_ITEM_TYPE_REVIEW,
         WORK_ITEM_STATE_CLAIMABLE, WORK_ITEM_STATE_DEFERRED))
    db.commit()
    if cur.rowcount:
        log.info("cancel_unheld_pipeline_items: patchset %s — removed %d "
                 "queued item(s) for the superseded iteration",
                 root, cur.rowcount)
    return cur.rowcount


def distinct_patchset_tags(db):
    """The distinct list tags actually carried by patchsets — the options for
       the listing's mailing-list filter."""
    return [r[0] for r in db.execute(
        "SELECT DISTINCT tag FROM patchset_tags ORDER BY tag")]


def distinct_patch_types(db):
    """The distinct prepare-derived primary patch types present in the corpus
       — the options for the listing's patch-type filter."""
    return [r[0] for r in db.execute(
        "SELECT DISTINCT json_extract(patch_type, '$.primary') t "
        "FROM patchset_metadata WHERE t IS NOT NULL ORDER BY t")]


def count_patchsets(db, *, q=None, state=None, comments=None, list_tag=None,
                    patch_type=None):
    """Total patchsets matching the listing's search + filter axes — the
       pager's denominator. Reads patchsets (and, for a search, the FTS
       index) only — never messages: the author fields it once joined
       for are denormalised onto the row (v11), and probing the
       messages PK once per gathered patchset had cost ~165ms at 11k
       rows on every page-load."""
    where, params = _patchset_list_where(q, state, comments, list_tag,
                                         patch_type)
    return db.execute(
        "SELECT count(*) FROM patchsets p" + where,
        params).fetchone()[0]


def list_patchsets_page(db, *, q=None, state=None, comments=None,
                        list_tag=None, patch_type=None, sort="date",
                        direction="desc", limit=25, offset=0):
    """One page of the web-UI patchset listing. Each row carries the table's
       display fields: `author` (the root message's name, falling back to its
       email then the patchset's submitter_email), `n_comments` (the thread's
       comment messages), `n_parts` (the patch messages actually attached —
       what the corpus holds, not the `[PATCH N/M]` series total in
       `n_patches`), and the lifecycle flags `is_prepared` / `is_reviewed` /
       `is_training` (0/1); `n_patches` + `state` ride on the patchset row.

       `sort` is one of PATCHSET_SORT_COLUMNS (anything else → date);
       `direction` is asc/desc. Results are stably tie-broken by sent DESC
       then root id so paging is deterministic.

       Two-phase shape: the inner query filters, sorts and LIMITs the
       patchsets rows; the outer query attaches the lifecycle EXISTS
       flags to just the emitted page. SQLite materialises the
       select-list into the sorter BEFORE applying LIMIT, so a
       single-query form would evaluate them for every gathered row on
       every page load. Everything else the listing shows — author,
       n_parts, n_comments — is denormalised ON the row (v11), which is
       what lets every sort key run in the inner query and keeps the
       whole listing path off the messages table: O(page), not
       O(corpus), in row width as well as count."""
    col = PATCHSET_SORT_COLUMNS.get(sort, PATCHSET_SORT_COLUMNS["date"])
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    where, params = _patchset_list_where(q, state, comments, list_tag,
                                         patch_type)
    order = (f" ORDER BY {col} {direction}, p.sent DESC, p.root_message_id ")
    rows = db.execute(
        "SELECT p.*, "
        "COALESCE(p.root_author_name, p.root_author_email, "
        "         p.submitter_email) AS author, "
        f"{_PATCHSET_PREPARED} AS is_prepared, "
        f"{_PATCHSET_REVIEWED} AS is_reviewed, "
        f"{_PATCHSET_TRAINING} AS is_training "
        "FROM (SELECT p.* FROM patchsets p" + where + order +
        "  LIMIT ? OFFSET ?) AS p"
        + order,
        [*params, limit, offset])
    return [dict(r) for r in rows]


# ===========================================================================
# Messages  (every thread email: cover, patch, comment)
# ===========================================================================

def upsert_message(db, message_id, *, root_message_id, type, body,
                   part_index=None, parent_message_id=None,
                   author_name=None, author_email=None, subject=None,
                   sent=None):
    """Insert a thread email (idempotent on message_id); refresh the mutable
       fields if it is already known. `type` is a MSG_TYPE_*."""
    db.execute(
        "INSERT INTO messages (message_id,root_message_id,type,part_index,"
        "parent_message_id,author_name,author_email,subject,sent,body) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id) DO UPDATE SET "
        "root_message_id=excluded.root_message_id, type=excluded.type, "
        "part_index=excluded.part_index, "
        "parent_message_id=excluded.parent_message_id, "
        "author_name=excluded.author_name, "
        "author_email=excluded.author_email, subject=excluded.subject, "
        "sent=excluded.sent, body=excluded.body",
        (norm_msgid(message_id), norm_msgid(root_message_id), type, part_index,
         norm_msgid(parent_message_id) if parent_message_id else None,
         author_name,
         norm_email(author_email) if author_email else None,
         subject, sent, body))
    # Keep the patchsets row's denormalised listing fields current —
    # the corpus listing never touches this (large) table. Recomputed,
    # not incremented: an upsert may change an existing message's type.
    root = norm_msgid(root_message_id)
    db.execute(
        "UPDATE patchsets SET "
        "n_parts=(SELECT count(*) FROM messages m "
        "  WHERE m.root_message_id=? AND m.type=?), "
        "n_comments=(SELECT count(*) FROM messages m "
        "  WHERE m.root_message_id=? AND m.type=?) "
        "WHERE root_message_id=?",
        (root, MSG_TYPE_PATCH, root, MSG_TYPE_COMMENT, root))
    # The ROOT message carries the author fields the listing displays
    # and the corpus search indexes — landing (or re-landing) it
    # updates the denormalised author and refreshes the FTS mirror.
    if norm_msgid(message_id) == root:
        db.execute(
            "UPDATE patchsets SET root_author_name=?, root_author_email=? "
            "WHERE root_message_id=?",
            (author_name,
             norm_email(author_email) if author_email else None, root))
        _refresh_patchset_fts(db, root)
    db.commit()


def messages_for_patchset(db, root_message_id, *, type=None):
    """Every message in a patchset, oldest first; optionally filtered to one
       MSG_TYPE_*."""
    root = norm_msgid(root_message_id)
    if type is None:
        rows = db.execute(
            "SELECT * FROM messages WHERE root_message_id=? "
            "ORDER BY sent, message_id", (root,))
    else:
        rows = db.execute(
            "SELECT * FROM messages WHERE root_message_id=? AND type=? "
            "ORDER BY sent, message_id", (root, type))
    return [dict(r) for r in rows]


def patch_message(db, root_message_id, part_index=None):
    """The patch message in a patchset at the given part_index, or the lone
       `[PATCH]` row when part_index is None. Returns a dict or None."""
    root = norm_msgid(root_message_id)
    if part_index is None:
        row = db.execute(
            "SELECT * FROM messages WHERE root_message_id=? AND type=? "
            "AND part_index IS NULL", (root, MSG_TYPE_PATCH)).fetchone()
    else:
        row = db.execute(
            "SELECT * FROM messages WHERE root_message_id=? AND type=? "
            "AND part_index=?",
            (root, MSG_TYPE_PATCH, part_index)).fetchone()
    return dict(row) if row else None


def comments_for_patch(db, message_id):
    """All review-comment messages whose parent is `message_id`, oldest first.
       (The parent is resolved up the In-Reply-To chain at gather time, so
       this returns everything in that patch's discussion.)"""
    return [dict(r) for r in db.execute(
        "SELECT * FROM messages WHERE parent_message_id=? AND type=? "
        "ORDER BY sent, message_id",
        (norm_msgid(message_id), MSG_TYPE_COMMENT))]


# ===========================================================================
# AI reviews  (hone-node's structured `concerns` review of a patchset)
# ===========================================================================

def upsert_ai_review(db, root_message_id, *, concerns,
                     source=AI_REVIEW_SOURCE_HONE_NODE,
                     model=None, input_tokens=None, output_tokens=None,
                     reviewed_at=None, methodology_version=None, node_id=None,
                     meta=None, check_coverage=None):
    """Insert (or replace) the structured AI review of a patchset. `concerns`
       is a dict ({"concerns": [...]}); it is JSON-encoded. One row per
       (patchset, source). `check_coverage` is the per-check applicable/fired
       denominator (core.check_gates), JSON-encoded; None leaves the column
       NULL — recomputable later. Returns the row id."""
    root = norm_msgid(root_message_id)
    now = int(time.time())
    db.execute(
        "INSERT INTO ai_reviews (root_message_id,source,concerns,model,"
        "input_tokens,output_tokens,reviewed_at,methodology_version,node_id,"
        "meta,check_coverage,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(root_message_id,source) DO UPDATE SET "
        "concerns=excluded.concerns, model=excluded.model, "
        "input_tokens=excluded.input_tokens, "
        "output_tokens=excluded.output_tokens, "
        "reviewed_at=excluded.reviewed_at, "
        "methodology_version=excluded.methodology_version, "
        "node_id=excluded.node_id, meta=excluded.meta, "
        "check_coverage=excluded.check_coverage, "
        "recorded_at=excluded.recorded_at",
        (root, source, json.dumps(concerns), model, input_tokens,
         output_tokens, reviewed_at if reviewed_at is not None else now,
         methodology_version, node_id,
         json.dumps(meta) if meta is not None else None,
         json.dumps(check_coverage) if check_coverage is not None else None,
         now))
    db.commit()
    return db.execute(
        "SELECT id FROM ai_reviews WHERE root_message_id=? AND source=?",
        (root, source)).fetchone()["id"]


def get_ai_review(db, root_message_id, *,
                  source=AI_REVIEW_SOURCE_HONE_NODE):
    """The AI review row as a dict (with `concerns` / `meta` /
       `check_coverage` decoded), or None."""
    row = db.execute(
        "SELECT * FROM ai_reviews WHERE root_message_id=? AND source=?",
        (norm_msgid(root_message_id), source)).fetchone()
    if row is None:
        return None
    r = dict(row)
    r["concerns"] = json.loads(r["concerns"])
    if r["meta"] is not None:
        r["meta"] = json.loads(r["meta"])
    if r.get("check_coverage") is not None:
        r["check_coverage"] = json.loads(r["check_coverage"])
    return r


def ai_review_concerns_for_roots(db, roots, *,
                                 source=AI_REVIEW_SOURCE_HONE_NODE):
    """Map root_message_id -> decoded concerns list, for the given roots that
       carry an AI review — the listing 'concerns' column, fetched for one page
       of roots in a single indexed query (idx_ai_reviews_root). A root ABSENT
       from the map has no review (the listing leaves the column blank); a root
       mapping to [] was reviewed with no concerns ('No concerns found')."""
    norm = [norm_msgid(r) for r in roots]
    if not norm:
        return {}
    marks = ",".join("?" * len(norm))
    out = {}
    for r in db.execute(
            f"SELECT root_message_id, concerns FROM ai_reviews "
            f"WHERE source=? AND root_message_id IN ({marks})",
            (source, *norm)):
        try:
            out[r["root_message_id"]] = json.loads(r["concerns"])
        except (ValueError, TypeError):
            out[r["root_message_id"]] = []
    return out


def delete_review(db, root_message_id):
    """Operator-triggered removal of a patchset's AI review together with
       the review work-item(s) that produced it — so the operator can wipe
       a bad review and request a fresh one. Deletes, in FK order: any
       review_evaluations that reference the review rows (foreign_keys is
       ON, no cascade), then the ai_reviews row(s) for this patchset (all
       sources), then its WORK_ITEM_TYPE_REVIEW work-items.

       Keyed on root_message_id (the operator acts from the patchset page).
       Returns 'ok' when an ai_review was removed, or 'unknown' when the
       patchset has no ai_review — an idempotent no-op, so a double-click
       is safe."""
    root = norm_msgid(root_message_id)
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM ai_reviews WHERE root_message_id=?", (root,))]
    if not ids:
        return "unknown"
    log.info("delete_review: patchset %s — removing %d ai_review(s) "
             "and its review work-items", root, len(ids))
    marks = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM review_evaluations WHERE ai_review_id IN ({marks})",
               ids)
    db.execute("DELETE FROM ai_reviews WHERE root_message_id=?", (root,))
    db.execute("DELETE FROM work_items WHERE root_message_id=? AND type=?",
               (root, WORK_ITEM_TYPE_REVIEW))
    db.commit()
    return "ok"


def reset_patchset_pipeline(db, root_message_id):
    """Drop everything the pipeline derived from a patchset's bodies — the
       AI review (evaluations + review work-items, via delete_review) and
       prepare's product (the patchset_metadata row + its prepare
       work-items). For re-uploads that change patch content: derived
       artifacts must never outlive the bodies they were computed from.
       The caller restarts the pipeline (maybe_enqueue_prepare)."""
    root = norm_msgid(root_message_id)
    log.info("reset_patchset_pipeline: patchset %s — dropping prepared "
             "metadata, AI review and pipeline work-items", root)
    delete_review(db, root)
    db.execute("DELETE FROM patchset_metadata WHERE root_message_id=?",
               (root,))
    db.execute("DELETE FROM work_items WHERE root_message_id=? AND type=?",
               (root, WORK_ITEM_TYPE_PREPARE))
    db.commit()


def delete_patchset(db, root_message_id):
    """Remove a patchset outright: the row, its thread messages, every
       work-item, the prepare metadata and the AI review (evaluations
       included, via delete_review). The uploader-facing cleanup for
       abandoned upload iterations — POLICY (uploaded-origin only,
       _can_act_on_patchset) lives with the caller; this is mechanics.
       An iteration chain through the row is spliced (whoever supersedes
       it re-points at what it superseded), so chains stay linear and
       the self-FK intact. Training-session references are NOT touched:
       uploaded patchsets never enter sessions, and an FK failure here
       means that invariant broke — better loud than a silent cascade.
       Returns 'ok', or 'unknown' for an idempotent miss."""
    root = norm_msgid(root_message_id)
    ps = db.execute(
        "SELECT supersedes_root_message_id FROM patchsets "
        "WHERE root_message_id=?", (root,)).fetchone()
    if ps is None:
        return "unknown"
    log.info("delete_patchset: %s — removing thread, work-items and "
             "derived artifacts", root)
    delete_review(db, root)
    db.execute("DELETE FROM patchset_metadata WHERE root_message_id=?",
               (root,))
    # Remaining work-items reference messages — they go first; then the
    # messages' self-FK (parent_message_id, no ON DELETE) is cleared so
    # the bulk delete can't trip on row order.
    db.execute("DELETE FROM work_items WHERE root_message_id=?", (root,))
    db.execute("UPDATE messages SET parent_message_id=NULL "
               "WHERE root_message_id=?", (root,))
    db.execute("DELETE FROM messages WHERE root_message_id=?", (root,))
    db.execute("DELETE FROM patchset_tags WHERE root_message_id=?", (root,))
    db.execute("UPDATE patchsets SET supersedes_root_message_id=? "
               "WHERE supersedes_root_message_id=?",
               (ps["supersedes_root_message_id"], root))
    # Defensive: uploaded rows are never IN the search index, but a
    # stale map/FTS pair must not linger if one ever appeared.
    db.execute("DELETE FROM patchset_fts WHERE rowid="
               "(SELECT rowid FROM patchset_fts_map WHERE root_message_id=?)",
               (root,))
    db.execute("DELETE FROM patchset_fts_map WHERE root_message_id=?",
               (root,))
    db.execute("DELETE FROM patchsets WHERE root_message_id=?", (root,))
    db.commit()
    return "ok"


# ===========================================================================
# Patchset metadata  (the prepare task's structured output; corpus group)
# ===========================================================================

_PATCHSET_METADATA_FIELDS = ("tree_state", "subsystem", "patch_size",
                             "maintainer", "patch_type", "review_intensity",
                             "preparation_notes")


def upsert_patchset_metadata(db, root_message_id, *, mode,
                             methodology_version=None,
                             node_tree_revision=None, **fields):
    """Write or overwrite the patchset_metadata row for a patchset. `mode`
       is 'authoritative' | 'heuristic' | 'mixed'; the seven structured
       fields (tree_state, subsystem, patch_size, maintainer, patch_type,
       review_intensity, preparation_notes) must be passed as keyword
       arguments holding the JSON-serialisable dicts produced by the
       prepare task."""
    if mode not in ("authoritative", "heuristic", "mixed"):
        raise ValueError(f"bad mode: {mode!r}")
    missing = [f for f in _PATCHSET_METADATA_FIELDS if f not in fields]
    if missing:
        raise ValueError(f"missing metadata fields: {missing}")
    root = norm_msgid(root_message_id)
    db.execute(
        "INSERT INTO patchset_metadata "
        "(root_message_id,methodology_version,node_tree_revision,mode,"
        "tree_state,subsystem,patch_size,maintainer,patch_type,"
        "review_intensity,preparation_notes,prepared_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(root_message_id) DO UPDATE SET "
        "methodology_version=excluded.methodology_version, "
        "node_tree_revision=excluded.node_tree_revision, "
        "mode=excluded.mode, tree_state=excluded.tree_state, "
        "subsystem=excluded.subsystem, patch_size=excluded.patch_size, "
        "maintainer=excluded.maintainer, patch_type=excluded.patch_type, "
        "review_intensity=excluded.review_intensity, "
        "preparation_notes=excluded.preparation_notes, "
        "prepared_at=excluded.prepared_at",
        (root, methodology_version, node_tree_revision, mode,
         json.dumps(fields["tree_state"]),
         json.dumps(fields["subsystem"]),
         json.dumps(fields["patch_size"]),
         json.dumps(fields["maintainer"]),
         json.dumps(fields["patch_type"]),
         json.dumps(fields["review_intensity"]),
         json.dumps(fields["preparation_notes"]),
         int(time.time())))
    db.commit()


def get_patchset_metadata(db, root_message_id):
    """The patchset_metadata row as a dict with JSON fields decoded, or
       None if the patchset has not been prepared yet."""
    row = db.execute(
        "SELECT * FROM patchset_metadata WHERE root_message_id=?",
        (norm_msgid(root_message_id),)).fetchone()
    if row is None:
        return None
    r = dict(row)
    for field in _PATCHSET_METADATA_FIELDS:
        r[field] = json.loads(r[field])
    return r


# ===========================================================================
# Review evaluations  (per-patchset aggregation of per-comment trains)
# ===========================================================================

def write_review_evaluation(db, root_message_id, ai_review_id, session_id, *,
                            trains_consumed=0, coverage_rate=None,
                            severity_weighted_coverage_rate=None,
                            fp_rate=None,
                            preexisting_unmatched_count=0,
                            redundancy_pairs=0,
                            had_missed_critical=False,
                            had_missed_major=False,
                            per_concern_verdicts=None,
                            per_candidate_review_stats=None,
                            notes=None):
    """Write or overwrite the review-level aggregation row for one
       (patchset, ai_review, session) triple. Idempotent: re-aggregation
       within the same session rewrites the row in place."""
    db.execute(
        "INSERT INTO review_evaluations "
        "(root_message_id,ai_review_id,session_id,evaluated_at,"
        "trains_consumed,coverage_rate,severity_weighted_coverage_rate,"
        "fp_rate,preexisting_unmatched_count,redundancy_pairs,"
        "had_missed_critical,had_missed_major,per_concern_verdicts,"
        "per_candidate_review_stats,notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(root_message_id,ai_review_id,session_id) DO UPDATE SET "
        "evaluated_at=excluded.evaluated_at, "
        "trains_consumed=excluded.trains_consumed, "
        "coverage_rate=excluded.coverage_rate, "
        "severity_weighted_coverage_rate=excluded.severity_weighted_coverage_rate, "
        "fp_rate=excluded.fp_rate, "
        "preexisting_unmatched_count=excluded.preexisting_unmatched_count, "
        "redundancy_pairs=excluded.redundancy_pairs, "
        "had_missed_critical=excluded.had_missed_critical, "
        "had_missed_major=excluded.had_missed_major, "
        "per_concern_verdicts=excluded.per_concern_verdicts, "
        "per_candidate_review_stats=excluded.per_candidate_review_stats, "
        "notes=excluded.notes",
        (norm_msgid(root_message_id), ai_review_id, session_id,
         int(time.time()), trains_consumed, coverage_rate,
         severity_weighted_coverage_rate, fp_rate,
         preexisting_unmatched_count, redundancy_pairs,
         1 if had_missed_critical else 0, 1 if had_missed_major else 0,
         json.dumps(per_concern_verdicts or []),
         json.dumps(per_candidate_review_stats or []),
         json.dumps(notes) if notes is not None else None))
    db.commit()


def get_review_evaluation(db, root_message_id, ai_review_id, session_id):
    """The review_evaluations row for one (patchset, ai_review, session)
       as a dict with JSON fields decoded, or None when aggregation has
       not run for that triple."""
    row = db.execute(
        "SELECT * FROM review_evaluations "
        "WHERE root_message_id=? AND ai_review_id=? AND session_id=?",
        (norm_msgid(root_message_id), ai_review_id, session_id)).fetchone()
    if row is None:
        return None
    r = dict(row)
    r["per_concern_verdicts"] = json.loads(r["per_concern_verdicts"])
    r["per_candidate_review_stats"] = json.loads(r["per_candidate_review_stats"])
    if r["notes"] is not None:
        r["notes"] = json.loads(r["notes"])
    r["had_missed_critical"] = bool(r["had_missed_critical"])
    r["had_missed_major"] = bool(r["had_missed_major"])
    return r


def review_evaluations_for_patchset(db, root_message_id):
    """All review_evaluations rows for one patchset, ordered by
       evaluated_at — the merge-gate evidence panel reads this to show
       per-session evaluations of the same review."""
    rows = db.execute(
        "SELECT * FROM review_evaluations WHERE root_message_id=? "
        "ORDER BY evaluated_at",
        (norm_msgid(root_message_id),)).fetchall()
    out = []
    for row in rows:
        r = dict(row)
        r["per_concern_verdicts"] = json.loads(r["per_concern_verdicts"])
        r["per_candidate_review_stats"] = json.loads(r["per_candidate_review_stats"])
        if r["notes"] is not None:
            r["notes"] = json.loads(r["notes"])
        r["had_missed_critical"] = bool(r["had_missed_critical"])
        r["had_missed_major"] = bool(r["had_missed_major"])
        out.append(r)
    return out


# ===========================================================================
# List tags  (lore mailing-list universe + operator gather filter)
# ===========================================================================

def seed_list_tags(db, manifest):
    """Seed the tag universe from lore's manifest. `manifest` is iterable of
       (tag, description). Idempotent: a tag already present (manifest or
       observed) keeps its `enabled` / `origin`; only `description` is
       refreshed when missing. Returns the number of newly-inserted tags."""
    now = int(time.time())
    n = 0
    for tag, description in manifest:
        cur = db.execute(
            "INSERT INTO list_tags (tag,description,origin,first_seen_at,"
            "last_seen_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(tag) DO UPDATE SET "
            "description=COALESCE(list_tags.description, excluded.description)",
            (tag, description, LIST_TAG_ORIGIN_MANIFEST, now, now))
        n += cur.rowcount
    db.commit()
    return n


def note_observed_tag(db, tag):
    """Record that gather has seen a patchset carrying `tag`. Creates the row
       with origin=OBSERVED if absent; bumps last_seen_at; never demotes a
       manifest-origin tag."""
    now = int(time.time())
    db.execute(
        "INSERT INTO list_tags (tag,origin,first_seen_at,last_seen_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(tag) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (tag, LIST_TAG_ORIGIN_OBSERVED, now, now))
    db.commit()


def set_tag_enabled(db, tag, enabled):
    """Set the operator's gather-filter flag on `tag`."""
    db.execute("UPDATE list_tags SET enabled=? WHERE tag=?",
               (1 if enabled else 0, tag))
    db.commit()


def enabled_tags(db):
    """The set of tags the operator has enabled for gather, as a list."""
    return [r["tag"] for r in db.execute(
        "SELECT tag FROM list_tags WHERE enabled=1 ORDER BY tag")]


def list_tags(db, *, enabled_only=False):
    """Every known list tag, as a list of dicts."""
    if enabled_only:
        rows = db.execute(
            "SELECT * FROM list_tags WHERE enabled=1 ORDER BY tag")
    else:
        rows = db.execute("SELECT * FROM list_tags ORDER BY tag")
    return [dict(r) for r in rows]


def set_patchset_tags(db, root_message_id, tags):
    """Replace the patchset's tag set. Tags not in `list_tags` are first
       added (origin=OBSERVED) so the join FK holds."""
    root = norm_msgid(root_message_id)
    now = int(time.time())
    tags = list(tags)
    for tag in tags:
        db.execute(
            "INSERT INTO list_tags (tag,origin,first_seen_at,last_seen_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(tag) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (tag, LIST_TAG_ORIGIN_OBSERVED, now, now))
    db.execute("DELETE FROM patchset_tags WHERE root_message_id=?", (root,))
    db.executemany("INSERT INTO patchset_tags (root_message_id,tag) "
                   "VALUES (?,?)", [(root, t) for t in tags])
    db.commit()


def tags_for_patchset(db, root_message_id):
    """Every tag carried by a patchset, as a list."""
    return [r["tag"] for r in db.execute(
        "SELECT tag FROM patchset_tags WHERE root_message_id=? ORDER BY tag",
        (norm_msgid(root_message_id),))]


# ===========================================================================
# Work queue  (prepare + review per-patchset, train per-patch-message)
# ===========================================================================

def enqueue_prepare(db, root_message_id, *, requested_by_user_id=None):
    """Enqueue a `prepare` work item for a gathered patchset. Returns the
       work-item id (or the existing one if a prepare item already exists
       for this patchset). `requested_by_user_id` stamps origin: NULL =
       system (gather), non-NULL = user-requested. Raises ValueError if
       the patchset is not gathered."""
    root = norm_msgid(root_message_id)
    ps = db.execute("SELECT state FROM patchsets WHERE root_message_id=?",
                    (root,)).fetchone()
    if ps is None:
        raise KeyError(root)
    if ps["state"] != PATCHSET_STATE_GATHERED:
        raise ValueError(f"patchset {root!r} is not gathered")
    existing = db.execute(
        "SELECT id FROM work_items WHERE root_message_id=? AND type=?",
        (root, WORK_ITEM_TYPE_PREPARE)).fetchone()
    if existing:
        return existing["id"]
    cur = db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at,"
        "requested_by_user_id) VALUES (?,?,?,?,?)",
        (WORK_ITEM_TYPE_PREPARE, root, WORK_ITEM_STATE_CLAIMABLE,
         int(time.time()), requested_by_user_id))
    db.commit()
    return cur.lastrowid


def enqueue_review(db, root_message_id, *, requested_by_user_id=None):
    """Enqueue a `review` work item for a gathered patchset. Returns the
       work-item id (or the existing one if a review item already exists for
       this patchset). `requested_by_user_id` stamps origin: NULL = system,
       non-NULL = user-requested. Raises ValueError if the patchset is not
       gathered, or if its prepare task has not produced a patchset_metadata
       row yet (prepare gates review — see docs/ARCHITECTURE-WORK-LIFECYCLE.md)."""
    root = norm_msgid(root_message_id)
    ps = db.execute("SELECT state FROM patchsets WHERE root_message_id=?",
                    (root,)).fetchone()
    if ps is None:
        raise KeyError(root)
    if ps["state"] != PATCHSET_STATE_GATHERED:
        raise ValueError(f"patchset {root!r} is not gathered")
    if db.execute(
            "SELECT 1 FROM patchset_metadata WHERE root_message_id=?",
            (root,)).fetchone() is None:
        raise ValueError(f"patchset {root!r} has no patchset_metadata: "
                         "prepare must complete before review")
    existing = db.execute(
        "SELECT id FROM work_items WHERE root_message_id=? AND type=?",
        (root, WORK_ITEM_TYPE_REVIEW)).fetchone()
    if existing:
        return existing["id"]
    cur = db.execute(
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at,"
        "requested_by_user_id) VALUES (?,?,?,?,?)",
        (WORK_ITEM_TYPE_REVIEW, root, WORK_ITEM_STATE_CLAIMABLE,
         int(time.time()), requested_by_user_id))
    db.commit()
    return cur.lastrowid


def enqueue_session_train(db, *, session_id, root_message_id,
                          patch_message_id, comment_message_id,
                          session_role, stratum_label,
                          requested_by_user_id=None):
    """Create one `train` work item, bound to a session at insertion.
       Called by the session orchestrator at `draft → ready` materialisation
       (once per `(patch, comment)` pair that passed the trainability
       filter). The work_items CHECK constraint enforces that every train
       row has its session fields, message_id (the patch), and
       comment_message_id populated. Returns the new work-item id.
       Raises ValueError on missing prerequisites — including an
       uploaded-origin patchset: uploads are submissions, never training
       data, and this is the choke point every train work-item passes
       through, so the exclusion is structural rather than a property of
       whichever selector enqueued it."""
    if session_role not in SESSION_ROLE_NAMES:
        raise ValueError(f"bad role: {session_role!r}")
    root = norm_msgid(root_message_id)
    ps = db.execute("SELECT origin FROM patchsets WHERE root_message_id=?",
                    (root,)).fetchone()
    if ps is not None and ps["origin"] != PATCHSET_ORIGIN_GATHERED:
        raise ValueError(f"patchset {root!r} is uploaded-origin: uploads "
                         "are never training data")
    if get_ai_review(db, root,
                     source=AI_REVIEW_SOURCE_HONE_NODE) is None:
        raise ValueError(f"no hone-node ai_review for patchset {root!r}: "
                         "cannot enqueue train")
    cur = db.execute(
        "INSERT INTO work_items (type,root_message_id,message_id,"
        "comment_message_id,state,training_session_id,session_role,"
        "stratum_label,enqueued_at,requested_by_user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (WORK_ITEM_TYPE_TRAIN, root, norm_msgid(patch_message_id),
         norm_msgid(comment_message_id), WORK_ITEM_STATE_CLAIMABLE,
         session_id, session_role, stratum_label, int(time.time()),
         requested_by_user_id))
    db.commit()
    return cur.lastrowid


# ----------------------------------------------------------------------------
# Auto-enqueue triggers — the gather pipeline calls these after upserting
# refs. Each is a no-op when its gating condition is unmet. Note: there is
# no `maybe_enqueue_train`; trains are created exclusively by the session
# orchestrator at session materialisation (see enqueue_session_train).
# ----------------------------------------------------------------------------

def maybe_enqueue_prepare(db, root_message_id, *, requested_by_user_id=None):
    """Enqueue a `prepare` work item for `root_message_id` once the patchset
       row exists in state=GATHERED. Idempotent — re-calling once the
       prepare item exists is a no-op. Returns the work-item id if a
       prepare now exists for the patchset, or None if the patchset is
       not yet gathered."""
    root = norm_msgid(root_message_id)
    ps = db.execute(
        "SELECT 1 FROM patchsets WHERE root_message_id=? AND state=?",
        (root, PATCHSET_STATE_GATHERED)).fetchone()
    if ps is None:
        return None
    return enqueue_prepare(db, root, requested_by_user_id=requested_by_user_id)


def maybe_enqueue_review(db, root_message_id, *, requested_by_user_id=None):
    """Enqueue a `review` work item for `root_message_id` once (a) the
       patchset has a `patchset_metadata` row (prepare has terminated), and
       (b) all of its patch messages are in the corpus (count of kind=PATCH
       messages >= patchsets.n_patches). Idempotent — re-calling once the
       review item exists is a no-op. Returns the work-item id if a review
       now exists for the patchset, or None when either gate doesn't yet
       pass."""
    root = norm_msgid(root_message_id)
    ps = db.execute(
        "SELECT n_patches FROM patchsets "
        "WHERE root_message_id=? AND state=?",
        (root, PATCHSET_STATE_GATHERED)).fetchone()
    if ps is None or ps["n_patches"] is None:
        return None
    if db.execute(
            "SELECT 1 FROM patchset_metadata WHERE root_message_id=?",
            (root,)).fetchone() is None:
        return None
    have = db.execute(
        "SELECT COUNT(*) AS n FROM messages "
        "WHERE root_message_id=? AND type=?",
        (root, MSG_TYPE_PATCH)).fetchone()["n"]
    if have < ps["n_patches"]:
        return None
    return enqueue_review(db, root, requested_by_user_id=requested_by_user_id)


def claim_work_item(db, worker_id, *, methodology_version, types=None,
                    owner_user_id=None, handles_system=True,
                    lease_seconds=1800):
    """Atomically claim the oldest claimable (or lease-expired) work item.

       Claim order:
         1. If `owner_user_id` is not None, prefer a USER item whose
            `requested_by_user_id` matches the owner (FIFO).
         2. Otherwise (or if no owner item is available) and
            `handles_system` is True, fall back to the SYSTEM pool
            (FIFO): system-origin items (`requested_by_user_id IS
            NULL`) plus *orphan rescue* — USER items whose requester
            currently owns no active node. Such an item has no
            dedicated server, so the system pool absorbs it rather
            than letting it starve (a user who pairs a node later
            takes their queue back; a user who deletes their last
            node releases pending items to the pool).
         3. Return None.

       A node with no owner *and* `handles_system=False` is configured for
       nothing — it gets None. A node with no owner and handles_system=True
       behaves as today: system-only (plus rescue).

       `types`, if given, restricts to that subset of WORK_ITEM_TYPE_*.
       Marks the item claimed under a fresh claim_id + lease and stamps
       `methodology_version` on the row — the version is frozen at
       claim time so an in-flight claim is pinned to the methodology
       its payload was compiled against, even if the active version
       rolls over before the node submits its result. Returns
       {claim_id, id, type, root_message_id, message_id,
       comment_message_id, lease_expires, methodology_version,
       training_session_id, session_role, stratum_label} or None when no
       work is available. The trailing fields are NULL for prepare/review
       and non-null for trains (per the work_items CHECK constraint).

       SQLite serializes writers, so two workers cannot claim the same row;
       a crashed worker's claim is reclaimed once its lease elapses."""
    if types is not None:
        types = tuple(types)
        if not types:
            return None

    if owner_user_id is not None:
        row = _try_claim(db, worker_id, methodology_version, types,
                         lease_seconds,
                         origin_clause="AND requested_by_user_id=?",
                         origin_params=(owner_user_id,))
        if row is not None:
            return row
    if handles_system:
        return _try_claim(
            db, worker_id, methodology_version, types, lease_seconds,
            origin_clause=(
                "AND (requested_by_user_id IS NULL "
                "     OR requested_by_user_id NOT IN "
                "        (SELECT owner_user_id FROM nodes "
                "         WHERE owner_user_id IS NOT NULL AND state=?))"),
            origin_params=(NODE_STATE_ACTIVE,))
    return None


def _try_claim(db, worker_id, methodology_version, types, lease_seconds,
               *, origin_clause, origin_params):
    """Single atomic claim attempt, scoped by `origin_clause`. Internal
       helper for claim_work_item's owner-then-system fallback."""
    now = int(time.time())
    claim_id = _new_claim_id()
    type_clause = ""
    type_params = ()
    if types is not None:
        type_clause = f"AND type IN ({','.join('?' * len(types))}) "
        type_params = types
    # completed_at=NULL: a deferred row re-offered on lease lapse arrives
    # here straight from a terminal state, still carrying its superseded
    # verdict's timestamp — a claimed row is by definition not completed.
    row = db.execute(
        f"UPDATE work_items SET state=?, claim_id=?, claimed_by=?, "
        f"claimed_at=?, lease_expires=?, heartbeat_at=?, "
        f"methodology_version=?, completed_at=NULL "
        f"WHERE id=("
        f"  SELECT id FROM work_items "
        f"  WHERE (state=? "
        f"         OR (state IN (?, ?) AND lease_expires<=?)) "
        f"  {origin_clause} "
        f"  {type_clause}"
        f"  ORDER BY enqueued_at, id LIMIT 1) "
        f"RETURNING claim_id, id, type, root_message_id, message_id, "
        f"comment_message_id, lease_expires, methodology_version, "
        f"training_session_id, session_role, stratum_label",
        (WORK_ITEM_STATE_CLAIMED, claim_id, worker_id, now,
         now + lease_seconds, now, methodology_version,
         WORK_ITEM_STATE_CLAIMABLE,
         WORK_ITEM_STATE_CLAIMED, WORK_ITEM_STATE_DEFERRED, now,
         *origin_params, *type_params)).fetchone()
    db.commit()
    return dict(row) if row else None


def heartbeat(db, claim_id, lease_seconds=1800):
    """Extend a live claim's lease (works for a work item or a draft
       task). Returns True if the claim is still valid, False if it has
       lapsed or completed - the worker should then stop and re-claim."""
    now = int(time.time())
    for table, claimed_state in (
            ("work_items", WORK_ITEM_STATE_CLAIMED),
            ("draft_tasks", DRAFT_TASK_STATE_CLAIMED)):
        cur = db.execute(
            f"UPDATE {table} SET lease_expires=?, heartbeat_at=? "
            f"WHERE claim_id=? AND state=?",
            (now + lease_seconds, now, claim_id, claimed_state))
        if cur.rowcount:
            db.commit()
            return True
    db.commit()
    return False


# Deferral retry policy. A DEFERRED row is re-offered once its
# lease_expires passes (the claim picker's existing gate), so backoff is
# expressed by pushing lease_expires out: lease × 4^(n-1), capped at a
# day. After DEFER_CAP deferrals the row PARKS — lease_expires goes NULL,
# which the re-offer clause (`lease_expires <= now`) never matches — so a
# permanently-unobtainable base stops looping; the admin's
# release-deferred button is the escape hatch (it resets the count).
DEFER_CAP                  = 5
_DEFER_BACKOFF_BASE        = 1800            # = the default claim lease
_DEFER_BACKOFF_MULT        = 4
_DEFER_BACKOFF_MAX_SECONDS = 24 * 3600


def _defer_backoff_seconds(defer_count):
    """Seconds until a row deferred for the n-th time is re-offered:
       30 min, 2 h, 8 h, then capped at 24 h."""
    return min(_DEFER_BACKOFF_BASE * _DEFER_BACKOFF_MULT ** (defer_count - 1),
               _DEFER_BACKOFF_MAX_SECONDS)


def submit_work_result(db, claim_id, *, state, record):
    """Record a node's verdict on a claimed work item and close it out.
       `state` is one of WORK_ITEM_STATE_COMPLETED|UNAPPLIABLE|DEFERRED;
       `record` (a dict) is stored as JSON. The row's methodology_version
       was stamped at claim time and is not touched here.

       A DEFERRED verdict additionally advances the retry policy above:
       defer_count increments and lease_expires becomes the next re-offer
       time (or NULL — parked — at DEFER_CAP). Returns:
         'ok'      recorded (or a no-op re-submit of the same claim)
         'lapsed'  the claim was reclaimed - the node must discard the result"""
    if state not in _WORK_ITEM_STATE_TERMINAL:
        raise ValueError(f"bad work-item terminal state: {state!r}")
    row = db.execute("SELECT state, defer_count FROM work_items "
                     "WHERE claim_id=?", (claim_id,)).fetchone()
    if row is None:
        return "lapsed"                          # reclaimed (or never issued)
    if row["state"] != WORK_ITEM_STATE_CLAIMED:
        return "ok"                              # already recorded
    now = int(time.time())
    if state == WORK_ITEM_STATE_DEFERRED:
        n = (row["defer_count"] or 0) + 1
        lease = (None if n >= DEFER_CAP          # parked — never re-offered
                 else now + _defer_backoff_seconds(n))
        if lease is None:
            log.info("submit_work_result: claim %s deferred ×%d — parked "
                     "(admin release-deferred to retry)", claim_id, n)
        db.execute(
            "UPDATE work_items SET state=?, record=?, completed_at=?, "
            "defer_count=?, lease_expires=? WHERE claim_id=?",
            (state, json.dumps(record), now, n, lease, claim_id))
    else:
        db.execute(
            "UPDATE work_items SET state=?, record=?, completed_at=? "
            "WHERE claim_id=?",
            (state, json.dumps(record), now, claim_id))
    db.commit()
    return "ok"


def release_claim(db, claim_id, *, reason=None):
    """Release a claim back to the CLAIMABLE pool — same effect as
       waiting for the lease to lapse, but immediate. The row's state
       flips CLAIMED → CLAIMABLE and every claim-time field is
       cleared (claim_id, claimed_by, claimed_at, lease_expires,
       heartbeat_at, methodology_version) so the next claimer sees
       a fresh row.

       Used by a hone-node that hit a configuration-fatal error
       mid-task (Claude API key rejected, model not found, …) and is
       about to exit — releasing fast lets a correctly-configured
       peer pick up the work without burning the lease window.

       Returns 'ok' on a successful release, 'ok' on a no-op re-call
       against an already-terminal or already-released claim
       (idempotent), or 'lapsed' when the claim_id was unknown
       (reclaimed by lease expiry, or never issued)."""
    row = db.execute("SELECT state FROM work_items WHERE claim_id=?",
                     (claim_id,)).fetchone()
    if row is None:
        return "lapsed"
    if row["state"] != WORK_ITEM_STATE_CLAIMED:
        return "ok"                              # already terminal / released
    log.info("release_claim: %s%s", claim_id,
             f" — {reason}" if reason else "")
    db.execute(
        "UPDATE work_items SET state=?, claim_id=NULL, claimed_by=NULL, "
        "claimed_at=NULL, lease_expires=NULL, heartbeat_at=NULL, "
        "methodology_version=NULL "
        "WHERE claim_id=?",
        (WORK_ITEM_STATE_CLAIMABLE, claim_id))
    db.commit()
    return "ok"


def release_deferred(db, item_id):
    """Operator-triggered release of a DEFERRED work item back to the
       CLAIMABLE pool — the manual, immediate analog of the lease-elapsed
       re-arm (claim_work_item already re-offers a deferred row once its
       lease lapses; this lets the operator skip the wait from the
       work-item detail page). The row's state flips DEFERRED → CLAIMABLE
       and every claim-time field is cleared (claim_id, claimed_by,
       claimed_at, lease_expires, heartbeat_at, methodology_version) —
       along with the stale completed_at, which belongs to the superseded
       verdict — so the next claimer sees a fresh row.

       Safe: a deferred item is terminal and unheld — the node that
       deferred it already submitted and moved on — so there is no
       in-flight claim to disrupt and no counters are touched.

       Keyed on `item_id` (the operator acts from the UI, holding the row
       id, not a claim_id). Returns 'ok' on release, 'not_deferred' when
       the row is in any other state (idempotent no-op — a double click,
       or a row already re-claimed), or 'unknown' when the id doesn't
       exist."""
    row = db.execute("SELECT state FROM work_items WHERE id=?",
                     (item_id,)).fetchone()
    if row is None:
        return "unknown"
    if row["state"] != WORK_ITEM_STATE_DEFERRED:
        return "not_deferred"
    log.info("release_deferred: work item %s → claimable", item_id)
    # defer_count resets — the admin's release grants a fresh retry
    # budget, un-parking a row that hit DEFER_CAP.
    db.execute(
        "UPDATE work_items SET state=?, claim_id=NULL, claimed_by=NULL, "
        "claimed_at=NULL, lease_expires=NULL, heartbeat_at=NULL, "
        "methodology_version=NULL, defer_count=0, completed_at=NULL "
        "WHERE id=?",
        (WORK_ITEM_STATE_CLAIMABLE, item_id))
    db.commit()
    return "ok"


def retry_unappliable(db, item_id):
    """Operator-triggered retry of an UNAPPLIABLE work item — flip it
       UNAPPLIABLE → CLAIMABLE so a node re-claims and re-attempts it.
       The 'try again' button on the work-item detail page POSTs here.

       A patchset goes UNAPPLIABLE when its series would not apply to the
       base at submission time; a review re-claims against the *current*
       tip-at-submission base, so a retry can succeed once the underlying
       tree has moved on. Like release_deferred, the row is terminal and
       unheld — there is no in-flight claim to disrupt — so we just clear
       every claim-time field plus the superseded completed_at and re-arm
       it; the next claimer sees a fresh row and overwrites the stale
       completion record on resubmission.

       Keyed on `item_id` (the operator acts from the UI, holding the row
       id). Returns 'ok' on re-arm, 'not_unappliable' when the row is in
       any other state (idempotent no-op — a double click, or a row already
       re-claimed), or 'unknown' when the id doesn't exist."""
    row = db.execute("SELECT state FROM work_items WHERE id=?",
                     (item_id,)).fetchone()
    if row is None:
        return "unknown"
    if row["state"] != WORK_ITEM_STATE_UNAPPLIABLE:
        return "not_unappliable"
    log.info("retry_unappliable: work item %s → claimable", item_id)
    db.execute(
        "UPDATE work_items SET state=?, claim_id=NULL, claimed_by=NULL, "
        "claimed_at=NULL, lease_expires=NULL, heartbeat_at=NULL, "
        "methodology_version=NULL, completed_at=NULL "
        "WHERE id=?",
        (WORK_ITEM_STATE_CLAIMABLE, item_id))
    db.commit()
    return "ok"


def retry_review(db, root_message_id, *, requested_by_user_id=None):
    """Patchset-scoped retry of an UNAPPLIABLE review — the "Retry
       review" action on the patchset detail page (the work-item-page
       retry_unappliable above is its admin, item-id-keyed sibling).
       Unappliable can be stale: the tip-at-submission base moves on,
       and a node-side failure (e.g. a schema-rejection fallback) lands
       here too — so the people the review is FOR (claimant/uploader,
       via _can_act_on_patchset at the route) get a way out of the
       dead end.

       Unlike retry_unappliable, the re-arm RE-STAMPS the requester:
       the retry is a fresh request on the retrier's behalf, routing to
       their nodes — which is exactly what makes it a per-user action
       rather than an operator one. Returns 'ok', 'not_unappliable'
       (any other state — idempotent no-op), or 'unknown' (no review
       work-item for this patchset)."""
    root = norm_msgid(root_message_id)
    row = db.execute(
        "SELECT id, state FROM work_items WHERE root_message_id=? "
        "AND type=?", (root, WORK_ITEM_TYPE_REVIEW)).fetchone()
    if row is None:
        return "unknown"
    if row["state"] != WORK_ITEM_STATE_UNAPPLIABLE:
        return "not_unappliable"
    log.info("retry_review: patchset %s review item %s → claimable "
             "(requested_by=%s)", root, row["id"], requested_by_user_id)
    db.execute(
        "UPDATE work_items SET state=?, requested_by_user_id=?, "
        "claim_id=NULL, claimed_by=NULL, claimed_at=NULL, "
        "lease_expires=NULL, heartbeat_at=NULL, methodology_version=NULL, "
        "completed_at=NULL "
        "WHERE id=?",
        (WORK_ITEM_STATE_CLAIMABLE, requested_by_user_id, row["id"]))
    db.commit()
    return "ok"


def cancel_work_item(db, item_id):
    """Admin-triggered cancellation of an UNHELD work item — the row is
       deleted, removing the request from the queue entirely. Only
       CLAIMABLE and DEFERRED rows qualify: both are terminal-or-waiting
       with no node holding a claim, so there is no in-flight work to
       disrupt and no completion record to lose. A CLAIMED row must run
       its lease out (or complete); COMPLETED / UNAPPLIABLE rows carry
       records and are removed through their own flows (delete_review).

       Cancelling a claimable review re-arms the patchset's Request-
       review button (nothing review-related remains); cancelling a
       prepare leaves the patchset unprepared — an informed admin call.

       Returns 'ok' on cancel, 'not_cancellable' for any other state
       (idempotent under double-click — the second POST sees 'unknown'),
       or 'unknown' when the id doesn't exist."""
    row = db.execute("SELECT state FROM work_items WHERE id=?",
                     (item_id,)).fetchone()
    if row is None:
        return "unknown"
    if row["state"] not in (WORK_ITEM_STATE_CLAIMABLE,
                            WORK_ITEM_STATE_DEFERRED):
        return "not_cancellable"
    log.info("cancel_work_item: work item %s deleted", item_id)
    db.execute("DELETE FROM work_items WHERE id=?", (item_id,))
    db.commit()
    return "ok"


def reclaim_expired(db):
    """Crash recovery: return lease-expired claims to their queues. Returns
       (work_items_reclaimed, draft_tasks_reclaimed)."""
    now = int(time.time())
    w = db.execute(
        "UPDATE work_items SET state=?, claim_id=NULL, claimed_by=NULL, "
        "claimed_at=NULL, lease_expires=NULL, heartbeat_at=NULL "
        "WHERE state=? AND lease_expires<=?",
        (WORK_ITEM_STATE_CLAIMABLE, WORK_ITEM_STATE_CLAIMED, now)).rowcount
    d = db.execute(
        "UPDATE draft_tasks SET state=?, claim_id=NULL, "
        "claimed_by=NULL, claimed_at=NULL, lease_expires=NULL, "
        "heartbeat_at=NULL WHERE state=? AND lease_expires<=?",
        (DRAFT_TASK_STATE_CLAIMABLE,
         DRAFT_TASK_STATE_CLAIMED, now)).rowcount
    db.commit()
    return w, d


def work_item_counts(db, *, requested_by_user_id=None):
    """Counts per (type, state), zero-filled across every state. Returns a
       dict {type: {state: n}}. `requested_by_user_id` scopes to one
       user's items (the non-admin queue view); None counts everything."""
    counts = {t: {s: 0 for s in WORK_ITEM_STATE_NAMES}
              for t in WORK_ITEM_TYPE_NAMES}
    sql = "SELECT type, state, COUNT(*) AS n FROM work_items "
    params = ()
    if requested_by_user_id is not None:
        sql += "WHERE requested_by_user_id=? "
        params = (requested_by_user_id,)
    sql += "GROUP BY type, state"
    for row in db.execute(sql, params):
        counts[row["type"]][row["state"]] = row["n"]
    return counts


def list_work_items(db, *, type=None, state=None, requested_by_user_id=None,
                    limit=200, offset=0):
    """The work queue joined with patchset metadata — sorted by start
       date (claimed_at) DESC, matching the Started column the queue
       renders: the most recently started work reads top-down first.
       Never-started rows (claimed_at NULL — SQLite sorts NULLs last
       under DESC) follow as the waiting backlog, newest-enqueued
       first, with id DESC as the final tiebreaker so a single-second
       gather batch keeps a stable order.

       Optionally filtered by type and/or state; `requested_by_user_id`
       scopes to one user's items (the non-admin queue view). `offset`
       skips that many rows (page 2 of size 50 = offset 50). Pairs with
       `count_work_items` for the UI's pagination."""
    sql = ("SELECT w.id, w.type, w.state, w.root_message_id, w.message_id, "
           "w.claimed_by, w.claimed_at, w.completed_at, w.enqueued_at, "
           "w.lease_expires, w.defer_count, w.requested_by_user_id, "
           "p.subject "
           "FROM work_items w "
           "LEFT JOIN patchsets p ON p.root_message_id=w.root_message_id ")
    params = []
    where = []
    if type is not None:
        where.append("w.type=?")
        params.append(type)
    if state is not None:
        where.append("w.state=?")
        params.append(state)
    if requested_by_user_id is not None:
        where.append("w.requested_by_user_id=?")
        params.append(requested_by_user_id)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += ("ORDER BY w.claimed_at DESC, w.enqueued_at DESC, "
            "w.id DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    return [dict(r) for r in db.execute(sql, params)]


def queue_version(db, *, type=None, state=None, requested_by_user_id=None):
    """A short, monotone token for the filtered queue's current activity
       state — two reads return the same value iff no row has appeared,
       transitioned state, been claimed, or completed in between.

       The UI's auto-poll passes the value back in an `X-Queue-Version`
       header; the queue handler returns 204 No Content when the header
       matches (so HTMX skips the swap and the server skips the
       template render). Cheap: one indexed aggregate over the filtered
       set. See docs/ARCHITECTURE-WORK-LIFECYCLE.md.

       `(max_activity, count, state_sum)`:
         - max_activity covers state transitions and new arrivals at
           wall-clock granularity (claimed_at / completed_at /
           enqueued_at).
         - count catches the rare drop case (a row removed without any
           timestamp moving).
         - state_sum catches state transitions that happen in the same
           wall-clock second as the prior poll, where max_activity
           would otherwise tie. (Timestamps are int seconds — under a
           bulk fleet claim several rows can transition in a single
           second; without state_sum the poll would return 204 and the
           operator wouldn't see the badge flip until the next claim
           crossed a second boundary.)"""
    sql = ("SELECT COALESCE(MAX(COALESCE(completed_at, claimed_at, "
           "enqueued_at)), 0) AS t, COUNT(*) AS n, "
           "COALESCE(SUM(state), 0) AS s FROM work_items")
    params, where = [], []
    if type is not None:
        where.append("type=?"); params.append(type)
    if state is not None:
        where.append("state=?"); params.append(state)
    if requested_by_user_id is not None:
        where.append("requested_by_user_id=?")
        params.append(requested_by_user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    r = db.execute(sql, params).fetchone()
    return f"{r['t']}-{r['n']}-{r['s']}"


def work_items_for_node(db, claimed_by, *, limit=50, offset=0):
    """Recent work-items claimed by a node, most-recent-claim first.
       `claimed_by` is whatever the api layer wrote into the column
       — the node's name when set, str(node.id) otherwise (see
       api.claim_task). `offset` skips that many rows for paging on
       the node detail page. Used by the node detail page to show
       per-node activity history."""
    rows = db.execute(
        "SELECT w.id, w.type, w.state, w.root_message_id, w.message_id, "
        "w.claimed_at, w.completed_at, w.enqueued_at, "
        "w.methodology_version, p.subject "
        "FROM work_items w "
        "LEFT JOIN patchsets p ON p.root_message_id=w.root_message_id "
        "WHERE w.claimed_by=? "
        "ORDER BY COALESCE(w.completed_at, w.claimed_at, w.enqueued_at) "
        "DESC, w.id DESC LIMIT ? OFFSET ?",
        (claimed_by, limit, offset))
    return [dict(r) for r in rows]


def count_work_items_for_node(db, claimed_by):
    """Total work-items claimed by a node — pairs with
       work_items_for_node for the node-detail Recent-claims
       paginator's total / page-count math."""
    return db.execute(
        "SELECT COUNT(*) FROM work_items WHERE claimed_by=?",
        (claimed_by,)).fetchone()[0]


def ai_reviews_for_node(db, node_id, *, limit=50):
    """Recent ai_reviews authored by a specific node (audit link via
       ai_reviews.node_id). Most-recently-recorded first."""
    rows = db.execute(
        "SELECT a.id, a.root_message_id, a.model, a.recorded_at, "
        "a.methodology_version, a.concerns, p.subject "
        "FROM ai_reviews a "
        "LEFT JOIN patchsets p ON p.root_message_id=a.root_message_id "
        "WHERE a.node_id=? "
        "ORDER BY a.recorded_at DESC LIMIT ?",
        (node_id, limit))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["concerns"] = json.loads(d["concerns"]) if d["concerns"] \
                            else []
        except (ValueError, TypeError):
            d["concerns"] = []
        out.append(d)
    return out


def get_work_item(db, work_item_id):
    """A single work_items row by id, with the JSON `record` column
       decoded. Returns None on unknown id. Used by the per-work-item
       detail page in the operator UI."""
    row = db.execute(
        "SELECT * FROM work_items WHERE id=?", (work_item_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("record"):
        try:
            d["record"] = json.loads(d["record"])
        except (ValueError, TypeError):
            d["record"] = None
    return d


def work_items_for_patchset(db, root_message_id):
    """Every work-item attached to a patchset, oldest-enqueued first — the
       queue history a per-patchset detail page renders."""
    rows = db.execute(
        "SELECT id, type, state, message_id, comment_message_id, "
        "claimed_by, claimed_at, lease_expires, heartbeat_at, "
        "completed_at, enqueued_at, methodology_version, "
        "training_session_id, session_role, stratum_label, "
        "requested_by_user_id "
        "FROM work_items WHERE root_message_id=? "
        "ORDER BY enqueued_at, id",
        (norm_msgid(root_message_id),))
    return [dict(r) for r in rows]


def count_work_items(db, *, type=None, state=None, requested_by_user_id=None):
    """Total rows in `work_items` under the same filter `list_work_items`
       takes. The UI calls this to size the queue page's pagination
       (page X of Y, the page-number window)."""
    sql = "SELECT COUNT(*) AS n FROM work_items"
    params, where = [], []
    if type is not None:
        where.append("type=?"); params.append(type)
    if state is not None:
        where.append("state=?"); params.append(state)
    if requested_by_user_id is not None:
        where.append("requested_by_user_id=?")
        params.append(requested_by_user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return db.execute(sql, params).fetchone()["n"]


# ===========================================================================
# Methodology + candidates + proposals
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
    """Store `document` as the next methodology version and make it active,
       superseding the previous active version. Returns the version."""
    nxt = db.execute("SELECT COALESCE(MAX(version),0)+1 "
                     "FROM methodology_versions").fetchone()[0]
    db.execute("UPDATE methodology_versions SET state=? WHERE state=?",
               (METHODOLOGY_VERSION_STATE_SUPERSEDED,
                METHODOLOGY_VERSION_STATE_ACTIVE))
    db.execute("INSERT INTO methodology_versions "
               "(version,document,state,note,created_at) "
               "VALUES (?,?,?,?,?)",
               (nxt, json.dumps(document), METHODOLOGY_VERSION_STATE_ACTIVE,
                note, int(time.time())))
    db.commit()
    return nxt


def active_methodology(db):
    """The active methodology as (version, document_dict), or None if the
       store has not been bootstrapped."""
    row = db.execute(
        "SELECT version, document FROM methodology_versions WHERE state=?",
        (METHODOLOGY_VERSION_STATE_ACTIVE,)).fetchone()
    return (row["version"], json.loads(row["document"])) if row else None


def methodology_document(db, version):
    """The methodology document (dict) for a specific version, or None — used
       to derive a review's per-check coverage against the exact check set that
       review ran under, not whatever is active now."""
    if version is None:
        return None
    row = db.execute(
        "SELECT document FROM methodology_versions WHERE version=?",
        (version,)).fetchone()
    return json.loads(row["document"]) if row else None


def add_candidate(db, candidate_id, body, origin=None):
    """Register a candidate practice on trial. Idempotent on the id."""
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO methodology_candidates "
               "(id,body,origin,created_at,updated_at) "
               "VALUES (?,?,?,?,?)",
               (candidate_id, body, origin, now, now))
    db.commit()


def bump_candidate(db, candidate_id, *, applied=0, catches=0,
                   unique_catches=0):
    """Add to a candidate's pooled counters. Severity is per-finding and
       lives in the `severity_witness_introduced` / `severity_witness_preexisting`
       histograms — bump those via `bump_severity_witness`."""
    cur = db.execute(
        "UPDATE methodology_candidates SET applied=applied+?, "
        "catches=catches+?, unique_catches=unique_catches+?, "
        "updated_at=? WHERE id=?",
        (applied, catches, unique_catches, int(time.time()), candidate_id))
    if cur.rowcount == 0:
        raise KeyError(candidate_id)
    db.commit()


def bump_severity_witness(db, candidate_id, severity, *,
                          is_preexisting=False, n=1):
    """Increment the candidate's `severity_witness_introduced` or
       `severity_witness_preexisting` histogram by `n` at the given severity
       tag. `severity` is a SEVERITY_* int or its lowercase tag string."""
    if isinstance(severity, int):
        tag = SEVERITY_NAMES.get(severity)
    else:
        tag = severity
    if tag not in SEVERITY_BY_NAME:
        raise ValueError(f"bad severity: {severity!r}")
    column = ("severity_witness_preexisting" if is_preexisting
              else "severity_witness_introduced")
    row = db.execute(
        f"SELECT {column} AS hist FROM methodology_candidates WHERE id=?",
        (candidate_id,)).fetchone()
    if row is None:
        raise KeyError(candidate_id)
    hist = json.loads(row["hist"]) if row["hist"] else {}
    hist[tag] = hist.get(tag, 0) + n
    db.execute(
        f"UPDATE methodology_candidates SET {column}=?, updated_at=? "
        "WHERE id=?",
        (json.dumps(hist), int(time.time()), candidate_id))
    db.commit()


def set_candidate_state(db, candidate_id, state):
    """Move a candidate to TRIAL | GRADUATED | PRUNED."""
    if state not in METHODOLOGY_CANDIDATE_STATE_NAMES:
        raise ValueError(f"bad candidate state: {state!r}")
    db.execute("UPDATE methodology_candidates SET state=?, updated_at=? "
               "WHERE id=?", (state, int(time.time()), candidate_id))
    db.commit()


def list_candidates(db, *, state=None):
    """Candidate practices, all or filtered by state, as a list of dicts
       with both `severity_witness_*` histograms decoded from JSON."""
    if state is None:
        rows = db.execute("SELECT * FROM methodology_candidates ORDER BY id")
    else:
        rows = db.execute(
            "SELECT * FROM methodology_candidates WHERE state=? ORDER BY id",
            (state,))
    out = []
    for row in rows:
        r = dict(row)
        r["severity_witness_introduced"] = json.loads(
            r["severity_witness_introduced"])
        r["severity_witness_preexisting"] = json.loads(
            r["severity_witness_preexisting"])
        out.append(r)
    return out


def add_proposal(db, type, payload):
    """Queue a merge-gate proposal. `type` is a METHODOLOGY_PROPOSAL_TYPE_*.
       Returns the new proposal id."""
    if type not in METHODOLOGY_PROPOSAL_TYPE_NAMES:
        raise ValueError(f"bad proposal type: {type!r}")
    cur = db.execute("INSERT INTO methodology_proposals "
                     "(type,payload,state,created_at) VALUES (?,?,?,?)",
                     (type, json.dumps(payload),
                      METHODOLOGY_PROPOSAL_STATE_PENDING, int(time.time())))
    db.commit()
    return cur.lastrowid


def list_proposals(db, *, state=METHODOLOGY_PROPOSAL_STATE_PENDING):
    """Merge-gate proposals in a given state, as a list of dicts."""
    return [dict(r) for r in db.execute(
        "SELECT * FROM methodology_proposals WHERE state=? ORDER BY id",
        (state,))]


def decide_proposal(db, proposal_id, decision, *, decided_by=None, note=None):
    """Record a human decision on a proposal. `decision` is a
       METHODOLOGY_PROPOSAL_STATE_* terminal value (ACCEPTED | DEFERRED |
       REJECTED | RETURNED). RETURNED bumps redraft_count."""
    terminal = {METHODOLOGY_PROPOSAL_STATE_ACCEPTED,
                METHODOLOGY_PROPOSAL_STATE_DEFERRED,
                METHODOLOGY_PROPOSAL_STATE_REJECTED,
                METHODOLOGY_PROPOSAL_STATE_RETURNED}
    if decision not in terminal:
        raise ValueError(f"bad decision: {decision!r}")
    db.execute(
        "UPDATE methodology_proposals SET state=?, decided_by=?, note=?, "
        "decided_at=?, redraft_count=redraft_count+? WHERE id=?",
        (decision, decided_by, note, int(time.time()),
         1 if decision == METHODOLOGY_PROPOSAL_STATE_RETURNED else 0,
         proposal_id))
    db.commit()


# ===========================================================================
# Eligibility flags  (deterministic-gate state for the draft-task pipeline)
# ===========================================================================

def set_eligibility_flag(db, subject_kind, subject_id, kind,
                         evidence_snapshot):
    """Mark `(subject_kind, subject_id, kind)` as eligible. Idempotent on
       the unique tuple — re-setting refreshes the evidence snapshot and
       clears any prior suppressed_at / defer_watermark_at. Returns the
       row id."""
    if subject_kind not in ELIGIBILITY_SUBJECT_KIND_NAMES:
        raise ValueError(f"bad subject_kind: {subject_kind!r}")
    if kind not in ELIGIBILITY_KIND_NAMES:
        raise ValueError(f"bad eligibility kind: {kind!r}")
    now = int(time.time())
    db.execute(
        "INSERT INTO eligibility_flags "
        "(subject_kind,subject_id,kind,evidence_snapshot,set_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(subject_kind,subject_id,kind) DO UPDATE SET "
        "evidence_snapshot=excluded.evidence_snapshot, "
        "set_at=excluded.set_at, suppressed_at=NULL, "
        "defer_watermark_at=NULL",
        (subject_kind, subject_id, kind, json.dumps(evidence_snapshot), now))
    db.commit()
    return db.execute(
        "SELECT id FROM eligibility_flags "
        "WHERE subject_kind=? AND subject_id=? AND kind=?",
        (subject_kind, subject_id, kind)).fetchone()["id"]


def clear_eligibility_flag(db, subject_kind, subject_id, kind):
    """Drop the eligibility flag — evidence regressed, gate's precondition
       no longer applies. No-op if no such flag exists."""
    db.execute(
        "DELETE FROM eligibility_flags "
        "WHERE subject_kind=? AND subject_id=? AND kind=?",
        (subject_kind, subject_id, kind))
    db.commit()


def mark_flag_suppressed(db, subject_kind, subject_id, kind):
    """Stamp `suppressed_at` so the flag never enters a draft batch — the
       *Reject* disposition's permanent, scoped effect."""
    db.execute(
        "UPDATE eligibility_flags SET suppressed_at=? "
        "WHERE subject_kind=? AND subject_id=? AND kind=?",
        (int(time.time()), subject_kind, subject_id, kind))
    db.commit()


def mark_flag_defer_watermark(db, subject_kind, subject_id, kind):
    """Stamp `defer_watermark_at` so the flag holds until counter growth
       exceeds the watermark by `defer_growth_factor` — the *Defer*
       disposition's gating effect."""
    db.execute(
        "UPDATE eligibility_flags SET defer_watermark_at=? "
        "WHERE subject_kind=? AND subject_id=? AND kind=?",
        (int(time.time()), subject_kind, subject_id, kind))
    db.commit()


def list_actionable_eligibility_flags(db, *, kind=None, limit=None):
    """Eligibility flags that have not been suppressed and have not been
       defer-watermarked — the candidates for the next draft batch. Order
       by `set_at` (FIFO). Returns a list of dicts with `evidence_snapshot`
       decoded."""
    sql = ("SELECT * FROM eligibility_flags "
           "WHERE suppressed_at IS NULL AND defer_watermark_at IS NULL")
    params = []
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY set_at, id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = db.execute(sql, params).fetchall()
    out = []
    for row in rows:
        r = dict(row)
        r["evidence_snapshot"] = json.loads(r["evidence_snapshot"])
        out.append(r)
    return out


# ===========================================================================
# Draft tasks  (the merge-gate work tier)
# ===========================================================================

def enqueue_draft_task(db, eligibility_flag_snapshot, *,
                       methodology_version=None, parent_proposal_id=None):
    """Enqueue a draft task carrying a snapshot of currently-eligible flags.
       `eligibility_flag_snapshot` is the JSON array of flag entries as of
       enqueue (subject to layer-4 batching rules — see
       docs/ARCHITECTURE-MERGE-GATE.md → Draft-task trigger logic).
       `parent_proposal_id` is set on redraft tasks (the Return-for-redraft
       lineage). Returns the new task id."""
    cur = db.execute(
        "INSERT INTO draft_tasks (eligibility_flag_snapshot,"
        "parent_proposal_id,methodology_version,state,created_at) "
        "VALUES (?,?,?,?,?)",
        (json.dumps(eligibility_flag_snapshot), parent_proposal_id,
         methodology_version, DRAFT_TASK_STATE_CLAIMABLE, int(time.time())))
    db.commit()
    return cur.lastrowid


def claim_draft_task(db, worker_id, lease_seconds=1800):
    """Atomically claim the oldest claimable (or lease-expired) draft task.
       Returns {claim_id, id, eligibility_flag_snapshot, parent_proposal_id,
       methodology_version} or None."""
    now = int(time.time())
    claim_id = _new_claim_id()
    row = db.execute(
        "UPDATE draft_tasks SET state=?, claim_id=?, claimed_by=?, "
        "claimed_at=?, lease_expires=?, heartbeat_at=? "
        "WHERE id=("
        "  SELECT id FROM draft_tasks "
        "  WHERE state=? OR (state=? AND lease_expires<=?) "
        "  ORDER BY created_at, id LIMIT 1) "
        "RETURNING claim_id, id, eligibility_flag_snapshot, "
        "parent_proposal_id, methodology_version",
        (DRAFT_TASK_STATE_CLAIMED, claim_id, worker_id, now,
         now + lease_seconds, now,
         DRAFT_TASK_STATE_CLAIMABLE,
         DRAFT_TASK_STATE_CLAIMED, now)).fetchone()
    db.commit()
    if not row:
        return None
    task = dict(row)
    task["eligibility_flag_snapshot"] = json.loads(
        task["eligibility_flag_snapshot"])
    return task


def complete_draft_task(db, claim_id, record):
    """Record a draft task's completion record. Returns 'ok' | 'lapsed'."""
    row = db.execute("SELECT state FROM draft_tasks WHERE claim_id=?",
                     (claim_id,)).fetchone()
    if row is None:
        return "lapsed"
    if row["state"] != DRAFT_TASK_STATE_CLAIMED:
        return "ok"
    db.execute("UPDATE draft_tasks SET state=?, record=?, "
               "completed_at=? WHERE claim_id=?",
               (DRAFT_TASK_STATE_COMPLETED, json.dumps(record),
                int(time.time()), claim_id))
    db.commit()
    return "ok"


def has_outstanding_draft_task(db):
    """True if and only if a draft task is currently claimable or claimed — the
       single-outstanding-task debounce gate. See
       docs/ARCHITECTURE-MERGE-GATE.md → Batching."""
    return db.execute(
        "SELECT 1 FROM draft_tasks WHERE state IN (?, ?) LIMIT 1",
        (DRAFT_TASK_STATE_CLAIMABLE, DRAFT_TASK_STATE_CLAIMED)
    ).fetchone() is not None


# ===========================================================================
# Training sessions  (operator-triggered batch overlays on the train flow)
# ===========================================================================

def create_session_draft(db, profile, *, created_by=None,
                         target_pool_size=None, target_holdout_size=None,
                         stratification_spec=None,
                         methodology_version=None):
    """Create a session in DRAFT state — the session-draft page's
       assembly target. Returns the new session id. Selection and
       materialisation happen at the DRAFT → READY transition (see
       `transition_session`); a DRAFT session holds nothing in
       `training_session_patchsets` yet."""
    cur = db.execute(
        "INSERT INTO training_sessions "
        "(created_at,created_by,state,profile,target_pool_size,"
        "target_holdout_size,stratification_spec,methodology_version) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (int(time.time()), created_by, SESSION_STATE_DRAFT, profile,
         target_pool_size, target_holdout_size,
         json.dumps(stratification_spec) if stratification_spec else None,
         methodology_version))
    db.commit()
    return cur.lastrowid


def transition_session(db, session_id, new_state, *, stats=None):
    """Advance a session's lifecycle. `new_state` is a SESSION_STATE_*
       value. Setting state=ANALYZED also records `stats` (JSON), and
       state=COMPLETE stamps `completed_at`."""
    if new_state not in SESSION_STATE_NAMES:
        raise ValueError(f"bad session state: {new_state!r}")
    fields = "state=?"
    params = [new_state]
    if new_state == SESSION_STATE_COMPLETE:
        fields += ", completed_at=?"
        params.append(int(time.time()))
    if stats is not None:
        fields += ", stats=?"
        params.append(json.dumps(stats))
    params.append(session_id)
    cur = db.execute(
        f"UPDATE training_sessions SET {fields} WHERE id=?", params)
    if cur.rowcount == 0:
        raise KeyError(session_id)
    db.commit()


def add_session_patchset(db, session_id, root_message_id, *,
                         role, stratum_label):
    """Link a patchset to a session under the given role + stratum, and
       record it in `patchset_session_history`. Called during the DRAFT →
       READY materialisation. Idempotent on (session_id, root_message_id)."""
    if role not in SESSION_ROLE_NAMES:
        raise ValueError(f"bad role: {role!r}")
    root = norm_msgid(root_message_id)
    now = int(time.time())
    db.execute(
        "INSERT INTO training_session_patchsets "
        "(session_id,root_message_id,role,stratum_label,selected_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(session_id,root_message_id) DO NOTHING",
        (session_id, root, role, stratum_label, now))
    db.execute(
        "INSERT INTO patchset_session_history "
        "(root_message_id,session_id,role,used_at) VALUES (?,?,?,?) "
        "ON CONFLICT(root_message_id,session_id) DO NOTHING",
        (root, session_id, role, now))
    db.commit()


def bump_session_patchset_progress(db, session_id, root_message_id, *,
                                   total_delta=0, done_delta=0):
    """Increment a session-patchset's train-work-item counters. The
       session orchestrator calls this as work-items are created (total++)
       and as they terminate (done++); the completion_state is recomputed
       from the new totals."""
    root = norm_msgid(root_message_id)
    row = db.execute(
        "SELECT train_work_items_total, train_work_items_done "
        "FROM training_session_patchsets "
        "WHERE session_id=? AND root_message_id=?",
        (session_id, root)).fetchone()
    if row is None:
        raise KeyError((session_id, root))
    total = row["train_work_items_total"] + total_delta
    done = row["train_work_items_done"] + done_delta
    if total == 0:
        cs = SESSION_PATCHSET_COMPLETION_PENDING
    elif done >= total:
        cs = SESSION_PATCHSET_COMPLETION_COMPLETE
    else:
        cs = SESSION_PATCHSET_COMPLETION_PARTIAL
    db.execute(
        "UPDATE training_session_patchsets SET "
        "train_work_items_total=?, train_work_items_done=?, "
        "completion_state=? WHERE session_id=? AND root_message_id=?",
        (total, done, cs, session_id, root))
    db.commit()


def list_sessions(db, *, state=None, limit=200):
    """Training sessions, all or filtered by lifecycle state, newest
       first. `stratification_spec` and `stats` are returned as decoded
       JSON when present."""
    sql = "SELECT * FROM training_sessions"
    params = []
    if state is not None:
        sql += " WHERE state=?"
        params.append(state)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    out = []
    for row in db.execute(sql, params):
        r = dict(row)
        if r["stratification_spec"]:
            r["stratification_spec"] = json.loads(r["stratification_spec"])
        if r["stats"]:
            r["stats"] = json.loads(r["stats"])
        out.append(r)
    return out


def session_patchsets(db, session_id, *, role=None):
    """The patchsets linked to a session, optionally filtered by role."""
    sql = ("SELECT * FROM training_session_patchsets "
           "WHERE session_id=?")
    params = [session_id]
    if role is not None:
        sql += " AND role=?"
        params.append(role)
    sql += " ORDER BY stratum_label, root_message_id"
    return [dict(r) for r in db.execute(sql, params)]


def patchset_appears_in_session_role(db, root_message_id, role):
    """True if and only if the patchset has ever been used in any session in the
       given role. The session-draft solver's strict-virginity filter
       calls this (with role=HOLDOUT) to find patchsets the methodology
       has never been tuned against."""
    return db.execute(
        "SELECT 1 FROM patchset_session_history "
        "WHERE root_message_id=? AND role=? LIMIT 1",
        (norm_msgid(root_message_id), role)).fetchone() is not None


# ===========================================================================
# Gather state  (per-source opaque resume cursor)
# ===========================================================================

def get_gather_state(db, source):
    """A gather module's watermark - its opaque resume cursor, or '' if the
       source has never run."""
    row = db.execute("SELECT cursor FROM gather_state WHERE source=?",
                     (source,)).fetchone()
    return row["cursor"] if row else ""


def set_gather_state(db, source, cursor):
    """Record `source`'s watermark - the resume cursor of the last patchset
       gathered - so the next GATHER pass resumes after it."""
    db.execute(
        "INSERT INTO gather_state (source,cursor,updated_at) VALUES (?,?,?) "
        "ON CONFLICT(source) DO UPDATE SET "
        "cursor=excluded.cursor, updated_at=excluded.updated_at",
        (source, cursor or "", int(time.time())))
    db.commit()


# ===========================================================================
# OAuth node enrollment & bearer tokens  (see API.md, ARCHITECTURE.md)
# ===========================================================================

class DuplicateNodeName(Exception):
    """Raised when an enrollment would create or approve a node whose
       name is already taken by an ACTIVE node. Revoked tombstones do
       not block — they're discarded identities, and an operator who
       wants the row gone can use `delete_node`. The API surface
       maps this to HTTP 409 Conflict; the UI silently drops the
       click so the pending row stays visible and the operator can
       deny it."""


def active_node_with_name(db, node_name):
    """The active node row matching this name, or None. Used by the
       enrollment guards — a non-None return is the duplicate-name
       conflict. A NULL / empty name never matches (a node that
       didn't self-identify can't conflict with one that did)."""
    if not node_name:
        return None
    row = db.execute(
        "SELECT * FROM nodes WHERE name=? AND state=?",
        (node_name, NODE_STATE_ACTIVE)).fetchone()
    return dict(row) if row else None


def create_enrollment(db, *, node_name=None, task_types=None,
                      ttl_seconds=900, interval_seconds=5):
    """Begin a device-authorization enrollment. Returns
       {device_code, user_code, expires_in, interval}. The secrets are
       returned ONCE - device_code is hashed for storage; user_code is kept
       in clear for the operator's approval lookup.

       Raises DuplicateNodeName if `node_name` matches an already-
       active node. (Revoked tombstones don't block — see
       active_node_with_name.)"""
    if active_node_with_name(db, node_name) is not None:
        raise DuplicateNodeName(
            f"a node already exists with name {node_name!r}")
    now = int(time.time())
    device_code = _token()
    for _ in range(10):                          # retry an (astronomical) clash
        try:
            db.execute(
                "INSERT INTO node_enrollments (device_code_hash,user_code,"
                "node_name,task_types,interval_seconds,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (_hash(device_code), _user_code(), node_name,
                 json.dumps(task_types) if task_types is not None else None,
                 interval_seconds, now, now + ttl_seconds))
            break
        except sqlite3.IntegrityError:
            continue
    else:
        raise RuntimeError("could not allocate a unique user_code")
    db.commit()
    row = db.execute(
        "SELECT user_code FROM node_enrollments WHERE device_code_hash=?",
        (_hash(device_code),)).fetchone()
    return {"device_code": device_code, "user_code": row["user_code"],
            "expires_in": ttl_seconds, "interval": interval_seconds}


def get_enrollment_by_device_code(db, device_code):
    """The enrollment row for a device_code, as a dict, or None."""
    row = db.execute(
        "SELECT * FROM node_enrollments WHERE device_code_hash=?",
        (_hash(device_code),)).fetchone()
    return dict(row) if row else None


def get_enrollment_by_user_code(db, user_code):
    """The enrollment row for a user_code, as a dict, or None. The code is
       normalized first."""
    row = db.execute("SELECT * FROM node_enrollments WHERE user_code=?",
                     (_norm_user_code(user_code),)).fetchone()
    return dict(row) if row else None


def list_pending_enrollments(db, *, requested_by_user_id=None):
    """Pending, unexpired enrollments awaiting an operator decision.

       When `requested_by_user_id` is given (the regular user case),
       returns only enrollments stamped by that user via
       tag_pending_enrollment — the lookup-er. When None (config-token
       admin), returns every pending row (including untagged legacy
       ones)."""
    now = int(time.time())
    if requested_by_user_id is None:
        return [dict(r) for r in db.execute(
            "SELECT * FROM node_enrollments WHERE state=? AND expires_at>? "
            "ORDER BY created_at", (NODE_ENROLLMENT_STATE_PENDING, now))]
    return [dict(r) for r in db.execute(
        "SELECT * FROM node_enrollments "
        "WHERE state=? AND expires_at>? AND requested_by_user_id=? "
        "ORDER BY created_at",
        (NODE_ENROLLMENT_STATE_PENDING, now, requested_by_user_id))]


def tag_pending_enrollment(db, user_code, requested_by_user_id):
    """Claim a pending enrollment for a user — the first user to look up
       a `user_code` on the /enroll page owns the pairing. Idempotent for
       the same (user_code, user) pair; subsequent calls by a different
       user are refused so the original lookup-er retains the pairing.

       Returns the enrollment row (as a dict) on success, or None if the
       user_code is unknown, the enrollment is no longer pending /
       expired, or another user already claimed it."""
    now = int(time.time())
    enr = get_enrollment_by_user_code(db, user_code)
    if enr is None:
        return None
    if enr["state"] != NODE_ENROLLMENT_STATE_PENDING:
        return None
    if enr["expires_at"] is not None and enr["expires_at"] <= now:
        return None
    # Atomic first-lookup-wins: the UPDATE itself re-checks that the tag
    # is still unset (or already ours — idempotent re-lookup) and that the
    # row is still pending and unexpired, so two concurrent lookups race
    # on the serialized write, not on the stale read above. The loser's
    # rowcount is 0 and it gets the same None as any late lookup-er.
    cur = db.execute(
        "UPDATE node_enrollments SET requested_by_user_id=? "
        "WHERE id=? AND state=? "
        "  AND (requested_by_user_id IS NULL OR requested_by_user_id=?) "
        "  AND (expires_at IS NULL OR expires_at>?)",
        (requested_by_user_id, enr["id"], NODE_ENROLLMENT_STATE_PENDING,
         requested_by_user_id, now))
    db.commit()
    if cur.rowcount == 0:
        return None
    enr["requested_by_user_id"] = requested_by_user_id
    return enr


def set_enrollment_polled(db, enrollment_id, when=None):
    """Stamp `last_polled_at` - the token endpoint records each poll so a
       too-fast poll can be answered with `slow_down`."""
    db.execute("UPDATE node_enrollments SET last_polled_at=? WHERE id=?",
               (when if when is not None else int(time.time()), enrollment_id))
    db.commit()


def approve_enrollment(db, user_code, *, node_name=None, decided_by=None,
                       owner_user_id=None):
    """Operator approval: create the enrollment's `nodes` row and mark the
       enrollment approved. Returns the new node id. Raises KeyError if the
       user_code is unknown, ValueError if the enrollment is not pending or
       has expired, DuplicateNodeName if approving would create a
       second active node with the same name (the race-protection
       check that guards the create_enrollment/approve_enrollment
       window when two concurrent enrolments share a name).

       `owner_user_id` stamps the new node's owner (typically the same
       user who looked up the user_code on /enroll). An owned node is
       created with `handles_system=0` — strictly user-only until the
       owner opts in from the /nodes/{id} detail page. Pass None for
       the config-token admin to leave the node ownerless: that node
       gets `handles_system=1` (system-only), since an ownerless node
       with no system fallback could never claim anything."""
    enr = get_enrollment_by_user_code(db, user_code)
    if enr is None:
        raise KeyError(user_code)
    if enr["state"] != NODE_ENROLLMENT_STATE_PENDING:
        raise ValueError(f"enrollment already {enr['state']}")
    if enr["expires_at"] is not None and enr["expires_at"] <= int(time.time()):
        raise ValueError("enrollment expired")
    resolved_name = node_name or enr["node_name"]
    if active_node_with_name(db, resolved_name) is not None:
        raise DuplicateNodeName(
            f"a node already exists with name {resolved_name!r}")
    now = int(time.time())
    # handles_system=0 for owned nodes — the owner opts in later from the
    # /nodes/{id} detail page. An ownerless (admin-approved) node must get
    # 1 instead: with no owner queue to serve, no system fallback would
    # leave it unable to claim anything at all.
    handles_system = 1 if owner_user_id is None else 0
    node_id = db.execute(
        "INSERT INTO nodes (name,task_types,state,enrolled_at,"
        "owner_user_id,handles_system) "
        "VALUES (?,?,?,?,?,?)",
        (resolved_name, enr["task_types"],
         NODE_STATE_ACTIVE, now, owner_user_id, handles_system)).lastrowid
    db.execute(
        "UPDATE node_enrollments SET state=?, node_id=?, decided_at=?, "
        "decided_by=? WHERE id=?",
        (NODE_ENROLLMENT_STATE_APPROVED, node_id, now, decided_by, enr["id"]))
    db.commit()
    return node_id


def deny_enrollment(db, user_code, *, decided_by=None):
    """Operator denial. Raises KeyError if the user_code is unknown,
       ValueError if the enrollment is not pending."""
    enr = get_enrollment_by_user_code(db, user_code)
    if enr is None:
        raise KeyError(user_code)
    if enr["state"] != NODE_ENROLLMENT_STATE_PENDING:
        raise ValueError(f"enrollment already {enr['state']}")
    db.execute(
        "UPDATE node_enrollments SET state=?, decided_at=?, decided_by=? "
        "WHERE id=?",
        (NODE_ENROLLMENT_STATE_DENIED, int(time.time()), decided_by, enr["id"]))
    db.commit()


def complete_enrollment(db, enrollment_id):
    """Mark an approved enrollment redeemed - the token endpoint calls this
       once it has issued the node's tokens, so the device code is
       single-use (a replay then gets `invalid_grant`)."""
    db.execute("UPDATE node_enrollments SET state=? WHERE id=?",
               (NODE_ENROLLMENT_STATE_COMPLETED, enrollment_id))
    db.commit()


def issue_tokens(db, node_id, *, access_ttl=3600, refresh_ttl=None):
    """Issue a fresh (access, refresh) token pair for a node. Returns
       {access_token, refresh_token, expires_in}; the tokens are returned ONCE
       (only their hashes are stored). `refresh_ttl` None => the refresh
       token does not expire."""
    now = int(time.time())
    access, refresh = _token(), _token()
    db.execute(
        "INSERT INTO node_tokens (node_id,access_token_hash,access_expires_at,"
        "refresh_token_hash,refresh_expires_at,state,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (node_id, _hash(access), now + access_ttl, _hash(refresh),
         now + refresh_ttl if refresh_ttl else None,
         NODE_TOKEN_STATE_ACTIVE, now))
    db.commit()
    return {"access_token": access, "refresh_token": refresh,
            "expires_in": access_ttl}


def resolve_access_token(db, access_token):
    """The node behind a bearer access token, as a dict (the `nodes` row), or
       None if the token is unknown, expired, superseded/revoked, or its
       node is revoked. Stamps the node's `last_seen`. This is the
       per-request auth check for the main API."""
    now = int(time.time())
    row = db.execute(
        "SELECT n.*, t.access_expires_at, t.state AS token_state "
        "FROM node_tokens t JOIN nodes n ON n.id=t.node_id "
        "WHERE t.access_token_hash=?", (_hash(access_token),)).fetchone()
    if row is None or row["token_state"] != NODE_TOKEN_STATE_ACTIVE \
            or row["access_expires_at"] <= now \
            or row["state"] != NODE_STATE_ACTIVE:
        return None
    db.execute("UPDATE nodes SET last_seen=? WHERE id=?", (now, row["id"]))
    db.commit()
    return {k: row[k] for k in row.keys()
            if k not in ("access_expires_at", "token_state")}


def rotate_refresh_token(db, refresh_token, *, access_ttl=3600,
                         refresh_ttl=None):
    """The refresh grant: validate a refresh token, supersede its pair, and
       issue a fresh pair for the same node. Returns the new
       {access_token, refresh_token, expires_in}, or None if the refresh
       token is unknown, expired, already used, revoked, or its node is
       revoked."""
    now = int(time.time())
    row = db.execute(
        "SELECT t.id, t.node_id, t.state, t.refresh_expires_at, "
        "n.state AS node_state FROM node_tokens t "
        "JOIN nodes n ON n.id=t.node_id WHERE t.refresh_token_hash=?",
        (_hash(refresh_token),)).fetchone()
    if row is None or row["state"] != NODE_TOKEN_STATE_ACTIVE \
            or row["node_state"] != NODE_STATE_ACTIVE:
        return None
    if row["refresh_expires_at"] is not None \
            and row["refresh_expires_at"] <= now:
        return None
    db.execute("UPDATE node_tokens SET state=? WHERE id=?",
               (NODE_TOKEN_STATE_SUPERSEDED, row["id"]))
    db.commit()
    return issue_tokens(db, row["node_id"], access_ttl=access_ttl,
                        refresh_ttl=refresh_ttl)


def revoke_node(db, node_id):
    """Revoke an enrolled node - mark it revoked and kill all its tokens. The
       node must re-enroll through the operator gate to return."""
    db.execute("UPDATE nodes SET state=? WHERE id=?",
               (NODE_STATE_REVOKED, node_id))
    db.execute("UPDATE node_tokens SET state=? WHERE node_id=?",
               (NODE_TOKEN_STATE_REVOKED, node_id))
    db.commit()


def delete_node(db, node_id):
    """Hard-delete an enrolled node — remove the row, delete its tokens
       (the only NOT-NULL FK pointing back), and NULL the audit-trail
       references on ai_reviews and node_enrollments so the historical
       record survives the deletion. Returns True when a row was
       removed, False when the node_id was unknown.

       Distinct from `revoke_node`, which is a soft delete: revoke
       keeps the row visible with state=revoked and only invalidates
       tokens. Delete is for the operator who wants the node gone
       from the fleet listing. Any in-flight work the node had
       claimed becomes orphaned (claimed_by is a TEXT column, not an
       FK) and is rescued when its lease lapses via reclaim_expired."""
    if db.execute("SELECT 1 FROM nodes WHERE id=?",
                  (node_id,)).fetchone() is None:
        return False
    db.execute("DELETE FROM node_tokens WHERE node_id=?", (node_id,))
    db.execute("UPDATE ai_reviews SET node_id=NULL WHERE node_id=?",
               (node_id,))
    db.execute("UPDATE node_enrollments SET node_id=NULL WHERE node_id=?",
               (node_id,))
    db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
    db.commit()
    return True


def _decode_node(row):
    """Turn a `nodes` row into a dict and JSON-decode the `health`
       column on the way out. Centralised so the UI doesn't have to
       json.loads each row — and the dict it gets back is exactly
       what the node POSTed, no escaping surprises."""
    if row is None:
        return None
    out = dict(row)
    if out.get("health"):
        try:
            out["health"] = json.loads(out["health"])
        except (ValueError, TypeError):
            out["health"] = None
    return out


def get_node(db, node_id):
    """The node row as a dict, or None. `health` (if set) is decoded
       from the stored JSON snapshot into a dict."""
    row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    return _decode_node(row)


def list_nodes(db):
    """Every enrolled node, as a list of dicts (health decoded)."""
    return [_decode_node(r)
            for r in db.execute("SELECT * FROM nodes ORDER BY id")]


def set_node_handles_system(db, node_id, handles_system):
    """Flip a node's `handles_system` flag (0 or 1). Returns True when a
       row was updated."""
    cur = db.execute(
        "UPDATE nodes SET handles_system=? WHERE id=?",
        (1 if handles_system else 0, node_id))
    db.commit()
    return cur.rowcount > 0


def set_node_owner(db, node_id, owner_user_id):
    """Reassign a node's owner (or set it ownerless with None). Returns
       True when a row was updated."""
    cur = db.execute(
        "UPDATE nodes SET owner_user_id=? WHERE id=?",
        (owner_user_id, node_id))
    db.commit()
    return cur.rowcount > 0


def fleet_status(db, stale_after_seconds):
    """The rollup view of the node fleet — for the operator UI's
       always-visible fleet-pulse chip and any future status-page
       widgets. One query, O(nodes) at the SQL level, returns a tiny
       dict regardless of fleet size:

         {
           "total":            7,   # NODE_STATE_ACTIVE rows only
           "healthy":          5,   # fresh + no anthropic error
           "errored":          1,   # last health snapshot carries
                                    # a non-null last_anthropic_error
           "stale":            1,   # last_seen older than the cutoff
           "in_flight":        3,   # CLAIMED work_items, lease unexpired
           "last_activity_at": <unix>  # max(nodes.last_seen) or None
         }

       Revoked nodes are excluded. `stale_after_seconds` is the
       freshness cutoff — typically a small multiple of the heartbeat
       interval (e.g. 3× heartbeat_seconds). A node counted as
       `errored` is NOT also counted as `stale` even if both apply;
       errored is the louder signal and the rollup is exclusive."""
    now = int(time.time())
    cutoff = now - max(stale_after_seconds, 1)
    rows = db.execute(
        "SELECT id, last_seen, health FROM nodes WHERE state=?",
        (NODE_STATE_ACTIVE,)).fetchall()
    healthy = errored = stale = 0
    last_activity_at = None
    for row in rows:
        seen = row["last_seen"] or 0
        if last_activity_at is None or seen > last_activity_at:
            last_activity_at = seen
        # Errored wins over stale — operator wants the anthropic-error
        # count surfaced even on a node that's also gone quiet.
        snapshot = row["health"]
        anth_err = None
        if snapshot:
            try:
                anth_err = json.loads(snapshot).get("last_anthropic_error")
            except (ValueError, TypeError):
                anth_err = None
        if anth_err:
            errored += 1
        elif seen < cutoff:
            stale += 1
        else:
            healthy += 1
    # A CLAIMED row whose lease has lapsed is no longer in flight — the
    # node went silent and the claim protocol will re-offer the row (the
    # periodic reclaim sweep flips it back to claimable; this filter
    # keeps the count honest in the window before the sweep fires).
    in_flight = db.execute(
        "SELECT COUNT(*) FROM work_items WHERE state=? "
        "AND (lease_expires IS NULL OR lease_expires > ?)",
        (WORK_ITEM_STATE_CLAIMED, now)).fetchone()[0]
    return {"total":            len(rows),
             "healthy":          healthy,
             "errored":          errored,
             "stale":            stale,
             "in_flight":        in_flight,
             "last_activity_at": last_activity_at or None}


def fleet_throughput(db, *, window_seconds=3600, bin_seconds=60):
    """Per-minute count of work_items that reached a terminal state
       (COMPLETED / UNAPPLIABLE / DEFERRED) within the last
       `window_seconds`. Returns a list of N integer counts where
       N = window_seconds // bin_seconds (60 by default), oldest
       bin first / most-recent bin last — so the operator-UI
       sparkline draws left-to-right with the latest minute at the
       right edge.

       Bins are anchored to "now minus k minutes", not to wall-clock
       minute boundaries — keeps the rightmost bin stable across
       polls and avoids a phantom spike when wall-clock minutes
       roll over. Cheap regardless of fleet size: one indexed range
       scan on completed_at."""
    now = int(time.time())
    cutoff = now - window_seconds
    nbins = max(1, window_seconds // bin_seconds)
    bins = [0] * nbins
    rows = db.execute(
        "SELECT completed_at FROM work_items "
        "WHERE state IN (?,?,?) AND completed_at >= ?",
        (WORK_ITEM_STATE_COMPLETED, WORK_ITEM_STATE_UNAPPLIABLE,
         WORK_ITEM_STATE_DEFERRED, cutoff)).fetchall()
    for r in rows:
        ts = r[0] or 0
        offset = now - ts
        if 0 <= offset < window_seconds:
            idx = nbins - 1 - (offset // bin_seconds)
            bins[idx] += 1
    return bins


_HEALTH_ALERT_TITLES = {"disk_low":         "low free disk",
                        "api_error":        "Anthropic API errors",
                        "budget_exhausted": "token budget exhausted"}


def _health_alert_kinds(snap):
    """The set of alert-kind strings a health snapshot trips."""
    kinds = set()
    if not isinstance(snap, dict):
        return kinds
    if snap.get("disk_low"):
        kinds.add("disk_low")
    if snap.get("last_anthropic_error"):
        kinds.add("api_error")
    tb = snap.get("token_budget")
    if isinstance(tb, dict) and tb.get("exhausted"):
        kinds.add("budget_exhausted")
    return kinds


def _notify_node_health(db, node_id, prior_row, snapshot):
    """Edge-triggered node-health notification to the node's owner: fire only
       for alert kinds newly present vs the prior snapshot, so a steady-state
       repeat (health posts every tick) doesn't spam."""
    owner = prior_row["owner_user_id"]
    if owner is None:
        return                              # system node — no owner to notify
    try:
        prior_snap = (json.loads(prior_row["health"])
                      if prior_row["health"] else {})
    except (ValueError, TypeError):
        prior_snap = {}
    new_kinds = _health_alert_kinds(snapshot) - _health_alert_kinds(prior_snap)
    if not new_kinds:
        return
    name = prior_row["name"] or f"node {node_id}"
    day = time.strftime("%Y-%m-%d", time.gmtime())
    for kind in new_kinds:
        insert_notification(
            db, owner, type=NOTIF_TYPE_NODE_HEALTH,
            dedup_key=f"node_health:{node_id}:{kind}:{day}",
            title=f"Node {name}: {_HEALTH_ALERT_TITLES.get(kind, kind)}",
            link=f"/nodes/{node_id}")


def update_node_health(db, node_id, snapshot):
    """Stamp the latest health snapshot on a node row. `snapshot` is a
       JSON-serializable dict — the node's choice of fields; today
       the first cut is {free_disk_mb, refrepo_size_mb,
       last_anthropic_error}, but the wire is loose so adding
       fields later doesn't need a migration. Returns True when a
       row was updated, False when the node_id was unknown
       (revoked / deleted between request and write — best treated
       as a silent no-op).

       Edge-triggers a node-health notification to the node's owner when the
       snapshot newly trips an alert (low disk / API errors / budget spent)."""
    prior = db.execute(
        "SELECT health, owner_user_id, name FROM nodes WHERE id=?",
        (node_id,)).fetchone()
    cur = db.execute(
        "UPDATE nodes SET health=?, health_at=? WHERE id=?",
        (json.dumps(snapshot), int(time.time()), node_id))
    db.commit()
    if cur.rowcount > 0 and prior is not None:
        try:
            _notify_node_health(db, node_id, prior, snapshot)
        except Exception:
            log.warning("node-health notification failed (non-fatal)",
                        exc_info=True)
    return cur.rowcount > 0


# ===========================================================================
# Operator users
# ===========================================================================

def get_user_by_email(db, email):
    return db.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()


def get_user_by_google_sub(db, sub):
    return db.execute(
        "SELECT * FROM users WHERE google_sub = ?", (sub,)
    ).fetchone()


def get_user_by_id(db, user_id):
    return db.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def list_users(db):
    return db.execute(
        "SELECT * FROM users ORDER BY created_at ASC"
    ).fetchall()


def create_user(db, email, display_name, auth_provider, *,
                password_hash=None, google_sub=None):
    now = int(time.time())
    cur = db.execute(
        """INSERT INTO users (email, display_name, auth_provider,
                              password_hash, google_sub, state, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (email.lower().strip(), display_name, auth_provider,
         password_hash, google_sub, now))
    db.commit()
    return cur.lastrowid


def set_user_state(db, user_id, state):
    now = int(time.time())
    approved_at = now if state == "approved" else None
    # COALESCE preserves approved_at on revoke/un-approve (audit trail) and
    # refreshes it with `now` on (re-)approval.
    db.execute(
        "UPDATE users SET state = ?, approved_at = COALESCE(?, approved_at) WHERE id = ?",
        (state, approved_at, user_id))
    db.commit()


def set_user_display_name(db, user_id, display_name):
    """Update a user's display name (the User-settings profile form).
       Returns True when a row was updated."""
    cur = db.execute(
        "UPDATE users SET display_name = ? WHERE id = ?",
        (display_name, user_id))
    db.commit()
    return cur.rowcount > 0


def set_user_password_hash(db, user_id, password_hash):
    """Replace a user's Argon2 password hash (the User-settings
       change-password form). Returns True when a row was updated."""
    cur = db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (password_hash, user_id))
    db.commit()
    return cur.rowcount > 0


def set_user_admin(db, user_id, is_admin):
    """Flip a user's `is_admin` grant (0 or 1). Returns True when a row
       was updated. Takes effect on the user's next request —
       auth.current_session_user re-derives the flag from this column."""
    cur = db.execute(
        "UPDATE users SET is_admin = ? WHERE id = ?",
        (1 if is_admin else 0, user_id))
    db.commit()
    return cur.rowcount > 0


def set_user_maintainer(db, user_id, is_maintainer):
    """Flip a user's `is_maintainer` grant (0 or 1). Returns True when a
       row was updated. Same freshness as is_admin: re-derived per
       request, so it takes effect on the user's next request."""
    cur = db.execute(
        "UPDATE users SET is_maintainer = ? WHERE id = ?",
        (1 if is_maintainer else 0, user_id))
    db.commit()
    return cur.rowcount > 0


def delete_user(db, user_id):
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()


def touch_last_login(db, user_id):
    db.execute(
        "UPDATE users SET last_login_at = ? WHERE id = ?",
        (int(time.time()), user_id))
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
    elif cmd == "seed-list-tags" and len(a) >= 3:
        # File is a JSON object: {"<list-id>": "<description>", ...}. The
        # operator gets the set from lore.kernel.org/manifest.js.gz —
        # gunzip, strip the JS `Manifest(...);` wrapper, transform paths
        # into List-Ids, then pass the resulting JSON here.
        with open(a[2], encoding="utf-8") as f:
            manifest = json.load(f)
        n = seed_list_tags(db, manifest.items())
        print(f"seeded {n} new list-tag(s); universe is "
              f"{db.execute('SELECT COUNT(*) FROM list_tags').fetchone()[0]}")
    elif cmd == "stats":
        for t in ("patchsets", "messages", "ai_reviews",
                  "patchset_metadata", "review_evaluations",
                  "list_tags", "patchset_tags",
                  "methodology_versions", "methodology_candidates",
                  "methodology_proposals", "eligibility_flags",
                  "work_items", "draft_tasks",
                  "training_sessions", "training_session_patchsets",
                  "patchset_session_history",
                  "nodes", "node_enrollments", "node_tokens",
                  "gather_state"):
            n = db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:30} {n}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
