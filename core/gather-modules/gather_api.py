#!/usr/bin/env python3
"""gather_api.py - the hone gather-module public API.

A *gather module* is one data source for hone: it surfaces
kernel patchsets and the external review signal on them (an AI review bot, or
human reviewers on a mailing list). Every module in this directory implements
the same contract, so the loop (PROCEDURE.md) stays source-agnostic.

To add a gather module, drop `core/gather-modules/<name>.py` in this directory with
a class that subclasses `GatherModule` and implements its four abstract
methods, and end the file with `run_cli(<YourClass>())` so it is runnable as
`python3 core/gather-modules/<name>.py <list|pull|findings|base|inline>`:

    from gather_api import GatherModule, PatchsetRef, Finding, run_cli

    class MyModule(GatherModule):
        name = "my-source"        # registry id; must equal the file basename
        kind = "human"            # 'ai' | 'human'
        def list(self): ...
        def pull(self, patchset_id, dest_dir): ...
        def findings(self, patchset_id): ...
        def base(self, patchset_id): ...

    if __name__ == "__main__":
        run_cli(MyModule())

The data types crossing the API boundary are the dataclasses `PatchsetRef`
and `Finding` below. The CLI shim serialises them to JSON — one object per
line for `list`, a JSON array for `findings`. In-process, use `load(name)` to
get a ready module instance and `available()` to enumerate modules.
"""
# Deferred annotation evaluation — GatherModule defines a `list()` method,
# which shadows the `list` builtin for the rest of the class body; without
# this, evaluating a later `-> list[str]` annotation at class-definition time
# raises TypeError on Python < 3.14 (where annotations are still eager).
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

MODULES_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class PatchsetRef:
    """One patchset surfaced by a module's `list()`."""
    id: str                          # the module's native patchset id;
                                     # pull()/findings()/base() take this
    root_message_id: str             # the patchset's root Message-ID — the
                                     # cross-source hone.db dedup key
    subject: str = ""
    sent: int | None = None          # unix time of the root / cover message
    n_replies: int | None = None     # human sources: review-reply count
    skip_reason: str | None = None   # set => the gather phase must skip-flag
                                     # this patchset, not pull it
    extra: dict = field(default_factory=dict)   # source-specific metadata


@dataclass
class Finding:
    """One external review item — an AI finding, or a human reviewer reply."""
    reviewer: str                    # reviewer name ('sashiko' for the bot)
    type: str                        # 'ai' | 'human' (== the module's kind)
    text: str                        # finding text, or reviewer reply body
    severity: str | None = None      # ai: structured; human: None (a human
                                     # reply's points are extracted downstream)
    preexisting: bool = False
    reviewer_email: str | None = None   # human-source attribution
    message_id: str | None = None       # the review reply's Message-ID (human)
    date: int | None = None             # unix time the review was sent
    date_ok: bool = True                # False => no resolvable Date
    extra: dict = field(default_factory=dict)   # source-specific metadata


class GatherModule(ABC):
    """The contract every gather module implements. `name` and `kind` are
       class attributes; the four abstract methods are the data interface."""

    name: str = ""        # registry id; must equal the module file's basename
    kind: str = ""        # 'ai' | 'human'

    @abstractmethod
    def list(self) -> list[PatchsetRef]:
        """All qualifying patchsets this source currently offers, oldest
           first. A ref with `skip_reason` set is flagged for the gather
           phase to skip-flag rather than pull."""

    @abstractmethod
    def pull(self, patchset_id: str, dest_dir: str) -> list[str]:
        """Reconstruct the patchset's patches into dest_dir as
           patch0.patch .. patchN.patch (git-am-able). Return the paths."""

    @abstractmethod
    def findings(self, patchset_id: str) -> list[Finding]:
        """The source's external review of the patchset — to be revealed and
           verified AFTER our blind review (see PROCEDURE.md)."""

    @abstractmethod
    def base(self, patchset_id: str) -> str | None:
        """The review's baseline commit, if the source states one."""

    def inline(self, patchset_id: str) -> str | None:
        """The source's free-text inline review, if it has one. Optional —
           the default is None."""
        return None


def run_cli(module: GatherModule, argv: list[str] | None = None) -> None:
    """Shared CLI shim — lets a module file run as
       `python3 core/gather-modules/<name>.py <verb> [args]`."""
    argv = list(sys.argv if argv is None else argv)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "list":
        for ref in module.list():
            print(json.dumps(dataclasses.asdict(ref)))
    elif cmd == "pull" and len(argv) >= 4:
        for path in module.pull(argv[2], argv[3]):
            print(path)
    elif cmd == "findings" and len(argv) >= 3:
        print(json.dumps([dataclasses.asdict(f)
                          for f in module.findings(argv[2])], indent=2))
    elif cmd == "base" and len(argv) >= 3:
        print(json.dumps({"base_commit": module.base(argv[2])}))
    elif cmd == "inline" and len(argv) >= 3:
        print(module.inline(argv[2]) or "")
    else:
        sys.exit(f"usage: {module.name or 'module'} "
                 f"<list | pull ID DIR | findings ID | base ID | inline ID>")


def available() -> list[str]:
    """Names of the gather modules in this directory."""
    return sorted(
        f[:-3] for f in os.listdir(MODULES_DIR)
        if f.endswith(".py") and not f.startswith(("_", "."))
        and f != "gather_api.py")


def load(name: str) -> GatherModule:
    """Import core/gather-modules/<name>.py and return an instance of its
       GatherModule subclass. The directory name has a hyphen, so modules are
       loaded by file path rather than as a dotted package."""
    path = os.path.join(MODULES_DIR, name + ".py")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no gather module {name!r}: {path}")
    if MODULES_DIR not in sys.path:
        sys.path.insert(0, MODULES_DIR)          # so the module finds gather_api
    spec = importlib.util.spec_from_file_location(
        "gathermod_" + name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for obj in vars(mod).values():
        if (isinstance(obj, type) and obj is not GatherModule
                and issubclass(obj, GatherModule)):
            return obj()
    raise TypeError(f"{name}: no GatherModule subclass defined")


if __name__ == "__main__":
    print(__doc__)
    print("available gather modules:", ", ".join(available()) or "(none)")
