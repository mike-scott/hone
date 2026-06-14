"""hone-node Claude API wrapper.

One entry point — `call_claude(cfg, system, user_text, max_tokens)` —
hides the Anthropic SDK behind a tiny function the task handlers (and
the tests) depend on. The handlers compose the methodology guidance
plus a per-task payload; this module shapes them into a messages-API
call, returns the model's text response plus token-accounting metadata.

Why a thin wrapper instead of calling the SDK from each handler:

  - Tests monkeypatch one symbol (`node.ai.call_claude`) to drive
    every handler against a stub response — no per-test SDK mock.
  - A single chokepoint to retry transient SDK errors, swap in a
    different vendor, or layer in tracing later.
  - Lazy-imports the SDK so an import of `node.tasks` never pulls
    in the Anthropic client at module-load time — useful in tests
    and in dry-run / `--help` style invocations.
"""
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from node import budget

log = logging.getLogger("hone.node.ai")


# node/health.py reads this — the last non-success category from a
# call_claude attempt, or None when the most recent call completed
# cleanly. Cleared on success so the operator's health snapshot
# reflects "current state" rather than "ever seen". Categories:
#   - "auth"        — AuthenticationError / PermissionDeniedError
#   - "rate_limit"  — anthropic 429
#   - "connection"  — APIConnectionError / APITimeoutError
#   - "other"       — anything else the SDK raises
_LAST_ERROR = None


def get_last_error():
    """The latest non-success Anthropic error category, or None when
       the most recent call_claude returned cleanly. Used by
       node/health.collect to populate the health snapshot's
       `last_anthropic_error` field."""
    return _LAST_ERROR


def _record_outcome(category):
    """Update the per-process last-error slot. category=None means a
       successful call landed (clear). Any string is recorded as the
       latest failure category."""
    global _LAST_ERROR
    _LAST_ERROR = category


class CallClaudeAuthError(Exception):
    """The Claude API rejected the configured key (HTTP 401 / 403).

       Translated from the SDK's AuthenticationError / PermissionDeniedError
       so the runner can catch a domain-level exception without importing
       the Anthropic SDK and can present a one-line operator-facing error
       in the container log — rather than a 30-line traceback whose root
       cause is buried at the bottom.

       This is configuration-fatal: there is no retry that resolves it.
       The operator must fix the key; the node exits and the container
       orchestrator (Docker / Kubernetes) restarts per its policy,
       producing the same clean error each time until the key is
       valid."""


class CallClaudeError(RuntimeError):
    """A Claude call that *ran* but produced no usable answer — the CLI
       exited non-zero (non-auth), timed out, or its stream ended without
       a success result.

       Unlike CallClaudeAuthError this is NOT configuration-fatal: it's a
       per-task failure. It carries whatever the turn produced before it
       failed (the partial `trace`, the CLI's `stderr`, the failure
       `category`, the wall-clock `duration_ms`) so the handler can submit
       a failure-outcome record — surfacing the attempt (and its agent
       trace) into the corpus — instead of crashing the claim loop."""

    def __init__(self, message, *, category="other", returncode=None,
                 stderr="", trace=None, duration_ms=0, model=None):
        super().__init__(message)
        self.category = category
        self.returncode = returncode
        self.stderr = stderr
        self.trace = trace or []
        self.duration_ms = duration_ms
        self.model = model


# Default model. Resolution order in call_claude: explicit `model=`
# kwarg → cfg.anthropic_model (ANTHROPIC_MODEL env) → DEFAULT_MODEL.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Output budget for a single completion. Prepare / review / train / draft
# all fit comfortably under this; bumped only if a future task's
# return-contract needs more.
DEFAULT_MAX_TOKENS = 8000

# Claude is asked for "raw JSON only — no prose, no markdown fences" in
# every operation's return contract, but practical resilience: if the
# model wraps the JSON in ```json ... ``` fences regardless, strip them
# before the caller's json.loads.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$",
                       re.DOTALL | re.IGNORECASE)


def _strip_fences(text):
    """Return `text` with a surrounding ```json ... ``` (or plain ```) code
       fence removed if present, otherwise unchanged. Defensive — the
       methodology's return contracts explicitly forbid fences, but
       LLM compliance with negative instructions is not absolute."""
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


