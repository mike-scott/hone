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
import time
import uuid

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

_MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2]


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
    migrate(db)                          # runs with foreign_keys off (default)
    db.execute("PRAGMA foreign_keys=ON")
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

def upsert_patchset(db, root_message_id, *, subject=None, submitter_email=None,
                    sent=None, n_patches=None, base_commit=None,
                    change_id=None, series_version=1, gathered_at=None):
    """Insert a gathered patchset (idempotent on the root id); refresh the
       mutable fields if it is already known. Does not touch state/skip_reason
       - a re-gather never un-skips a patchset. Returns the normalized
       root_message_id."""
    root = norm_msgid(root_message_id)
    db.execute(
        "INSERT INTO patchsets (root_message_id,subject,submitter_email,sent,"
        "n_patches,base_commit,change_id,series_version,state,gathered_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(root_message_id) DO UPDATE SET "
        "subject=excluded.subject, submitter_email=excluded.submitter_email, "
        "sent=excluded.sent, n_patches=excluded.n_patches, "
        "base_commit=excluded.base_commit, change_id=excluded.change_id, "
        "series_version=excluded.series_version",
        (root, subject,
         norm_email(submitter_email) if submitter_email else None,
         sent, n_patches, base_commit, change_id, series_version,
         PATCHSET_STATE_GATHERED,
         gathered_at if gathered_at is not None else int(time.time())))
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
    db.commit()


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
                     meta=None):
    """Insert (or replace) the structured AI review of a patchset. `concerns`
       is a dict ({"concerns": [...]}); it is JSON-encoded. One row per
       (patchset, source). Returns the row id."""
    root = norm_msgid(root_message_id)
    now = int(time.time())
    db.execute(
        "INSERT INTO ai_reviews (root_message_id,source,concerns,model,"
        "input_tokens,output_tokens,reviewed_at,methodology_version,node_id,"
        "meta,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(root_message_id,source) DO UPDATE SET "
        "concerns=excluded.concerns, model=excluded.model, "
        "input_tokens=excluded.input_tokens, "
        "output_tokens=excluded.output_tokens, "
        "reviewed_at=excluded.reviewed_at, "
        "methodology_version=excluded.methodology_version, "
        "node_id=excluded.node_id, meta=excluded.meta, "
        "recorded_at=excluded.recorded_at",
        (root, source, json.dumps(concerns), model, input_tokens,
         output_tokens, reviewed_at if reviewed_at is not None else now,
         methodology_version, node_id,
         json.dumps(meta) if meta is not None else None, now))
    db.commit()
    return db.execute(
        "SELECT id FROM ai_reviews WHERE root_message_id=? AND source=?",
        (root, source)).fetchone()["id"]


def get_ai_review(db, root_message_id, *,
                  source=AI_REVIEW_SOURCE_HONE_NODE):
    """The AI review row as a dict (with `concerns` / `meta` decoded), or
       None."""
    row = db.execute(
        "SELECT * FROM ai_reviews WHERE root_message_id=? AND source=?",
        (norm_msgid(root_message_id), source)).fetchone()
    if row is None:
        return None
    r = dict(row)
    r["concerns"] = json.loads(r["concerns"])
    if r["meta"] is not None:
        r["meta"] = json.loads(r["meta"])
    return r


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

def enqueue_prepare(db, root_message_id):
    """Enqueue a `prepare` work item for a gathered patchset. Returns the
       work-item id (or the existing one if a prepare item already exists
       for this patchset). Raises ValueError if the patchset is not
       gathered."""
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
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at) "
        "VALUES (?,?,?,?)",
        (WORK_ITEM_TYPE_PREPARE, root, WORK_ITEM_STATE_CLAIMABLE,
         int(time.time())))
    db.commit()
    return cur.lastrowid


def enqueue_review(db, root_message_id):
    """Enqueue a `review` work item for a gathered patchset. Returns the
       work-item id (or the existing one if a review item already exists for
       this patchset). Raises ValueError if the patchset is not gathered,
       or if its prepare task has not produced a patchset_metadata row yet
       (prepare gates review — see docs/ARCHITECTURE-WORK-LIFECYCLE.md)."""
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
        "INSERT INTO work_items (type,root_message_id,state,enqueued_at) "
        "VALUES (?,?,?,?)",
        (WORK_ITEM_TYPE_REVIEW, root, WORK_ITEM_STATE_CLAIMABLE,
         int(time.time())))
    db.commit()
    return cur.lastrowid


