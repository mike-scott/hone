"""hone-core configuration — read from the environment, with documented
defaults. Paths default under the mapped data volume ($HONE_DATA, default
/data); see ../ARCHITECTURE.md (Persistent storage, Auth/enrollment/transport)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    data_dir:             str   # HONE_DATA — the mapped persistent volume
    db_path:              str   # HONE_DB
    config_path:          str   # HONE_CONFIG — operator-tunable settings file
    methodology_dir:      str   # HONE_METHODOLOGY_DIR — import/export files
    archive_dir:          str   # HONE_ARCHIVE_DIR — gathered source archives
    cert_dir:             str   # HONE_CERT_DIR — the self-generated TLS material
    fleet_secret:         str   # HONE_FLEET_SECRET — the OAuth/enrollment gate
    admin_token:          str   # HONE_ADMIN_TOKEN — admin API credential
    http_port:            int   # HONE_HTTP_PORT — the port hone-core serves on
    hostname:             str   # HONE_HOSTNAME — the TLS cert / verification host
    public_url:           str   # HONE_PUBLIC_URL — base URL nodes/operators reach
    gather_interval:      int   # seconds between GATHER runs (default 600 = 10 min)
    lease_seconds:        int   # claim lease (default 1800 = 30 min)
    heartbeat_seconds:    int   # node heartbeat interval (default 300 = 5 min)
    access_token_ttl:     int   # HONE_ACCESS_TOKEN_TTL — node access-token life
    refresh_token_ttl:    int   # HONE_REFRESH_TOKEN_TTL — 0 = never expires
    device_code_ttl:      int   # HONE_DEVICE_CODE_TTL — the enrollment window
    device_poll_interval: int   # HONE_DEVICE_POLL_INTERVAL — node poll cadence

    @classmethod
    def from_env(cls) -> "Config":
        data = os.environ.get("HONE_DATA", "/data")
        host = os.environ.get("HONE_HOSTNAME", "localhost")
        port = int(os.environ.get("HONE_HTTP_PORT", "8000"))
        # The externally reachable port for the default public URL is the host
        # side of the container's port mapping (HONE_PUBLISH_PORT); a
        # non-containerised run has no mapping, so fall back to the listen port.
        ext_port = os.environ.get("HONE_PUBLISH_PORT") or str(port)
        return cls(
            data_dir             = data,
            db_path              = os.environ.get("HONE_DB", f"{data}/hone.db"),
            config_path          = os.environ.get("HONE_CONFIG", f"{data}/config.yaml"),
            methodology_dir      = os.environ.get("HONE_METHODOLOGY_DIR", f"{data}/methodology"),
            archive_dir          = os.environ.get("HONE_ARCHIVE_DIR", f"{data}/archive"),
            cert_dir             = os.environ.get("HONE_CERT_DIR", f"{data}/tls"),
            fleet_secret         = os.environ.get("HONE_FLEET_SECRET", ""),
            admin_token          = os.environ.get("HONE_ADMIN_TOKEN", ""),
            http_port            = port,
            hostname             = host,
            public_url           = (os.environ.get("HONE_PUBLIC_URL")
                                     or f"https://{host}:{ext_port}"),
            gather_interval      = int(os.environ.get("HONE_GATHER_INTERVAL", "600")),
            lease_seconds        = int(os.environ.get("HONE_LEASE_SECONDS", "1800")),
            heartbeat_seconds    = int(os.environ.get("HONE_HEARTBEAT_SECONDS", "300")),
            access_token_ttl     = int(os.environ.get("HONE_ACCESS_TOKEN_TTL", "3600")),
            refresh_token_ttl    = int(os.environ.get("HONE_REFRESH_TOKEN_TTL", "0")),
            device_code_ttl      = int(os.environ.get("HONE_DEVICE_CODE_TTL", "900")),
            device_poll_interval = int(os.environ.get("HONE_DEVICE_POLL_INTERVAL", "5")),
        )