# How often to log a "still working" heartbeat while a Claude call runs.
# Both backends are silent for the whole turn — the CLI captures (doesn't
# stream) its output, and the SDK blocks on one HTTPS call — so without
# this a console viewer sees a frozen log while Claude thinks. A periodic
# elapsed-time line shows the node is alive and how long it's been going.
_HEARTBEAT_SECONDS = 15


@contextlib.contextmanager
def _heartbeat(label, started):
    """Log `<label> … still working (Ns elapsed)` every _HEARTBEAT_SECONDS
       until the block exits (on return or raise). A daemon thread, so it
       never keeps the process alive; the stop is a threading.Event set in
       the finally."""
    done = threading.Event()

    def beat():
        while not done.wait(_HEARTBEAT_SECONDS):
            log.info("%s … still working (%.0fs elapsed)",
                     label, time.monotonic() - started)

    threading.Thread(target=beat, name="claude-heartbeat", daemon=True).start()
    try:
        yield
    finally:
        done.set()


def call_claude(cfg, system, user_text, *, model=None,
                max_tokens=DEFAULT_MAX_TOKENS, tools=None, cwd=None):
    """Call Claude and return the response, dispatching on
       cfg.claude_backend:

         - "sdk": the Anthropic Python SDK + ANTHROPIC_API_KEY
         - "cli": subprocess the `claude` CLI binary (uses the OAuth
                  session under $HOME/.claude — for Claude Code
                  subscribers without API billing)

       `tools` gates the CLI's tool access (the SDK path has no tools
       regardless): None leaves the CLI's tools at their default; a list
       restricts them to exactly those names — an empty list `[]` means NO
       tools, which is how the (tree-free) prepare task keeps the model from
       running Bash/git to probe for a kernel tree. This is enforced by the
       CLI, independent of what the prompt says.

       `cwd` sets the CLI subprocess's working directory, so the agent's
       file tools (Read / Grep / Glob / Bash) are rooted there — the review
       task points it at the prepared worktree at the patch's base commit.
       Only meaningful on the CLI path with tools enabled; the SDK path has
       no filesystem reach and ignores it.

       Both backends return the same shape:
         {
           "text":  the assistant text (markdown fences stripped),
           "model": the model identifier actually used,
           "usage": {"input_tokens", "output_tokens", "duration_ms"},
           "trace": ordered [{"step": ...}] of the turn's assistant text,
                    tool_use, and tool_result steps — for hone-core to
                    persist + present (CLI streams the real steps; the SDK
                    path is a single assistant_text step).
         }

       Auth failures are translated to `CallClaudeAuthError` regardless
       of backend — the runner's main() prints the clean error and
       exits. Non-success outcomes update node.ai._LAST_ERROR so the
       health snapshot picks up the category."""
    resolved = model or cfg.anthropic_model or None
    if cfg.claude_backend == "cli":
        out = _call_claude_cli(cfg, system, user_text, model=resolved,
                               tools=tools, cwd=cwd)
    else:
        out = _call_claude_sdk(cfg, system, user_text, model=resolved,
                               max_tokens=max_tokens)
    # Single chokepoint for the daily/weekly token budget: every
    # successful turn's reported usage accrues to the ledger here,
    # whichever backend produced it (budget.record never raises).
    budget.record(cfg, out.get("usage"))
    return out