def enqueue_session_train(db, *, session_id, root_message_id,
                          patch_message_id, comment_message_id,
                          session_role, stratum_label):
    """Create one `train` work item, bound to a session at insertion.
       Called by the session orchestrator at `draft → ready` materialisation
       (once per `(patch, comment)` pair that passed the trainability
       filter). The work_items CHECK constraint enforces that every train
       row has its session fields, message_id (the patch), and
       comment_message_id populated. Returns the new work-item id.
       Raises ValueError on missing prerequisites."""
    if session_role not in SESSION_ROLE_NAMES:
        raise ValueError(f"bad role: {session_role!r}")
    root = norm_msgid(root_message_id)
    if get_ai_review(db, root,
                     source=AI_REVIEW_SOURCE_HONE_NODE) is None:
        raise ValueError(f"no hone-node ai_review for patchset {root!r}: "
                         "cannot enqueue train")
    cur = db.execute(
        "INSERT INTO work_items (type,root_message_id,message_id,"
        "comment_message_id,state,training_session_id,session_role,"
        "stratum_label,enqueued_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (WORK_ITEM_TYPE_TRAIN, root, norm_msgid(patch_message_id),
         norm_msgid(comment_message_id), WORK_ITEM_STATE_CLAIMABLE,
         session_id, session_role, stratum_label, int(time.time())))
    db.commit()
    return cur.lastrowid


# ----------------------------------------------------------------------------
# Auto-enqueue triggers — the gather pipeline calls these after upserting
# refs. Each is a no-op when its gating condition is unmet. Note: there is
# no `maybe_enqueue_train`; trains are created exclusively by the session
# orchestrator at session materialisation (see enqueue_session_train).
# ----------------------------------------------------------------------------

def maybe_enqueue_prepare(db, root_message_id):
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
    return enqueue_prepare(db, root)


def maybe_enqueue_review(db, root_message_id):
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
    return enqueue_review(db, root)


def claim_work_item(db, worker_id, *, methodology_version, types=None,
                    lease_seconds=1800):
    """Atomically claim the oldest claimable (or lease-expired) work item.
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
    now = int(time.time())
    claim_id = _new_claim_id()
    type_clause = ""
    type_params = ()
    if types is not None:
        types = tuple(types)
        if not types:
            return None
        type_clause = f"AND type IN ({','.join('?' * len(types))}) "
        type_params = types
    row = db.execute(
        f"UPDATE work_items SET state=?, claim_id=?, claimed_by=?, "
        f"claimed_at=?, lease_expires=?, heartbeat_at=?, "
        f"methodology_version=? "
        f"WHERE id=("
        f"  SELECT id FROM work_items "
        f"  WHERE (state=? OR (state=? AND lease_expires<=?)) "
        f"  {type_clause}"
        f"  ORDER BY enqueued_at, id LIMIT 1) "
        f"RETURNING claim_id, id, type, root_message_id, message_id, "
        f"comment_message_id, lease_expires, methodology_version, "
        f"training_session_id, session_role, stratum_label",
        (WORK_ITEM_STATE_CLAIMED, claim_id, worker_id, now,
         now + lease_seconds, now, methodology_version,
         WORK_ITEM_STATE_CLAIMABLE, WORK_ITEM_STATE_CLAIMED, now,
         *type_params)).fetchone()
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


def submit_work_result(db, claim_id, *, state, record):
    """Record a node's verdict on a claimed work item and close it out.
       `state` is one of WORK_ITEM_STATE_COMPLETED|UNAPPLIABLE|DEFERRED;
       `record` (a dict) is stored as JSON. The row's methodology_version
       was stamped at claim time and is not touched here. Returns:
         'ok'      recorded (or a no-op re-submit of the same claim)
         'lapsed'  the claim was reclaimed - the node must discard the result"""
    if state not in _WORK_ITEM_STATE_TERMINAL:
        raise ValueError(f"bad work-item terminal state: {state!r}")
    row = db.execute("SELECT state FROM work_items WHERE claim_id=?",
                     (claim_id,)).fetchone()
    if row is None:
        return "lapsed"                          # reclaimed (or never issued)
    if row["state"] != WORK_ITEM_STATE_CLAIMED:
        return "ok"                              # already recorded
    db.execute(
        "UPDATE work_items SET state=?, record=?, completed_at=? "
        "WHERE claim_id=?",
        (state, json.dumps(record), int(time.time()), claim_id))
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


def work_item_counts(db):
    """Counts per (type, state), zero-filled across every state. Returns a
       dict {type: {state: n}}."""
    counts = {t: {s: 0 for s in WORK_ITEM_STATE_NAMES}
              for t in WORK_ITEM_TYPE_NAMES}
    for row in db.execute(
            "SELECT type, state, COUNT(*) AS n FROM work_items "
            "GROUP BY type, state"):
        counts[row["type"]][row["state"]] = row["n"]
    return counts


def list_work_items(db, *, type=None, state=None, limit=200, offset=0):
    """The work queue joined with patchset metadata — sorted by most-recent
       activity, so any state change (enqueue → claim → complete) bubbles
       the row to the top. The sort key is COALESCE(completed_at,
       claimed_at, enqueued_at): a freshly-claimed row promotes via
       claimed_at, a completed row via completed_at, an idle claimable
       row sits at its enqueued_at. id DESC is the tiebreaker so within
       a single-second gather batch the most-recently inserted row wins.

       This is the order the operator UI wants: the active part of the
       queue is always visible at the top, instead of buried under newer
       work that the FIFO claim picker hasn't reached yet.

       Optionally filtered by type and/or state. `offset` skips that
       many rows (page 2 of size 50 = offset 50). Pairs with
       `count_work_items` for the UI's pagination."""
    sql = ("SELECT w.id, w.type, w.state, w.root_message_id, w.message_id, "
           "w.claimed_by, w.claimed_at, w.completed_at, w.enqueued_at, "
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
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += ("ORDER BY "
            "COALESCE(w.completed_at, w.claimed_at, w.enqueued_at) DESC, "
            "w.id DESC LIMIT ? OFFSET ?")
    params.extend([limit, offset])
    return [dict(r) for r in db.execute(sql, params)]


def queue_version(db, *, type=None, state=None):
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
    if where:
        sql += " WHERE " + " AND ".join(where)
    r = db.execute(sql, params).fetchone()
    return f"{r['t']}-{r['n']}-{r['s']}"


def work_items_for_node(db, claimed_by, *, limit=50):
    """Recent work-items claimed by a node, most-recent-claim first.
       `claimed_by` is whatever the api layer wrote into the column
       — the node's name when set, str(node.id) otherwise (see
       api.claim_task). Used by the node detail page to show
       per-node activity history."""
    rows = db.execute(
        "SELECT w.id, w.type, w.state, w.root_message_id, w.message_id, "
        "w.claimed_at, w.completed_at, w.enqueued_at, "
        "w.methodology_version, p.subject "
        "FROM work_items w "
        "LEFT JOIN patchsets p ON p.root_message_id=w.root_message_id "
        "WHERE w.claimed_by=? "
        "ORDER BY COALESCE(w.completed_at, w.claimed_at, w.enqueued_at) "
        "DESC, w.id DESC LIMIT ?",
        (claimed_by, limit))
    return [dict(r) for r in rows]


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
        "training_session_id, session_role, stratum_label "
        "FROM work_items WHERE root_message_id=? "
        "ORDER BY enqueued_at, id",
        (norm_msgid(root_message_id),))
    return [dict(r) for r in rows]


