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
import re
import subprocess
import threading
import time

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
                max_tokens=DEFAULT_MAX_TOKENS):
    """Call Claude and return the response, dispatching on
       cfg.claude_backend:

         - "sdk": the Anthropic Python SDK + ANTHROPIC_API_KEY
         - "cli": subprocess the `claude` CLI binary (uses the OAuth
                  session under $HOME/.claude — for Claude Code
                  subscribers without API billing)

       Both backends return the same shape:
         {
           "text":  the assistant text (markdown fences stripped),
           "model": the model identifier actually used,
           "usage": {"input_tokens", "output_tokens", "duration_ms"},
         }

       Auth failures are translated to `CallClaudeAuthError` regardless
       of backend — the runner's main() prints the clean error and
       exits. Non-success outcomes update node.ai._LAST_ERROR so the
       health snapshot picks up the category."""
    resolved = model or cfg.anthropic_model or None
    if cfg.claude_backend == "cli":
        return _call_claude_cli(cfg, system, user_text, model=resolved)
    return _call_claude_sdk(cfg, system, user_text, model=resolved,
                             max_tokens=max_tokens)


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
                       "duration_ms":   duration_ms}}


# Markers in `claude` CLI stderr that classify the failure for the
# health snapshot. Conservative regex-free substring matching: the CLI's
# message phrasing isn't a stable contract, so we match short
# common tokens. A miss just lands as "other", which is still better
# than the SDK-only behaviour of crashing the loop opaquely.
_CLI_STDERR_CATEGORY = (
    ("auth",        ("credentials", "not logged in", "unauthor",
                      "auth required", "please run `claude` to log in")),
    ("rate_limit",  ("rate limit", "too many requests", "429")),
    ("connection",  ("network", "connect", "timeout", "unreachable")),
)


def _classify_cli_stderr(stderr):
    """Pick a node.ai._LAST_ERROR category from a `claude` CLI stderr
       message. Returns one of "auth" / "rate_limit" / "connection"
       / "other"."""
    lower = (stderr or "").lower()
    for category, markers in _CLI_STDERR_CATEGORY:
        if any(m in lower for m in markers):
            return category
    return "other"


# claude CLI invocation timeout in seconds. Long enough to swallow a
# full Claude turn (large prompt + large response), short enough that a
# truly-wedged subprocess won't pin the node forever.
_CLI_TIMEOUT_SECONDS = 600


def _call_claude_cli(cfg, system, user_text, *, model):
    """`claude` CLI subprocess path — uses the OAuth session in
       $HOME/.claude rather than ANTHROPIC_API_KEY. Pipes user_text
       through stdin (avoids the ARG_MAX cap on multi-thousand-line
       prompts); system prompt goes through `--system-prompt`;
       `--output-format json` gives us a structured envelope to parse.

       The CLI's stderr is the only auth-state signal we have — the
       process exits non-zero with a message like "Please run `claude`
       to log in." We classify that into the same category vocabulary
       the SDK path uses so the health snapshot stays uniform across
       backends."""
    chosen = model or DEFAULT_MODEL
    cmd = ["claude", "-p", "--output-format", "json",
           "--system-prompt", system]
    if chosen:
        cmd += ["--model", chosen]
    # INFO-level send/receive lines mirror what httpx auto-logs for the
    # SDK path — without them the CLI backend would be invisible in
    # `docker logs`. Lengths only; the full prompts ride at DEBUG so
    # operators can opt in to verbose tracing without flooding the
    # default log.
    log.info("claude CLI → model=%s system=%d user=%d chars",
              chosen, len(system), len(user_text))
    log.debug("claude CLI → system: %s", system)
    log.debug("claude CLI → user:   %s", user_text)
    started = time.monotonic()
    try:
        with _heartbeat("claude CLI", started):
            r = subprocess.run(cmd, input=user_text,
                               capture_output=True, text=True,
                               timeout=_CLI_TIMEOUT_SECONDS)
    except FileNotFoundError:
        # `claude` binary isn't in PATH. This is configuration-fatal —
        # same operator-feedback shape as a wrong API key.
        _record_outcome("auth")
        raise CallClaudeAuthError(
            "`claude` CLI not found in PATH. "
            "Either install @anthropic-ai/claude-code in the container "
            "image, or switch HONE_CLAUDE_BACKEND back to sdk."
        ) from None
    except subprocess.TimeoutExpired:
        _record_outcome("connection")
        raise RuntimeError(
            f"`claude` CLI timed out after {_CLI_TIMEOUT_SECONDS}s "
            "— the subprocess was wedged.") from None
    duration_ms = int((time.monotonic() - started) * 1000)

    if r.returncode != 0:
        category = _classify_cli_stderr(r.stderr)
        log.warning("claude CLI ← exit=%d category=%s in %.1fs: %s",
                     r.returncode, category, duration_ms / 1000,
                     (r.stderr or "").strip()[:200])
        _record_outcome(category)
        if category == "auth":
            raise CallClaudeAuthError(
                "`claude` CLI auth state is stale or absent — "
                "run `claude` on the host to re-login, then ensure "
                "the container's $HOME/.claude mount is read-write."
            ) from None
        raise RuntimeError(
            f"`claude` CLI failed ({r.returncode}): "
            f"{(r.stderr or '').strip()[:500]}")

    try:
        env = json.loads(r.stdout)
    except (ValueError, TypeError):
        _record_outcome("other")
        raise RuntimeError(
            f"`claude` CLI returned unparseable JSON envelope: "
            f"{r.stdout[:500]!r}") from None
    if env.get("is_error") or env.get("type") != "result":
        _record_outcome("other")
        raise RuntimeError(
            f"`claude` CLI returned an error envelope: {env!r}")

    _record_outcome(None)
    usage = env.get("usage") or {}
    result_text = _strip_fences(env.get("result", ""))
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
             "out=%s tokens, %.1fs",
              input_total, input_uncached, cache_read, cache_creation,
              usage.get("output_tokens"), duration_ms / 1000)
    log.debug("claude CLI ← result: %s", result_text)
    return {"text":  result_text,
             "model": env.get("model") or chosen,
             "usage": {"input_tokens":  input_total,
                       "output_tokens": usage.get("output_tokens"),
                       "duration_ms":   duration_ms}}


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
