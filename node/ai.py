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
import json
import logging
import re
import time

log = logging.getLogger("hone.node.ai")


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

# Default model. Overridable per call (and via ANTHROPIC_MODEL on the
# config — see node/config.py if you need it environment-driven).
DEFAULT_MODEL = "claude-opus-4-7"

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


def call_claude(cfg, system, user_text, *, model=None,
                max_tokens=DEFAULT_MAX_TOKENS):
    """Call Claude's Messages API and return the response.

       Returns a dict:
         {
           "text":         the assistant message text (fences stripped),
           "model":        the model identifier actually used,
           "usage":        {"input_tokens", "output_tokens", "duration_ms"}
                          — the three fields a hone completion record's
                          `usage` block needs,
         }

       Raises whatever the SDK raises; the runner's transient-failure
       backoff (see node/runner.py) wraps the surrounding submit/claim
       path but NOT this call — an AI failure should currently surface
       as a task failure rather than retrying a multi-second-cost
       completion silently. (A future refinement may add per-task
       retry for ratelimit-classified Anthropic errors specifically.)"""
    import anthropic                  # lazy: keeps node.tasks importable
                                       # without the SDK installed (tests).
    chosen = model or DEFAULT_MODEL
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    started = time.monotonic()
    try:
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
        status = getattr(exc, "status_code", "401/403")
        raise CallClaudeAuthError(
            f"Claude rejected the API key (HTTP {status}). "
            "Check ANTHROPIC_API_KEY in your .env / "
            "docker-compose env.") from None
    duration_ms = int((time.monotonic() - started) * 1000)
    # The Messages API returns a list of content blocks; for a single
    # text turn there's exactly one block of type "text".
    text = "".join(block.text for block in resp.content
                    if getattr(block, "type", None) == "text")
    return {"text":  _strip_fences(text),
             "model": chosen,
             "usage": {"input_tokens":  resp.usage.input_tokens,
                       "output_tokens": resp.usage.output_tokens,
                       "duration_ms":   duration_ms}}


def parse_json_response(text):
    """Parse Claude's response text as a JSON object. Returns the dict on
       success or raises ValueError with a short prose reason on failure
       — the caller wraps that into an uncharacterisable / failed
       completion record per the operation's failure-path contract."""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"response is not valid JSON: {e.msg} at line {e.lineno} "
            f"col {e.colno}") from e
    if not isinstance(obj, dict):
        raise ValueError(
            f"response JSON is a {type(obj).__name__}, expected an object")
    return obj