def _call_claude_sdk(cfg, system, user_text, *, model, max_tokens):
    """Anthropic SDK path — original behaviour. ANTHROPIC_API_KEY in
       cfg, normal HTTPS auth, structured usage in the response."""
    import anthropic                  # lazy: keeps node.tasks importable
                                       # without the SDK installed (tests).
    chosen = model or DEFAULT_MODEL
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    started = time.monotonic()
    try:
        with _heartbeat("claude SDK", started):
            resp = client.messages.create(
                model=chosen,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_text}])
    except (anthropic.AuthenticationError,
             anthropic.PermissionDeniedError) as exc:
        # Translate the SDK's specific auth-class error into a
        # domain-level exception the runner catches and reports as a
        # clean one-line operator message. The traceback is not useful
        # here — the cause is "ANTHROPIC_API_KEY is wrong" — so we
        # raise WITHOUT `from exc` to keep the operator log readable.
        _record_outcome("auth")
        status = getattr(exc, "status_code", "401/403")
        raise CallClaudeAuthError(
            f"Claude rejected the API key (HTTP {status}). "
            "Check ANTHROPIC_API_KEY in your .env / "
            "docker-compose env.") from None
    except anthropic.RateLimitError:
        _record_outcome("rate_limit")
        raise
    except (anthropic.APIConnectionError,
             anthropic.APITimeoutError):
        _record_outcome("connection")
        raise
    except anthropic.APIError:
        _record_outcome("other")
        raise
    _record_outcome(None)                  # successful call → clear
    duration_ms = int((time.monotonic() - started) * 1000)
    # The Messages API returns a list of content blocks; for a single
    # text turn there's exactly one block of type "text".
    text = "".join(block.text for block in resp.content
                    if getattr(block, "type", None) == "text")
    # An empty completion is a transient hiccup, not an empty success — raise
    # so the caller defers (see the CLI path for the rationale).
    if not text.strip():
        _record_outcome("other")
        raise CallClaudeError(
            "model returned an empty response (successful call, no text)",
            category="other", duration_ms=duration_ms, model=chosen)
    # Same cache-aware summation as the CLI path: SDK's input_tokens
    # is the uncached portion only when prompt-caching is in play.
    cache_read     = getattr(resp.usage, "cache_read_input_tokens",     None) or 0
    cache_creation = getattr(resp.usage, "cache_creation_input_tokens", None) or 0
    input_uncached = resp.usage.input_tokens or 0
    input_total    = input_uncached + cache_read + cache_creation
    return {"text":  _strip_fences(text),
             "model": chosen,
             "usage": {"input_tokens":  input_total,
                       "output_tokens": resp.usage.output_tokens,
                       "duration_ms":   duration_ms},
             # No tools / streaming on the SDK path (single completion) — the
             # trace is just the one assistant turn, for shape parity with
             # the CLI path so downstream can treat them uniformly.
             "trace": [{"step": "assistant_text", "text": text}]}


# Markers in `claude` CLI stderr that classify the failure for the
# health snapshot. Conservative regex-free substring matching: the CLI's
# message phrasing isn't a stable contract, so we match short
# common tokens. A miss just lands as "other", which is still better
# than the SDK-only behaviour of crashing the loop opaquely.
_CLI_STDERR_CATEGORY = (
    ("auth",        ("credentials", "not logged in", "unauthor",
                      "auth required", "please run `claude` to log in")),
    ("rate_limit",  ("rate limit", "too many requests", "429")),
    # TLS-trust failures (an intercepting/corporate proxy presents a cert
    # the CLI's bundled runtime doesn't trust) report "self-signed
    # certificate" / "unable to connect to api" — a connection problem,
    # not a model one.
    ("connection",  ("network", "connect", "timeout", "unreachable",
                      "self-signed", "self signed", "certificate",
                      "econnrefused", "enotfound", "etimedout")),
)


def _classify_cli_message(text):
    """Pick a node.ai._LAST_ERROR category from `claude` CLI failure text.
       Returns one of "auth" / "rate_limit" / "connection" / "other"."""
    lower = (text or "").lower()
    for category, markers in _CLI_STDERR_CATEGORY:
        if any(m in lower for m in markers):
            return category
    return "other"


def _cli_failure_text(stderr, result_event, trace):
    """Everything a failed CLI turn produced — stderr, the result event's
       text, and the assistant messages in the trace — joined for
       classification. A transport/API error (e.g. a TLS 'self-signed
       certificate' from an intercepting proxy) surfaces in the *stream*
       (an assistant 'API Error: …' message, echoed in the result), not on
       stderr, so classifying stderr alone misreads it as 'other'."""
    parts = [stderr or ""]
    if isinstance(result_event, dict):
        parts.append(str(result_event.get("result") or ""))
    for step in trace or []:
        if isinstance(step, dict) and step.get("step") == "assistant_text":
            parts.append(step.get("text") or "")
    return " ".join(parts)


# Default claude CLI invocation timeout in seconds — overridable per node
# via HONE_CLI_TIMEOUT (cfg.cli_timeout). Long enough to swallow a full
# Claude turn (large prompt + large response), short enough that a
# truly-wedged subprocess won't pin the node forever.
_CLI_TIMEOUT_SECONDS = 600

# `claude update` budget before a prompt: a no-op check is ~a second; an
# actual update downloads the ~230 MB binary. On timeout the turn just
# proceeds on the current version.
_CLI_UPDATE_TIMEOUT_SECONDS = 300


