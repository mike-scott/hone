"""hone-core deployment configuration — read from the environment.

The settings here are fixed for the lifetime of a container: secrets, the TLS
identity, the listen port, and the data-volume paths. The operator-tunable
settings (GATHER cadence, token lifetimes, ...) are separate — see
runtime_config.py and ARCHITECTURE.md → Configuration & the Settings page."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    data_dir:        str   # HONE_DATA — the mapped persistent volume
    db_path:         str   # HONE_DB
    config_path:     str   # HONE_CONFIG — the operator-tunable config.yaml
    methodology_dir: str   # HONE_METHODOLOGY_DIR — import/export files
    archive_dir:     str   # HONE_ARCHIVE_DIR — gathered source archives
    cert_dir:        str   # HONE_CERT_DIR — the self-generated TLS material
    fleet_secret:    str   # HONE_FLEET_SECRET — the OAuth/enrollment gate
    admin_token:     str   # HONE_ADMIN_TOKEN — admin API credential
    session_secret:  str   # HONE_SESSION_SECRET — signs UI session cookies
    google_client_id:     str  # HONE_GOOGLE_CLIENT_ID — Google SSO (empty = disabled)
    google_client_secret: str  # HONE_GOOGLE_CLIENT_SECRET
    http_port:       int   # HONE_HTTP_PORT — the port hone-core serves on
    hostname:        str   # HONE_HOSTNAME — the TLS cert / verification host
    public_url:      str   # HONE_PUBLIC_URL — base URL nodes/operators reach
    session_cookie_secure: bool  # HONE_SESSION_COOKIE_SECURE — sets the
                                  # session cookie's Secure flag

    @staticmethod
    def _env_bool(name, default):
        """Lenient bool parse — true on '1'/'true'/'yes'/'on' (case-insensitive),
           otherwise false; unset → `default`. Centralised here so future bool
           settings stay consistent."""
        v = os.environ.get(name)
        if v is None:
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

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
            data_dir        = data,
            db_path         = os.environ.get("HONE_DB", f"{data}/hone.db"),
            config_path     = os.environ.get("HONE_CONFIG", f"{data}/config.yaml"),
            methodology_dir = os.environ.get("HONE_METHODOLOGY_DIR", f"{data}/methodology"),
            archive_dir     = os.environ.get("HONE_ARCHIVE_DIR", f"{data}/archive"),
            cert_dir        = os.environ.get("HONE_CERT_DIR", f"{data}/tls"),
            fleet_secret    = os.environ.get("HONE_FLEET_SECRET", ""),
            admin_token     = os.environ.get("HONE_ADMIN_TOKEN", ""),
            session_secret  = os.environ.get("HONE_SESSION_SECRET", ""),
            google_client_id     = os.environ.get("HONE_GOOGLE_CLIENT_ID", ""),
            google_client_secret = os.environ.get("HONE_GOOGLE_CLIENT_SECRET", ""),
            http_port       = port,
            hostname        = host,
            public_url      = (os.environ.get("HONE_PUBLIC_URL")
                               or f"https://{host}:{ext_port}"),
            # Secure-by-default: the session cookie is only sent over HTTPS.
            # hone-core serves HTTPS directly (the self-generated cert), so
            # this works in dev too; flip to false only when fronting hone-core
            # with a proxy that speaks HTTP to the backend.
            session_cookie_secure = cls._env_bool(
                "HONE_SESSION_COOKIE_SECURE", default=True),
        )
