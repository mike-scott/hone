"""hone-core — the operator-tunable runtime configuration.

The settings an operator may change on a running instance — GATHER cadence,
claim lease, token lifetimes, the merge-gate redraft cap. They live in a YAML
file on the data volume (config.py's `config_path` / HONE_CONFIG, default
/data/config.yaml); the operator web UI's Settings page edits them; they apply
without a restart. Distinct from config.py, which holds the deployment
configuration (env vars: secrets, TLS, ports, paths). See ARCHITECTURE.md →
Configuration & the Settings page.
"""
import copy
import logging
import os

import yaml

log = logging.getLogger("hone.config")

# The canonical structure and built-in defaults — config.yaml is this overlaid
# with the operator's edits.
DEFAULTS = {
    "gather": {
        "interval_seconds": 600,
        "sources": [],                  # the enabled gather sources ([] = none)
    },
    "work_queue": {
        "lease_seconds": 1800,
        "heartbeat_seconds": 300,
    },
    "enrollment": {
        "access_token_ttl": 3600,
        "refresh_token_ttl": 0,         # 0 = no expiry
        "device_code_ttl": 900,
        "device_poll_interval": 5,
    },
    "merge_gate": {
        "redraft_cap": 3,
    },
}


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()]


# (group, key) -> (env var, parser). On first run an env var that is set seeds
# its key, so an existing env-configured deployment keeps its values.
_ENV_SEEDS = {
    ("gather", "interval_seconds"):         ("HONE_GATHER_INTERVAL", int),
    ("gather", "sources"):                  ("HONE_GATHER_SOURCES", _csv),
    ("work_queue", "lease_seconds"):        ("HONE_LEASE_SECONDS", int),
    ("work_queue", "heartbeat_seconds"):    ("HONE_HEARTBEAT_SECONDS", int),
    ("enrollment", "access_token_ttl"):     ("HONE_ACCESS_TOKEN_TTL", int),
    ("enrollment", "refresh_token_ttl"):    ("HONE_REFRESH_TOKEN_TTL", int),
    ("enrollment", "device_code_ttl"):      ("HONE_DEVICE_CODE_TTL", int),
    ("enrollment", "device_poll_interval"): ("HONE_DEVICE_POLL_INTERVAL", int),
    ("merge_gate", "redraft_cap"):          ("HONE_REDRAFT_CAP", int),
}


# Settings-form schema — drives the operator UI's Settings page and the
# validation of a submission. `kind`: "int" (a positive integer), "int0"
# (>= 0), "sources" (a toggle per installed gather module).
FIELDS = [
    ("gather", "interval_seconds", "GATHER cadence", "seconds", "int"),
    ("gather", "sources", "Enabled gather sources",
     "tick the sources GATHER pulls from; untick all to pause gathering",
     "sources"),
    ("work_queue", "lease_seconds", "Claim lease", "seconds", "int"),
    ("work_queue", "heartbeat_seconds", "Heartbeat interval", "seconds", "int"),
    ("enrollment", "access_token_ttl", "Access-token TTL", "seconds", "int"),
    ("enrollment", "refresh_token_ttl", "Refresh-token TTL",
     "seconds (0 = never expires)", "int0"),
    ("enrollment", "device_code_ttl", "Device-code TTL", "seconds", "int"),
    ("enrollment", "device_poll_interval", "Device poll interval",
     "seconds", "int"),
    ("merge_gate", "redraft_cap", "Merge-gate redraft cap", "count", "int"),
]