def _ensure_persistent_cli():
    """Seed the volume copy of the CLI from the image's pinned binary.

       The image bakes a SHA-verified `claude` at /usr/local/bin (a
       reproducible, pinned build); self-updates must NOT land in the
       container's overlay layer (copy-up churn, lost on recreate), so
       the Dockerfile sets HONE_CLAUDE_BIN_DIR=/data/bin — on the
       persistent volume, first on PATH — and this copies the pinned
       binary there once. `claude update` then maintains the volume
       copy: zero layer churn, and the updated CLI survives container
       recreates. No-op outside the container (env unset) or once the
       copy exists."""
    bin_dir = os.environ.get("HONE_CLAUDE_BIN_DIR")
    if not bin_dir:
        return
    dst = os.path.join(bin_dir, "claude")
    if os.path.exists(dst):
        return
    src = shutil.which("claude")
    if not src or os.path.realpath(src) == os.path.realpath(dst):
        return
    os.makedirs(bin_dir, exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)                 # atomic — a crash never leaves
    log.info("claude CLI: seeded %s from %s", dst, src)


# `claude --version` output, cached per process — health.collect runs
# every claim-loop tick and must stay fast; spawning a subprocess per
# tick is waste. _update_cli invalidates the cache after an update run,
# so the next health snapshot reports the version actually in use.
_CLI_VERSION_CACHE = {"value": None, "fresh": False}


def get_cli_version():
    """The `claude --version` string of the binary this node runs (e.g.
       "2.1.161 (Claude Code)"), or None when the CLI is absent (sdk
       backend) or unprobeable. Cached; refreshed after _update_cli
       runs. node/health.collect ships it in the health snapshot so
       hone-core's node detail page shows which CLI build the fleet is
       actually on — versions drift once per-prompt auto-update is in
       play."""
    if not _CLI_VERSION_CACHE["fresh"]:
        _CLI_VERSION_CACHE["value"] = _probe_cli_version()
        _CLI_VERSION_CACHE["fresh"] = True
    return _CLI_VERSION_CACHE["value"]


