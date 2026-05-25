#!/usr/bin/env python3
"""gather_api.py - the hone gather-module public API.

A *gather module* is one data source for hone: it surfaces kernel patchsets,
the patch messages that make them up, and review comments on those messages
— from lore.kernel.org's public-inbox archive. The framework
(see core/gather.py) drives the modules and handles ingest into hone.db;
a module's job is to yield refs in its native order.

To add a gather module, drop `core/gather-modules/<name>.py` in this
directory with a class that subclasses `GatherModule` and implements
`list(state)`, and end the file with `run_cli(<YourClass>())` so it can be
run as a CLI:

    from gather_api import (GatherModule, PatchsetRef, MessageRef,
                            GatherState, run_cli)

    class MyModule(GatherModule):
        name = "my-source"
        since_date = "2026-01-01"
        def list(self, state=None):
            ...
            yield PatchsetRef(root_message_id="<root@x>", cursor="...")
            yield MessageRef(message_id="<m@x>", root_message_id="<root@x>",
                             type=2, body="...", cursor="...")

    if __name__ == "__main__":
        run_cli(MyModule())

The data types crossing the API boundary are the dataclasses `PatchsetRef`
and `MessageRef`. The CLI shim JSON-encodes each yielded ref on its own line.
In-process, `available()` lists modules and `load(name)` returns a ready
instance.
"""
# Deferred annotation evaluation — GatherModule defines a `list()` method,
# which shadows the `list` builtin for the rest of the class body; without
# this, evaluating a later `list[...]` annotation at class-definition time
# raises TypeError on Python < 3.14 (where annotations are still eager).
from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import json
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

MODULES_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class PatchsetRef:
    """A patchset surfaced by a module — its metadata + list-tag set.

       A standalone `[PATCH]` is a 1-patch patchset whose `root_message_id`
       equals the patch message's own Message-ID; n_patches=1; no separate
       cover letter."""
    root_message_id: str                 # cross-source dedup key
    subject: str = ""
    submitter_email: str = ""
    sent: int | None = None              # unix time of the root message
    n_patches: int | None = None         # 1 for a single [PATCH]
    base_commit: str | None = None       # declared base, if known
    change_id: str | None = None         # b4 Change-Id; links revisions
    series_version: int = 1
    list_tags: list[str] = field(default_factory=list)
    skip_reason: str | None = None       # set => the framework marks skipped
    cursor: str = ""                     # opaque, module-defined resume token


@dataclass
class MessageRef:
    """A thread email — a patch, cover letter, or review comment."""
    message_id: str
    root_message_id: str                 # the patchset this message belongs to
    type: int                            # MSG_TYPE_COVER | PATCH | COMMENT
    body: str                            # raw email; for a comment, the
                                         # inline-annotated reply
    part_index: int | None = None        # 0 cover; 1..M series; None for a
                                         # standalone [PATCH] and for comments
    parent_message_id: str | None = None # comment: the patch/cover it
                                         # pertains to (resolved up the
                                         # In-Reply-To chain)
    author_name: str = ""
    author_email: str = ""
    subject: str = ""
    sent: int | None = None
    cursor: str = ""                     # opaque, module-defined resume token


@dataclass
class GatherState:
    """A gather module's watermark — where its last pass left off. `cursor`
       is an opaque, module-defined resume token; the framework persists it
       per source in hone.db (`gather_state`) and hands it back to `list()`,
       so the next pass resumes after it. '' means start from `since_date`."""
    cursor: str = ""


class GatherModule(ABC):
    """The contract every gather module implements: `name` and `since_date`
       are class attributes; `list()` is the data interface."""

    name: str = ""        # registry id; must equal the module file's basename
    since_date: str = ""  # ISO 'YYYY-MM-DD' — oldest patchset/message date
                          # this module gathers; '' means no floor

    def since_ts(self) -> int | None:
        """`since_date` as a unix timestamp (UTC midnight), or None if unset.
           A module's `list()` uses it to drop refs below the floor."""
        if not self.since_date:
            return None
        return int(datetime.datetime.strptime(self.since_date, "%Y-%m-%d")
                   .replace(tzinfo=datetime.timezone.utc).timestamp())

    @abstractmethod
    def list(self, state: GatherState | None = None, db=None
             ) -> Iterator[PatchsetRef | MessageRef]:
        """Yield a stream of refs in module order — a `PatchsetRef`
           introduces a patchset (and must precede the `MessageRef`s that
           belong to it); a `MessageRef` adds a thread email (patch, cover,
           or comment). For a comment, `parent_message_id` must already exist
           in the corpus or appear earlier in this stream.

           When `state.cursor` is set — the previous pass's watermark — the
           module resumes after it. Each yielded ref should carry its own
           `cursor` (the resume token to record once that ref is gathered);
           the framework persists it as the new watermark.

           `db`, if given, is the framework's hone.db connection — modules
           that need to resolve refs against the corpus (e.g. a late comment
           looking up which patchset its parent message belongs to) use it
           read-only. Modules that need no corpus state ignore it."""


def run_cli(module: GatherModule, argv: list[str] | None = None) -> None:
    """Shared CLI shim — lets a module file run as
       `python3 core/gather-modules/<name>.py list [CURSOR]`. Each yielded
       ref is JSON-encoded on its own line with its `kind` distinguished."""
    argv = list(sys.argv if argv is None else argv)
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "list":
        state = GatherState(cursor=argv[2]) if len(argv) > 2 else None
        for ref in module.list(state):       # CLI: no db handed to the module
            kind = "patchset" if isinstance(ref, PatchsetRef) else "message"
            print(json.dumps({"kind": kind, **dataclasses.asdict(ref)}))
    else:
        sys.exit(f"usage: {module.name or 'module'} list [CURSOR]")


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