class RuntimeConfig:
    """The resolved operator-tunable config — the defaults overlaid with the
       config.yaml file. Flat properties expose the values consumers use."""

    def __init__(self, data):
        self._data = data

    # --- gather ---
    @property
    def gather_interval(self): return self._data["gather"]["interval_seconds"]

    @property
    def gather_sources(self): return tuple(self._data["gather"]["sources"])

    # --- work queue ---
    @property
    def lease_seconds(self): return self._data["work_queue"]["lease_seconds"]

    @property
    def heartbeat_seconds(self):
        return self._data["work_queue"]["heartbeat_seconds"]

    # --- enrollment ---
    @property
    def access_token_ttl(self):
        return self._data["enrollment"]["access_token_ttl"]

    @property
    def refresh_token_ttl(self):
        return self._data["enrollment"]["refresh_token_ttl"]

    @property
    def device_code_ttl(self):
        return self._data["enrollment"]["device_code_ttl"]

    @property
    def device_poll_interval(self):
        return self._data["enrollment"]["device_poll_interval"]

    # --- merge gate ---
    @property
    def redraft_cap(self): return self._data["merge_gate"]["redraft_cap"]

    def as_dict(self):
        """A deep copy of the nested config — for the Settings page / export."""
        return copy.deepcopy(self._data)


def _merge(base, overlay):
    """Deep-overlay `overlay` onto a copy of `base`. Only keys present in
       `base` are taken, and only when the value keeps the default's type — so
       an unknown or malformed entry in a hand-edited file is ignored, not a
       crash."""
    out = copy.deepcopy(base)
    for group, vals in (overlay or {}).items():
        if group not in out or not isinstance(vals, dict):
            continue
        for key, value in vals.items():
            if key not in out[group]:
                continue
            if type(value) is type(out[group][key]):
                out[group][key] = value
            else:
                log.warning("config.yaml: %s.%s has the wrong type — "
                            "keeping the default", group, key)
    return out


def _seed_from_env(all_sources=None):
    """The defaults with any env-var-set tunable overriding its key — the
       first-run seed, so an env-configured deployment keeps its values. When
       `all_sources` is given, the enabled sources default to every installed
       module (HONE_GATHER_SOURCES still overrides, if set)."""
    seeded = copy.deepcopy(DEFAULTS)
    if all_sources is not None:
        seeded["gather"]["sources"] = list(all_sources)
    for (group, key), (env, parse) in _ENV_SEEDS.items():
        raw = os.environ.get(env)
        if raw not in (None, ""):
            try:
                seeded[group][key] = parse(raw)
            except (ValueError, TypeError):
                log.warning("ignoring malformed %s=%r", env, raw)
    return seeded


def _write(path, data):
    """Atomically write `data` as YAML to `path`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.replace(tmp, path)


def load(path, all_sources=None):
    """Resolve the runtime config. If `path` exists it is the defaults overlaid
       with that file. If not — first run — write a fresh config.yaml seeded
       from the defaults plus any tunable env vars (the enabled sources
       defaulting to `all_sources`, every installed module), and use that.
       Returns a RuntimeConfig."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = _merge(DEFAULTS, yaml.safe_load(f) or {})
        log.info("runtime config loaded from %s", path)
    else:
        data = _seed_from_env(all_sources)
        _write(path, data)
        log.info("runtime config initialised at %s", path)
    return RuntimeConfig(data)


def save(path, runtime_config):
    """Persist a RuntimeConfig to `path` — the Settings page calls this."""
    _write(path, runtime_config.as_dict())


def parse_form(form, valid_sources=None):
    """Validate a Settings-page submission. `form` maps each 'group.key' to its
       posted value. Returns (RuntimeConfig, {}) on success, or
       (None, {'group.key': message}) on failure — the caller writes
       config.yaml only on success. `valid_sources`, if given, constrains the
       gather sources to the installed modules."""
    data = copy.deepcopy(DEFAULTS)
    errors = {}
    for group, key, label, _unit, kind in FIELDS:
        name = f"{group}.{key}"
        if kind == "sources":
            # checkbox group — the posted values are the ticked sources;
            # none posted == all unticked == an empty (paused) set.
            data[group][key] = [s for s in form.getlist(name)
                                if valid_sources is None or s in valid_sources]
            continue
        raw = (form.get(name) or "").strip()
        try:
            value = int(raw)
        except ValueError:
            errors[name] = f"{label} must be a whole number"
            continue
        floor = 0 if kind == "int0" else 1
        if value < floor:
            errors[name] = f"{label} must be {floor} or more"
        else:
            data[group][key] = value
    return (None, errors) if errors else (RuntimeConfig(data), {})