def _probe_cli_version():
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True,
                           text=True, timeout=30)
        if r.returncode == 0:
            return (r.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _update_cli():
    """Check for (and install) a CLI update before running a prompt.

       The image pins a SHA-verified version so builds are reproducible
       and never fail on a bad upstream release; this keeps the RUNNING
       fleet current without rebuilding. Best-effort by design: offline,
       a registry error, a timeout, or a missing binary all log and fall
       through — the prompt proceeds on the current version. Opt out
       with HONE_CLAUDE_AUTOUPDATE=0 (air-gapped deployments)."""
    if os.environ.get("HONE_CLAUDE_AUTOUPDATE", "1").lower() in (
            "0", "false", "no", "off"):
        return
    try:
        _ensure_persistent_cli()
        r = subprocess.run(["claude", "update"], capture_output=True,
                           text=True, timeout=_CLI_UPDATE_TIMEOUT_SECONDS)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        last = out.splitlines()[-1] if out else "ok"
        if r.returncode == 0:
            log.info("claude update: %s", last)
            # Re-probe the version on the next health tick — an update
            # may have just landed.
            _CLI_VERSION_CACHE["fresh"] = False
            # Deny rules parked as unknown are version-specific: a new
            # CLI may re-introduce a name, and the deny must come back
            # with it. Probe only when something is parked (the common
            # empty case costs nothing).
            if _CLI_UNKNOWN_DENY_RULES:
                new = _probe_cli_version()
                if new != _CLI_VERSION_CACHE.get("value"):
                    log.info("claude CLI version changed (%s -> %s) — "
                             "re-arming dropped deny rules %s",
                             _CLI_VERSION_CACHE.get("value"), new,
                             sorted(_CLI_UNKNOWN_DENY_RULES))
                    _CLI_UNKNOWN_DENY_RULES.clear()
                _CLI_VERSION_CACHE["value"] = new
                _CLI_VERSION_CACHE["fresh"] = True
        else:
            log.warning("claude update failed (rc=%d): %s — proceeding "
                        "on current version", r.returncode, last[:300])
    except FileNotFoundError:
        pass         # no CLI at all — the prompt path raises the real error
    except subprocess.TimeoutExpired:
        log.warning("claude update timed out after %ds — proceeding on "
                    "current version", _CLI_UPDATE_TIMEOUT_SECONDS)
    except OSError:
        log.exception("claude update failed — proceeding on current version")


def _parse_event(line):
    """One stream-json line → a dict, or None for blank / non-JSON lines
       (the CLI occasionally interleaves a stray non-event line; ignore it
       rather than abort the turn)."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


# Input keys, in priority order, that name what a tool acted on — used to
# put the target (file / pattern / command) on the INFO tool_use line.
_TOOL_INPUT_KEYS = ("file_path", "path", "pattern", "command", "query", "url")


def _tool_summary(tool_input):
    """A short representative value from a tool_use's input for the log
       line — the file path / pattern / command, whichever is present."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _TOOL_INPUT_KEYS:
        val = tool_input.get(key)
        if val:
            return str(val)[:80]
    return ""


def _trace_assistant(ev, trace):
    """Log + record an `assistant` event's text and tool_use blocks. The
       INFO lines carry a short text snippet and the tool's target so a
       console viewer sees WHAT Claude is doing; the full text + tool input
       ride in `trace` (and the full result at DEBUG). Each block becomes
       one ordered `trace` step (assistant_text / tool_use) for hone-core to
       persist + show."""
    for block in (ev.get("message") or {}).get("content", []):
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            snippet = " ".join(text.split())     # collapse newlines/runs
            shown = snippet[:80] + ("…" if len(snippet) > 80 else "")
            log.info('claude CLI ‹ assistant: "%s" (%d chars)',
                     shown, len(text))
            trace.append({"step": "assistant_text", "text": text})
        elif btype == "tool_use":
            name = block.get("name")
            summary = _tool_summary(block.get("input"))
            log.info("claude CLI ‹ assistant: tool_use %s%s",
                     name, f" {summary}" if summary else "")
            trace.append({"step": "tool_use", "id": block.get("id"),
                          "name": name, "input": block.get("input")})


def _trace_tool_results(ev, trace):
    """Record a `user` event's tool_result blocks — size only, not the full
       content (a Read of a big file would bloat the record)."""
    for block in (ev.get("message") or {}).get("content", []):
        if block.get("type") == "tool_result":
            content = block.get("content")
            trace.append({"step": "tool_result",
                          "id": block.get("tool_use_id"),
                          "chars": len(content) if isinstance(content, str)
                                   else None})


# Tools the CLI must NEVER run on a constrained (prepare/review) turn. The
# `--allowedTools` allowlist only governs which tools *auto-approve*, NOT which
# may run at all, so a "read-only" review could still shell out (Bash) or fan
# out into subagents (Task/Agent) — both observed in the wild, and a subagent
# finalization crash is what wedged the CLI on at least one review (the run
# exited non-zero with no result event). `--disallowedTools` is the hard
# exclusion: it overrides the prompt and the allowlist both. We pass it on
# every turn that declares an explicit tool set (`tools is not None`),
# covering prepare (tools=[]) and review (tools=Read/Grep/Glob) alike; a
# caller that leaves tools=None wants the CLI default and opts out. Task and
# Agent are both listed because the subagent tool surfaces under either name
# across CLI versions.
_CLI_BLOCKED_TOOLS = ["Task", "Agent", "Bash", "BashOutput", "KillShell",
                      "Write", "Edit", "MultiEdit", "NotebookEdit",
                      "WebFetch", "WebSearch"]

# Deny-rule names the RUNNING CLI has rejected as unknown. The list above
# deliberately over-names across CLI versions (Task/Agent, MultiEdit) so
# every version's spelling of a tool is denied — but newer CLIs hard-FAIL
# on a permission rule naming a tool they don't have ('Permission deny
# rule "MultiEdit" matches no known tool'), turning the safety margin
# into an outage (auto-update flipped this under a running fleet,
# 2026-06-12). Denying a tool the CLI doesn't ship is semantically a
# no-op, so on that error the name is parked here and the call retried
# without it; the failed attempt exits at flag validation, before any
# model turn. ALLOW-rule rejections are NOT self-healed: silently
# dropping an allowed tool would degrade the task (a review without
# Read), so those stay loud. Cleared when the CLI version changes
# (_update_cli) — a later version may re-introduce a name.
_CLI_UNKNOWN_DENY_RULES = set()

_UNKNOWN_DENY_RULE_RE = re.compile(
    r'[Pp]ermission deny rule "([^"]+)" matches no known tool')


def _call_claude_cli(cfg, system, user_text, *, model, tools=None, cwd=None):
    """`claude` CLI subprocess path — uses the OAuth session in
       $HOME/.claude rather than ANTHROPIC_API_KEY. Pipes user_text through
       stdin (avoids the ARG_MAX cap on multi-thousand-line prompts); the
       system prompt goes through `--system-prompt`.

       `tools` gates tool access: None leaves the default; a list passes
       `--allowedTools <names>`, and an empty list passes an empty allowlist
       — no tools. prepare uses `[]` so the model can't run Bash/git to hunt
       for a kernel tree (it has none; Tier-0 owns the tree-dependent
       fields), regardless of what the prompt says. Whenever `tools` is given
       (not None) we ALSO pass `--disallowedTools _CLI_BLOCKED_TOOLS`: the
       allowlist only governs auto-approval, so the hard denylist is what
       actually keeps a "read-only" turn from shelling out or spawning
       subagents.

       Streams `--output-format stream-json --verbose`: the CLI emits one
       JSON event per line as the turn unfolds (session init, assistant
       text, tool_use, tool_result, then a final `result`). We read them as
       they arrive — logging each step so a console viewer can follow what
       Claude is doing, and building a `trace` of assistant messages + tool
       uses for hone-core to persist and present. The final `result` event
       carries the answer text + token usage.

       A watchdog thread emits a 'still working' heartbeat between events
       (covering a stall mid-tool-run or mid-think) AND enforces
       _CLI_TIMEOUT_SECONDS — Popen has no built-in timeout and the stdout
       read blocks until the CLI closes the pipe. stderr is drained on its
       own thread (a chatty CLI mustn't deadlock on a full pipe) and is the
       auth-state signal: a non-zero exit with a message like 'Please run
       `claude` to log in', classified into the SDK path's category
       vocabulary."""
    # Update check before every prompt (HONE_CLAUDE_AUTOUPDATE=0 opts
    # out). The image pins a SHA-verified CLI; this keeps the running
    # fleet current — see _update_cli / _ensure_persistent_cli.
    _update_cli()
    chosen = model or DEFAULT_MODEL
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--system-prompt", system]
    if tools is not None:
        cmd += ["--allowedTools", ",".join(tools)]   # [] → "" → no tools
        # Hard exclusion — the allowlist alone does not stop unlisted tools
        # (Bash, Task/Agent subagents) from running; this does. Names the
        # running CLI rejected as unknown are skipped (see
        # _CLI_UNKNOWN_DENY_RULES).
        cmd += ["--disallowedTools",
                ",".join(t for t in _CLI_BLOCKED_TOOLS
                         if t not in _CLI_UNKNOWN_DENY_RULES)]
    if chosen:
        cmd += ["--model", chosen]
    log.info("claude CLI → model=%s system=%d user=%d chars tools=%s cwd=%s "
             "(stream)", chosen, len(system), len(user_text),
              "default" if tools is None else (tools or "none"), cwd or ".")
    log.debug("claude CLI → system: %s", system)
    log.debug("claude CLI → user:   %s", user_text)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=cwd)
    except FileNotFoundError:
        # `claude` binary isn't in PATH. This is configuration-fatal —
        # same operator-feedback shape as a wrong API key.
        _record_outcome("auth")
        raise CallClaudeAuthError(
            "`claude` CLI not found in PATH. "
            "Either install @anthropic-ai/claude-code in the container "
            "image, or switch HONE_CLAUDE_BACKEND back to sdk."
        ) from None

    done = threading.Event()
    timed_out = threading.Event()
    # Per-call timeout: HONE_CLI_TIMEOUT (cfg.cli_timeout) overrides the
    # module default. getattr keeps test cfgs that omit the field working.
    timeout = getattr(cfg, "cli_timeout", None) or _CLI_TIMEOUT_SECONDS

    def watchdog():
        while not done.wait(_HEARTBEAT_SECONDS):
            if time.monotonic() - started >= timeout:
                timed_out.set()
                proc.kill()
                return
            log.info("claude CLI … still working (%.0fs elapsed)",
                     time.monotonic() - started)

    stderr_chunks = []

    def drain_stderr():
        for chunk in proc.stderr:
            stderr_chunks.append(chunk)

    threading.Thread(target=watchdog, name="claude-cli-watchdog",
                     daemon=True).start()
    threading.Thread(target=drain_stderr, name="claude-cli-stderr",
                     daemon=True).start()

    trace, result_event, model_used = [], None, chosen
    try:
        try:
            proc.stdin.write(user_text)
            proc.stdin.close()
        except BrokenPipeError:
            pass                            # process already exited; rc/stderr
                                            # below explain why
        for line in proc.stdout:
            ev = _parse_event(line)
            if ev is None:
                continue
            etype = ev.get("type")
            if etype == "system" and ev.get("subtype") == "init":
                model_used = ev.get("model") or model_used
                log.info("claude CLI ‹ session %s — model %s, %d tool(s)",
                          (ev.get("session_id") or "?")[:8],
                          ev.get("model"), len(ev.get("tools") or []))
            elif etype == "assistant":
                model_used = (ev.get("message") or {}).get("model") or model_used
                _trace_assistant(ev, trace)
            elif etype == "user":
                _trace_tool_results(ev, trace)
            elif etype == "result":
                result_event = ev
        rc = proc.wait()
    finally:
        done.set()                          # stop the watchdog
    duration_ms = int((time.monotonic() - started) * 1000)

    if timed_out.is_set():
        _record_outcome("connection")
        raise CallClaudeError(
            f"`claude` CLI timed out after {timeout}s "
            "— the subprocess was wedged.",
            category="connection", trace=trace,
            duration_ms=duration_ms, model=model_used) from None
    stderr = "".join(stderr_chunks)
    if rc != 0:
        failure_text = _cli_failure_text(stderr, result_event, trace)
        # Self-heal a deny rule the CLI rejects as unknown: park the name
        # and retry — the failed attempt died at flag validation (no model
        # turn), and not-denying a tool the CLI doesn't have is a no-op.
        m = _UNKNOWN_DENY_RULE_RE.search(failure_text)
        if (tools is not None and m
                and m.group(1) in _CLI_BLOCKED_TOOLS
                and m.group(1) not in _CLI_UNKNOWN_DENY_RULES):
            _CLI_UNKNOWN_DENY_RULES.add(m.group(1))
            log.warning("claude CLI rejects deny rule %r as unknown — "
                        "dropping it for this CLI version and retrying",
                        m.group(1))
            return _call_claude_cli(cfg, system, user_text, model=model,
                                    tools=tools, cwd=cwd)
        category = _classify_cli_message(failure_text)
        log.warning("claude CLI ← exit=%d category=%s in %.1fs: %s",
                     rc, category, duration_ms / 1000,
                     stderr.strip()[:200])
        _record_outcome(category)
        if category == "auth":
            # Auth is the one configuration-fatal CLI failure — no record
            # to submit, the operator must re-login. Stays a hard exit.
            raise CallClaudeAuthError(
                "`claude` CLI auth state is stale or absent — "
                "run `claude` on the host to re-login, then ensure "
                "the container's $HOME/.claude mount is read-write."
            ) from None
        raise CallClaudeError(
            f"`claude` CLI failed ({rc}): {stderr.strip()[:500]}",
            category=category, returncode=rc, stderr=stderr,
            trace=trace, duration_ms=duration_ms, model=model_used)
    # A clean exit can still be a failure: the stream may carry no result
    # event, or a result that's an API/transport error (the CLI reports a
    # TLS 'self-signed certificate' as an assistant 'API Error: …' message
    # and a non-success result, NOT a non-zero exit). Classify both off the
    # stream text so the operator's health page sees the real cause.
    if result_event is None:
        category = _classify_cli_message(
            _cli_failure_text(stderr, None, trace))
        _record_outcome(category)
        raise CallClaudeError(
            "`claude` CLI stream ended without a result event",
            category=category, returncode=rc, stderr=stderr,
            trace=trace, duration_ms=duration_ms, model=model_used)
    if result_event.get("is_error") or result_event.get("subtype") != "success":
        category = _classify_cli_message(
            _cli_failure_text(stderr, result_event, trace))
        _record_outcome(category)
        raise CallClaudeError(
            f"`claude` CLI returned an error result: {result_event!r}",
            category=category, returncode=rc, stderr=stderr,
            trace=trace, duration_ms=duration_ms, model=model_used)

    result_text = _strip_fences(result_event.get("result", ""))
    # Defensive: some CLI versions report an API/transport failure as a
    # *successful* result whose text is an "API Error: …" message rather
    # than setting is_error. prepare's JSON parse would reject that as
    # uncharacterisable later, but mislabel the cause — so detect the CLI's
    # error prefix here and raise the right category (connection for a
    # self-signed cert, rate_limit for a 429, …) instead.
    if result_text.lstrip().lower().startswith("api error"):
        category = _classify_cli_message(
            _cli_failure_text(stderr, result_event, trace))
        _record_outcome(category)
        raise CallClaudeError(
            f"`claude` CLI returned an API error: {result_text.strip()[:300]}",
            category=category, returncode=rc, stderr=stderr,
            trace=trace, duration_ms=duration_ms, model=model_used)

    # A successful result with no text is a transient empty completion (the
    # model billed a few tokens but produced nothing parseable — e.g. an empty
    # ```json``` fence). Treat it as a deferrable error, not an empty success:
    # otherwise the caller parses "" into a confusing failure and, for prepare,
    # can't tell it apart from a real refusal.
    if not result_text.strip():
        _record_outcome("other")
        raise CallClaudeError(
            "`claude` returned an empty response — a successful result with no "
            "text (transient; defer and retry)",
            category="other", returncode=rc, stderr=stderr,
            trace=trace, duration_ms=duration_ms, model=model_used)
    _record_outcome(None)
    usage = result_event.get("usage") or {}
    # The CLI's `input_tokens` is the *non-cached* portion only; cached
    # portions are reported separately. Sum all three so `input_tokens`
    # downstream reflects what Claude actually processed (otherwise a
    # 30k-token prompt served mostly from cache looks like a ~10-token
    # request in the work-item record). Keep the cache split exposed
    # too — useful for the per-call cost breakdown.
    cache_read     = usage.get("cache_read_input_tokens")     or 0
    cache_creation = usage.get("cache_creation_input_tokens") or 0
    input_uncached = usage.get("input_tokens")                or 0
    input_total    = input_uncached + cache_read + cache_creation
    log.info("claude CLI ← in=%d (uncached=%d cache_read=%d cache_new=%d) "
             "out=%s tokens, %.1fs, %d trace step(s)",
              input_total, input_uncached, cache_read, cache_creation,
              usage.get("output_tokens"), duration_ms / 1000, len(trace))
    log.debug("claude CLI ← result: %s", result_text)
    return {"text":  result_text,
             "model": result_event.get("model") or model_used,
             "usage": {"input_tokens":  input_total,
                       "output_tokens": usage.get("output_tokens"),
                       "duration_ms":   duration_ms},
             "trace": trace}