def count_work_items(db, *, type=None, state=None):
    """Total rows in `work_items` under the same filter `list_work_items`
       takes. The UI calls this to size the queue page's pagination
       (page X of Y, the page-number window)."""
    sql = "SELECT COUNT(*) AS n FROM work_items"
    params, where = [], []
    if type is not None:
        where.append("type=?"); params.append(type)
    if state is not None:
        where.append("state=?"); params.append(state)
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


def list_pending_enrollments(db):
    """Pending, unexpired enrollments awaiting an operator decision."""
    now = int(time.time())
    return [dict(r) for r in db.execute(
        "SELECT * FROM node_enrollments WHERE state=? AND expires_at>? "
        "ORDER BY created_at", (NODE_ENROLLMENT_STATE_PENDING, now))]


def set_enrollment_polled(db, enrollment_id, when=None):
    """Stamp `last_polled_at` - the token endpoint records each poll so a
       too-fast poll can be answered with `slow_down`."""
    db.execute("UPDATE node_enrollments SET last_polled_at=? WHERE id=?",
               (when if when is not None else int(time.time()), enrollment_id))
    db.commit()


def approve_enrollment(db, user_code, *, node_name=None, decided_by=None):
    """Operator approval: create the enrollment's `nodes` row and mark the
       enrollment approved. Returns the new node id. Raises KeyError if the
       user_code is unknown, ValueError if the enrollment is not pending or
       has expired, DuplicateNodeName if approving would create a
       second active node with the same name (the race-protection
       check that guards the create_enrollment/approve_enrollment
       window when two concurrent enrolments share a name)."""
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
    node_id = db.execute(
        "INSERT INTO nodes (name,task_types,state,enrolled_at) "
        "VALUES (?,?,?,?)",
        (resolved_name, enr["task_types"],
         NODE_STATE_ACTIVE, now)).lastrowid
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


def update_node_health(db, node_id, snapshot):
    """Stamp the latest health snapshot on a node row. `snapshot` is a
       JSON-serializable dict — the node's choice of fields; today
       the first cut is {free_disk_mb, refrepo_size_mb,
       last_anthropic_error}, but the wire is loose so adding
       fields later doesn't need a migration. Returns True when a
       row was updated, False when the node_id was unknown
       (revoked / deleted between request and write — best treated
       as a silent no-op)."""
    cur = db.execute(
        "UPDATE nodes SET health=?, health_at=? WHERE id=?",
        (json.dumps(snapshot), int(time.time()), node_id))
    db.commit()
    return cur.rowcount > 0


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
