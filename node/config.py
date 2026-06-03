"""hone-node configuration — read from the environment, with documented
defaults. A node starts from scratch given only the three required settings;
it enrolls into the fleet on first start (see node/.env.example and
../docs/ARCHITECTURE.md → hone-node / Auth, enrollment & transport)."""
import os
import socket
from dataclasses import dataclass

from node import cgit

# The supported Claude backends. `sdk` calls the Anthropic Python SDK and
# requires ANTHROPIC_API_KEY (standard API-billing path). `cli`
# subprocesses the `claude` CLI binary and uses whatever OAuth session
# is in $HOME/.claude — intended for Claude Code subscribers who don't
# have API billing set up. See docs/DEPLOYMENT.md → Backends.
CLAUDE_BACKENDS = ("sdk", "cli")

# How the node validates hone-core's TLS certificate (HONE_CORE_TLS):
#   adopt  — trust-on-first-use: adopt + pin the self-signed CA hone-core
#            hands out at enrollment. The default; works against hone-core
#            serving HTTPS directly (a dev box, no proxy).
#   system — validate against the OS trust store. For hone-core behind a
#            reverse proxy (Caddy/nginx) terminating TLS with a publicly
#            trusted cert (Let's Encrypt); no CA is adopted or pinned.
CORE_TLS_MODES = ("adopt", "system")


@dataclass(frozen=True)
class Config:
    core_url:           str    # HONE_CORE_URL — the hone-core base URL
    fleet_secret:       str    # HONE_FLEET_SECRET — gates the enrollment API
    anthropic_api_key:  str    # ANTHROPIC_API_KEY — Claude API token (sdk only)
    anthropic_model:    str    # ANTHROPIC_MODEL — model id; empty = code default
    claude_backend:     str    # HONE_CLAUDE_BACKEND — 'sdk' | 'cli'
    node_name:          str    # HONE_NODE_NAME — label shown to the operator
    data_dir:           str    # HONE_DATA — the mapped persistent volume
    repo_dir:           str    # HONE_REPO_DIR — the reference kernel repo
    cgit_trees:         tuple  # HONE_CGIT_TREES — ordered (name,url) trees
                                # prepare's deterministic phase probes for
                                # a declared base commit
    scratch_dir:        str    # HONE_SCRATCH_DIR — in-flight work across outages
    identity_path:      str    # HONE_IDENTITY — persisted bearer tokens
    ca_cert_path:       str    # HONE_CORE_CA — hone-core's CA, got at enrollment
                                # (adopt mode only)
    tls_mode:           str    # HONE_CORE_TLS — 'adopt' (pin self-signed CA)
                                # | 'system' (OS trust store, for a proxy)
    poll_interval:      int    # seconds to wait after an empty claim (204)
    backoff_initial:    float  # initial transient-failure backoff, seconds
    backoff_max:        float  # maximum transient-failure backoff, seconds
    heartbeat_interval: int    # seconds between claim heartbeats
    cli_timeout:        int    # HONE_CLI_TIMEOUT — max seconds for one
                                # `claude` CLI turn before the watchdog kills
                                # the wedged subprocess (cli backend only)
    repo_gc_threshold_mb: int  # HONE_REPO_GC_THRESHOLD_MB — gc the reference
                                # repo once it grows past this, bounding the
                                # daily-rebase churn arbitrary base fetches
                                # accrete; 0 disables the size trigger
    repo_gc_every:      int    # HONE_REPO_GC_EVERY — also gc every N completed
                                # tasks regardless of size (0 disables)
    min_free_disk_mb:   int    # HONE_MIN_FREE_DISK_MB — pause claiming while
                                # free space on the data volume is below this,
                                # so a base fetch / ~1.5 GB review worktree
                                # can't fail mid-task with ENOSPC; 0 disables

    @classmethod
    def from_env(cls) -> "Config":
        backend = os.environ.get("HONE_CLAUDE_BACKEND", "sdk").lower()
        if backend not in CLAUDE_BACKENDS:
            raise RuntimeError(
                f"HONE_CLAUDE_BACKEND={backend!r} unsupported; "
                f"expected one of {CLAUDE_BACKENDS}")
        # ANTHROPIC_API_KEY is required only for the SDK backend; the
        # CLI backend reads OAuth credentials out of $HOME/.claude.
        required = ["HONE_CORE_URL", "HONE_FLEET_SECRET"]
        if backend == "sdk":
            required.append("ANTHROPIC_API_KEY")
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                "missing required environment: " + ", ".join(missing))
        tls_mode = os.environ.get("HONE_CORE_TLS", "adopt").lower()
        if tls_mode not in CORE_TLS_MODES:
            raise RuntimeError(
                f"HONE_CORE_TLS={tls_mode!r} unsupported; "
                f"expected one of {CORE_TLS_MODES}")
        data = os.environ.get("HONE_DATA", "/data")
        return cls(
            core_url           = os.environ["HONE_CORE_URL"].rstrip("/"),
            fleet_secret       = os.environ["HONE_FLEET_SECRET"],
            anthropic_api_key  = os.environ.get("ANTHROPIC_API_KEY", ""),
            anthropic_model    = os.environ.get("ANTHROPIC_MODEL", "").strip(),
            claude_backend     = backend,
            node_name          = os.environ.get("HONE_NODE_NAME",
                                                socket.gethostname()),
            data_dir           = data,
            repo_dir           = os.environ.get("HONE_REPO_DIR", f"{data}/linux"),
            cgit_trees         = tuple(cgit.parse_trees_env(
                                          os.environ.get("HONE_CGIT_TREES"))),
            scratch_dir        = os.environ.get("HONE_SCRATCH_DIR", f"{data}/scratch"),
            identity_path      = os.environ.get("HONE_IDENTITY", f"{data}/identity.json"),
            ca_cert_path       = os.environ.get("HONE_CORE_CA", f"{data}/core-ca.crt"),
            tls_mode           = tls_mode,
            poll_interval      = int(os.environ.get("HONE_POLL_INTERVAL", "60")),
            backoff_initial    = float(os.environ.get("HONE_BACKOFF_INITIAL", "1")),
            backoff_max        = float(os.environ.get("HONE_BACKOFF_MAX", "300")),
            heartbeat_interval = int(os.environ.get("HONE_HEARTBEAT_INTERVAL", "300")),
            cli_timeout        = int(os.environ.get("HONE_CLI_TIMEOUT", "3600")),
            repo_gc_threshold_mb = int(os.environ.get(
                                          "HONE_REPO_GC_THRESHOLD_MB", "20000")),
            repo_gc_every      = int(os.environ.get("HONE_REPO_GC_EVERY", "25")),
            min_free_disk_mb   = int(os.environ.get("HONE_MIN_FREE_DISK_MB",
                                                    "5000")),
        )