def parse_json_response(text):
    """Parse Claude's response text as a JSON object. Returns the dict
       on success or raises ValueError with a short prose reason on
       failure — the caller wraps that into an uncharacterisable /
       failed completion record per the operation's failure-path
       contract.

       Tolerant of prose around the JSON: Claude is asked for raw JSON
       only, but habitually narrates — "Based on my discovery, …"
       preambles and `Now analyzing…` postambles are common and the
       methodology's "no prose" directive alone doesn't suppress them.
       The parser first strips an enclosing markdown fence (the
       cleanest case), then scans for the first structural JSON
       character and uses json.raw_decode to pluck out the first
       complete object, ignoring any trailing content. Equivalent to
       "find the JSON in this otherwise-text response"."""
    cleaned = _strip_fences(text)
    # First-pass: maybe the cleaned text IS pure JSON (the happy case
    # after fence stripping).
    direct_err = None
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as e:
        direct_err = e
    else:
        if isinstance(obj, dict):
            return obj
        raise ValueError(
            f"response JSON is a {type(obj).__name__}, expected an object")
    # Second-pass: scan for the first `{` that opens a parseable JSON
    # object somewhere in the text. raw_decode consumes one JSON value
    # from a string and reports where it stopped, ignoring trailing
    # content — exactly what we want when Claude wraps the object in
    # prose. Try every `{` position in order so a stray `{` in an
    # earlier sentence doesn't trap us.
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(cleaned[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(
        f"response contains no parseable JSON object: "
        f"{direct_err.msg} at line {direct_err.lineno} "
        f"col {direct_err.colno}") from direct_err
