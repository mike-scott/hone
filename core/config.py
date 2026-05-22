"""hone-core configuration — read from the environment, with documented
defaults. Paths default under the mapped data volume ($HONE_DATA, default
/data); see ../ARCHITECTURE.md (Persistent storage)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    data_dir:          str   # HONE_DATA — the mapped persistent volume
    db_path:           str   # HONE_DB
    config_path:       str   # HONE_CONFIG — operator-tunable settings file
    methodology_dir:   str   # HONE_METHODOLOGY_DIR — import/export files
    archive_dir:       str   # HONE_ARCHIVE_DIR — gathered source archives
    fleet_secret:      str   # HONE_FLEET_SECRET — node transport gate
    admin_token:       str   # HONE_ADMIN_TOKEN — admin API credential
    gather_interval:   int   # seconds between GATHER runs (default 600 = 10 min)
    lease_seconds:     int   # claim lease (default 1800 = 30 min)
    heartbeat_seconds: int   # node heartbeat interval (default 300 = 5 min)

    @classmethod
    def from_env(cls) -> "Config":
        data = os.environ.get("HONE_DATA", "/data")
        return cls(
            data_dir          = data,
            db_path           = os.environ.get("HONE_DB", f"{data}/hone.db"),
            config_path       = os.environ.get("HONE_CONFIG", f"{data}/config.yaml"),
            methodology_dir   = os.environ.get("HONE_METHODOLOGY_DIR", f"{data}/methodology"),
            archive_dir       = os.environ.get("HONE_ARCHIVE_DIR", f"{data}/archive"),
            fleet_secret      = os.environ.get("HONE_FLEET_SECRET", ""),
            admin_token       = os.environ.get("HONE_ADMIN_TOKEN", ""),
            gather_interval   = int(os.environ.get("HONE_GATHER_INTERVAL", "600")),
            lease_seconds     = int(os.environ.get("HONE_LEASE_SECONDS", "1800")),
            heartbeat_seconds = int(os.environ.get("HONE_HEARTBEAT_SECONDS", "300")),
        )
