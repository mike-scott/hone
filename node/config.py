"""hone-node configuration — read from the environment, with documented
defaults. A node starts from scratch given only the three required settings;
it enrolls into the fleet on first start (see node/.env.example and
../docs/ARCHITECTURE.md → hone-node / Auth, enrollment & transport)."""
import os
import socket
from dataclasses import dataclass

_REQUIRED = ("HONE_CORE_URL", "HONE_FLEET_SECRET", "ANTHROPIC_API_KEY")


@dataclass(frozen=True)
class Config:
    core_url:           str    # HONE_CORE_URL — the hone-core base URL
    fleet_secret:       str    # HONE_FLEET_SECRET — gates the enrollment API
    anthropic_api_key:  str    # ANTHROPIC_API_KEY — the Claude API token
    node_name:          str    # HONE_NODE_NAME — label shown to the operator
    data_dir:           str    # HONE_DATA — the mapped persistent volume
    repo_dir:           str    # HONE_REPO_DIR — the reference kernel repo
    scratch_dir:        str    # HONE_SCRATCH_DIR — in-flight work across outages
    identity_path:      str    # HONE_IDENTITY — persisted bearer tokens
    ca_cert_path:       str    # HONE_CORE_CA — hone-core's CA, got at enrollment
    poll_interval:      int    # seconds to wait after an empty claim (204)
    backoff_initial:    float  # initial transient-failure backoff, seconds
    backoff_max:        float  # maximum transient-failure backoff, seconds
    heartbeat_interval: int    # seconds between claim heartbeats

    @classmethod
    def from_env(cls) -> "Config":
        missing = [k for k in _REQUIRED if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                "missing required environment: " + ", ".join(missing))
        data = os.environ.get("HONE_DATA", "/data")
        return cls(
            core_url           = os.environ["HONE_CORE_URL"].rstrip("/"),
            fleet_secret       = os.environ["HONE_FLEET_SECRET"],
            anthropic_api_key  = os.environ["ANTHROPIC_API_KEY"],
            node_name          = os.environ.get("HONE_NODE_NAME",
                                                socket.gethostname()),
            data_dir           = data,
            repo_dir           = os.environ.get("HONE_REPO_DIR", f"{data}/linux"),
            scratch_dir        = os.environ.get("HONE_SCRATCH_DIR", f"{data}/scratch"),
            identity_path      = os.environ.get("HONE_IDENTITY", f"{data}/identity.json"),
            ca_cert_path       = os.environ.get("HONE_CORE_CA", f"{data}/core-ca.crt"),
            poll_interval      = int(os.environ.get("HONE_POLL_INTERVAL", "60")),
            backoff_initial    = float(os.environ.get("HONE_BACKOFF_INITIAL", "1")),
            backoff_max        = float(os.environ.get("HONE_BACKOFF_MAX", "300")),
            heartbeat_interval = int(os.environ.get("HONE_HEARTBEAT_INTERVAL", "300")),
        )
