"""Error fingerprinting and aggregation.

The point of this module: "the same failure happened again" is a question about
*shapes*, not strings. Two errors that differ only by a request id, a row count
or a temp path are the same failure. Normalizing those away and hashing what
remains turns a flat list of error text into countable patterns — which is what
makes "this recurs" a fact the plugin can assert instead of a guess it delegates
to the model.

Pure functions only: no DB, no config, no LLM. Everything here is unit-testable
without a Hermes host.
"""

import hashlib
import math
import re
from typing import Any, Dict, Iterable, List, Optional

try:
    from .sanitization import scrub_text
except ImportError:
    from sanitization import scrub_text  # type: ignore

# Path normalization has to collapse volatile detail (``/users/8821`` and
# ``/users/9134`` are one failure) *without* merging errors that only look alike
# after normalization. Three boundaries carry that second half:
#   * a *forward-slash* path must start at a word boundary, so ``read/write error``
#     and ``read/execute error`` stay two failures and ``50/50 attempts`` stays a
#     pair of numbers. A backslash or a ``C:`` is not prose, so that boundary is
#     not imposed there — doing so measurably stopped real Windows paths from
#     collapsing (``…\dir\file``, ``…\dir\sub``).
#   * interior spaces are accepted only under a root that cannot be anything else —
#     a drive letter or a UNC prefix — so prose between two paths survives:
#     ``no such file /tmp/a and permission denied /tmp/b`` normalizes to two PATH
#     tokens with ``and permission denied`` intact, and a lone backslash (which in
#     tool output is often a literal escape, ``step1\nretry aborted``) cannot span
#     the words that separate two failures.
#   * an unrooted path (``src/main.py``) is only recognized when its last segment
#     carries an extension — that is what separates a real relative path from two
#     prose words around a slash.
_SEGMENT = r"[\w.()\-]+"
_SPACED_SEGMENT = rf"{_SEGMENT}(?: {_SEGMENT})*"
# A drive letter or a UNC prefix is never prose, so segments under it may carry
# interior spaces (``C:\Program Files\x``).
_ROOTED_WINDOWS_PATH = rf"(?:[A-Za-z]:[\\/]|\\\\)(?:{_SPACED_SEGMENT}[\\/])*{_SEGMENT}"
# A lone backslash still starts a path — that is how ``…\dir\file`` collapses —
# but it takes no spaces and needs a second separator. One backslash followed by a
# single token is far more often a literal escape in tool output than a path, and
# collapsing it would erase the one word that separates two failures:
# ``step1\nretry failed`` and ``step1\nreload failed`` must stay apart. A
# single-separator relative path keeps collapsing through ``_RELATIVE_PATH``,
# which requires an extension (``src\main.py``).
_BACKSLASH_PATH = rf"\\(?:{_SEGMENT}[\\/])+{_SEGMENT}"
# Forward slashes appear in ordinary prose, so a POSIX path takes no spaces.
_POSIX_PATH = rf"(?<!\w)/(?:{_SEGMENT}/)*{_SEGMENT}"
# One flat, bounded separator loop. The trailing ``.ext`` requirement makes every
# separator run re-split when it is absent, so an unbounded loop turns a long run
# of extensionless ``a/b/c…`` text into quadratic work (105 ms for one 4 KB row,
# and ``/refine audit`` normalizes every row twice). Eight separators cover real
# paths; a deeper one still normalizes, just from a later segment on.
_RELATIVE_PATH = rf"(?<!\w){_SEGMENT}(?:[\\/]{_SEGMENT}){{1,8}}\.[A-Za-z0-9]{{1,8}}"
_PATH = re.compile(
    rf"(?:{_ROOTED_WINDOWS_PATH}|{_BACKSLASH_PATH}|{_POSIX_PATH}|{_RELATIVE_PATH})"
)

# A Windows-style CLI switch ("/help", "/force") is syntactically identical to
# a single-segment POSIX path -- one slash, one word -- so the path rule below
# swallows it as PATH, collapsing two genuinely different "unknown option"
# errors into one. Restricted to "option"/"flag"/"switch": those precede a
# flag NAME and nothing else, unlike "argument"/"parameter", which as often
# introduce a VALUE (``argument /tmp/x.txt was invalid``) that is exactly the
# volatile detail two failures should still collapse on.
_CLI_FLAG = re.compile(r"(?i)\b(option|flag|switch)\s+/(\S+)")

# An HTTP status code is the semantic core of the failure -- 404, 401 and 500
# are different errors, not the same one with a different row count -- so it
# must survive the blanket integer rule below rather than being erased into
# "N" like a duration or a request id.
#
# Split by how much context each shape needs. ``HTTP/1.1 404``, ``HTTP Error
# 404`` and requests.py's ``404 Client Error``/``500 Server Error`` are
# unambiguous by themselves -- nothing else reads that way -- so they always
# apply. A bare ``returned``/``status`` lead-in does not: tried ungated first,
# and it flagged "query returned 250 items" as an HTTP error over a row count.
# That one only applies once the message has separately proven itself
# HTTP-flavored, by containing "http"/"https" somewhere at all.
_HTTP_STATUS_PRESENT = re.compile(r"(?i)\bhttps?\b")
_HTTP_STATUS_ANCHORED = re.compile(
    r"(?i)\bhttps?/\d(?:\.\d+)?\s+([1-5]\d{2})\b"
    r"|\bhttps?\b[^\d]{0,20}\b([1-5]\d{2})\b"
    r"|\b([1-5]\d{2})\s+(?:client|server)\s+error\b"
)
_HTTP_STATUS_CONTEXTUAL = re.compile(
    r"(?i)\b(?:returned|status|responded\s+with|response)\b\s*(?:code\s*)?[:=]?\s*([1-5]\d{2})\b"
)


def _mark_http_status(match: "re.Match[str]") -> str:
    code = next(group for group in match.groups() if group)
    return match.group(0).replace(code, f"httpstatus{code}")


def _preserve_http_status(text: str) -> str:
    """Shield a real HTTP status code before the path/digit rules can erase it.

    Must run before ``_PATH`` and before the URL rule: ``HTTP/1.1 404`` is
    otherwise read as a relative path with a ".1" extension (swallowing the
    "http" anchor along with the code), and ``https://…`` is otherwise
    collapsed to the literal token ``URL`` before the presence check below
    ever sees it.
    """
    text = _HTTP_STATUS_ANCHORED.sub(_mark_http_status, text)
    if _HTTP_STATUS_PRESENT.search(text):
        text = _HTTP_STATUS_CONTEXTUAL.sub(_mark_http_status, text)
    return text


# Order matters: timestamps and paths must be replaced before bare integers,
# otherwise the digit rule eats the parts that make them recognizable.
_NORMALIZERS = [
    # ISO-ish timestamps and clock times
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "T"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "T"),
    # Single-quoted literals: keep the contents for the same reason as above
    # (``KeyError: 'user_id'`` is identified by the name, not by the quotes).
    (re.compile(r"'([^']*)'"), r"\1"),
    # CLI switches, before the path rule can mistake one for a single-segment
    # POSIX path — see _CLI_FLAG above.
    (_CLI_FLAG, lambda m: f"{m.group(1)} cliflag_{m.group(2)}"),
    # URLs before paths (a URL contains slashes)
    (re.compile(r"https?://\S+"), "URL"),
    # Filesystem paths, POSIX and Windows — see _PATH below for the boundaries
    # that keep this rule from merging genuinely different errors.
    (_PATH, "PATH"),
    # UUIDs, then any long hex run (ids, hashes, object addresses)
    (re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"), "X"),
    (re.compile(r"\b(?:0x)?[0-9a-fA-F]{7,}\b"), "X"),
    # Durations and sizes: a timeout after 10s and after 15s are one failure.
    (re.compile(r"(?i)\b\d+(?:\.\d+)?\s*(?:ms|s|m|h|kb|mb|gb)\b"), "N"),
    # Whatever integers survive
    (re.compile(r"\b\d+\b"), "N"),
]

_TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "', "  at ")
_TRACEBACK_WRAPPER_LINE = re.compile(r"(?i)^(?:g?make|ninja):")
_TRACEBACK_CHAIN_LINE = re.compile(
    r"(?i)^(?:during handling of the above exception|the above exception was the direct cause)"
)
_TRACEBACK_RUNNER_FOOTER_LINE = re.compile(
    r"(?i)^(?:process|command) exited with code \d+|^exit(?:ed)? code:? \d+"
)


def _is_python_exception_line(line: str) -> bool:
    """Recognize a printed exception type without assuming capitalization."""
    stripped = line.strip()
    if not stripped or _TRACEBACK_WRAPPER_LINE.match(stripped):
        return False
    type_name = stripped.partition(":")[0]
    return bool(type_name) and all(
        component.isidentifier() for component in type_name.split(".")
    )


# A complete double-quoted token, honouring backslash escapes.
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def _strip_quotes(text: str) -> str:
    """Drop quote characters but keep what is inside them.

    Blanking quoted strings wholesale looks tempting — they usually hold the
    volatile part — but the error *message* is also a quoted value, and it is
    the single most identifying thing about a failure. Blanking it makes
    "rate limited" and "permission denied" the same pattern, which defeats the
    entire purpose. The volatile pieces inside (ids, paths, timestamps) are
    already handled by the rules below, so keeping the words costs nothing.

    Tokenizing matters: a naive ``"[^"]*"`` regex matches from the closing quote
    of one JSON key to the opening quote of the next, mangling the boundary.
    """
    return _DOUBLE_QUOTED.sub(lambda match: match.group(0)[1:-1], text)


def normalize_error(content: str) -> str:
    """Reduce an error message to its invariant shape.

    ``HTTP 429 for /users/8821`` and ``HTTP 429 for /users/9134`` both normalize
    to ``http N for PATH`` — one pattern, not two.
    """
    if not content:
        return ""

    text = content.strip()

    # Hermes appends this diagnostic to repeated tool failures. It is host
    # instrumentation, not part of the tool's error shape, so remove only a
    # true terminal suffix; tool-controlled text before a later error detail
    # must not be able to hide that distinguishing detail.
    text = re.sub(
        r"\s*\[Tool loop warning:\s*repeated_exact_failure_warning;"
        r"[^\]]*\]\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()

    # For a traceback, the only stable part is the terminal exception line; the
    # frames above it are noise that changes with every refactor. Chained Python
    # exceptions contain more than one traceback, so inspect only the last one.
    # The unambiguous header is required because `File "` and `  at ` also occur
    # in normal CLI output.
    lines = text.splitlines()
    traceback_headers = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"Traceback \(most recent call last\):\s*", line)
    ]
    if traceback_headers:
        exception_line = None
        for position in range(len(traceback_headers) - 1, -1, -1):
            start = traceback_headers[position] + 1
            end = (
                traceback_headers[position + 1]
                if position + 1 < len(traceback_headers)
                else len(lines)
            )
            block = lines[start:end]
            # A traceback exception is its block's terminal non-wrapper line.
            # Permitting arbitrary indented candidates makes typed source
            # statements such as ``ConnectionError: annotation`` look like an
            # exception and hides the actual terminal failure.
            terminal = ""
            for line in reversed(block):
                if not line.strip():
                    continue
                if (
                    _TRACEBACK_WRAPPER_LINE.match(line.strip())
                    or _TRACEBACK_CHAIN_LINE.match(line.strip())
                    or _TRACEBACK_RUNNER_FOOTER_LINE.match(line.strip())
                ):
                    continue
                terminal = line
                break
            exception_line = (
                terminal.strip() if terminal and _is_python_exception_line(terminal) else None
            )
            if exception_line:
                break
        if exception_line:
            text = exception_line

    text = _strip_quotes(text)

    # Must run before the loop below: the URL rule collapses "https://…" to
    # the literal token "URL" (losing the "http" anchor this depends on), and
    # the path rule reads "HTTP/1.1" as a relative path with a ".1" extension
    # (swallowing the anchor along with the code). See _preserve_http_status.
    text = _preserve_http_status(text)

    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)

    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def fingerprint(tool_name: str, content: str) -> str:
    """Stable short id for an error shape, scoped by the tool that produced it.

    Hashes the full normalized text so errors sharing a long prefix but with
    different tails remain distinct patterns.
    """
    key = f"{tool_name or ''}|{normalize_error(content)}"
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


# NTP drift between two real machines is seconds, not hours: 300s tolerates a
# desktop and a server clock disagreeing without admitting the garbage this
# exists to catch -- a live server carried rows at 1.06e305 and -7.56e166.
MAX_CLOCK_SKEW_SECONDS = 300


def believable_ts(value: Any, *, now: float) -> Optional[float]:
    """A host-owned timestamp, or ``None`` when it cannot be believed.

    ``messages.timestamp`` is untrusted host input, compared against a
    plugin-owned clock at three sites with no validation: the cross-session
    horizon, a usage-count fallback query, and the audit recurrence test. A
    future-dated row is inside every horizon forever; a non-positive row is
    invisible to every horizon forever -- and callers historically read a
    missing value as ``0``, which is itself outside every horizon and so
    reports confident silence instead of "unmeasured". ``None`` means exactly
    that: no time. Callers must not fold it back into ``0``.

    ``now`` is a required, explicit parameter rather than an internal
    ``time.time()`` call so this stays a pure function: the same input always
    produces the same output, and callers control the clock in tests.
    """
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ts) or ts <= 0:
        return None
    if ts > now + MAX_CLOCK_SKEW_SECONDS:
        return None
    return ts


FORMAT_PATTERNS_LIMIT = 8


def extract_patterns(
    items: Iterable[Dict[str, Any]], limit: Optional[int] = FORMAT_PATTERNS_LIMIT
) -> List[Dict[str, Any]]:
    """Group error occurrences into counted patterns.

    ``limit=None`` returns every pattern and is used by the post-edit audit and
    signal gating. Prompt rendering separately retains the small interactive budget.
    """
    grouped: Dict[str, Dict[str, Any]] = {}

    for item in items:
        content = str(item.get("content") or "")
        if not content:
            continue
        tool = str(item.get("tool") or "")
        fp = fingerprint(tool, content)
        # A row whose host timestamp could not be believed arrives as None
        # (see believable_ts). ``or 0`` here would fold that back into a
        # value every horizon excludes -- the same confident-silence defect
        # this module exists downstream of. None must stay None.
        ts = item.get("ts")
        sid = str(item.get("session_id") or "")

        entry = grouped.get(fp)
        if entry is None:
            grouped[fp] = {
                "fingerprint": fp,
                "tool": tool,
                "sample": content[:300],
                "shape": normalize_error(content),
                "count": 1,
                "_sessions": {sid} if sid else set(),
                "first_ts": ts,
                "last_ts": ts,
            }
            continue

        entry["count"] += 1
        if sid:
            entry["_sessions"].add(sid)
        if ts:
            entry["first_ts"] = min(entry["first_ts"] or ts, ts)
            entry["last_ts"] = max(entry["last_ts"] or ts, ts)

    out: List[Dict[str, Any]] = []
    for entry in grouped.values():
        sessions = entry.pop("_sessions")
        entry["sessions_seen"] = max(1, len(sessions))
        out.append(entry)

    out.sort(key=lambda entry: (entry["sessions_seen"], entry["count"]), reverse=True)
    return out if limit is None else out[:limit]


def merge_patterns(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge overlapping current/cross-session windows without double-counting."""
    merged: Dict[str, Dict[str, Any]] = {}
    for group in groups:
        for entry in group or []:
            fp = entry.get("fingerprint", "")
            if not fp:
                continue
            current = merged.get(fp)
            if current is None:
                merged[fp] = dict(entry)
                continue
            current["count"] = max(current.get("count", 0), entry.get("count", 0))
            current["sessions_seen"] = max(
                current.get("sessions_seen", 1), entry.get("sessions_seen", 1)
            )
            # None means "no believable time was ever observed", not 0 --
            # folding it into 0 would read as ancient/ended-silence. Only
            # take a real value from a side that has one; stay None when
            # neither side does.
            current_first, entry_first = current.get("first_ts"), entry.get("first_ts")
            if current_first is None:
                current["first_ts"] = entry_first
            elif entry_first is not None:
                current["first_ts"] = min(current_first, entry_first)
            current_last, entry_last = current.get("last_ts"), entry.get("last_ts")
            if current_last is None:
                current["last_ts"] = entry_last
            elif entry_last is not None:
                current["last_ts"] = max(current_last, entry_last)

    out = list(merged.values())
    out.sort(key=lambda entry: (entry.get("sessions_seen", 1), entry.get("count", 0)), reverse=True)
    return out


def _pattern_has_signal(entry: Dict[str, Any], *, min_count: int, session_cap: int) -> bool:
    session_threshold = min(session_cap, max(1, min_count // 2 + 1))
    return (
        entry.get("count", 0) >= min_count
        or entry.get("sessions_seen", 1) >= session_threshold
    )


def prioritize_signal_patterns(
    patterns: List[Dict[str, Any]],
    *,
    min_count: int,
    session_cap: int,
    limit: int = FORMAT_PATTERNS_LIMIT,
) -> List[Dict[str, Any]]:
    """Keep bounded evidence while ensuring the rendered set can open the gate.

    Cross-session aggregation may need every observed pattern to decide whether a
    durable signal exists, but evidence and prompt payloads must remain bounded.
    Put qualifying patterns first, then fill the remaining display budget in the
    established relevance order. Calling ``has_signal()`` on this result keeps
    the decision tied to data the proposal model can actually inspect.
    """
    if limit <= 0:
        return []
    return sorted(
        patterns or [],
        key=lambda entry: not _pattern_has_signal(
            entry, min_count=min_count, session_cap=session_cap
        ),
    )[:limit]


def has_signal(
    patterns: List[Dict[str, Any]],
    corrections: List[Any],
    min_count: int = 2,
    session_cap: int = 25,
) -> bool:
    """Return whether a repeated failure or explicit correction is present.

    A pattern seen in several distinct sessions is stronger evidence than the
    same count inside one session, so the cross-session threshold is derived
    below ``min_count`` rather than equal to it. It must still scale with
    ``min_count``: pinning it to a constant (as an earlier version did) meant a
    caller asking for a strict threshold (``min_count=100``) still had its gate
    opened by any ordinary two-session pattern, defeating the caller's request
    entirely. Halving keeps the default (``min_count=2`` -> threshold ``2``)
    and the ``min_count=3`` cross-session case byte-identical to before, while
    a much higher ``min_count`` now requires a proportionally wider spread.

    ``session_cap`` bounds the threshold to what the cross-session query can
    actually return (default ``cross_session_max_sessions = 25``). Without this
    clamp, ``min_count > 50`` would make ``session_threshold`` unreachable.
    """
    if corrections:
        return True
    for entry in patterns or []:
        if _pattern_has_signal(entry, min_count=min_count, session_cap=session_cap):
            return True
    return False



def format_patterns(
    patterns: List[Dict[str, Any]], limit: int = FORMAT_PATTERNS_LIMIT
) -> str:
    """Render patterns as a compact block for the proposal prompt.

    ``sample`` is raw error content, the same text ``fingerprint()`` hashed —
    but this function only renders it for display, never re-fingerprints it.
    Escaping ``<``/``>`` here cannot change any fingerprint: fingerprinting
    already happened in ``extract_patterns`` before this function ever runs.
    """
    if not patterns:
        return "  (none)"
    lines = []
    for entry in patterns[:limit]:
        lines.append(
            "  [{count}x across {sessions} session(s)] {tool} — {sample} (fp:{fp})".format(
                count=entry.get("count", 1),
                sessions=entry.get("sessions_seen", 1),
                tool=re.sub(
                    r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+",
                    " ",
                    scrub_text(str(entry.get("tool") or "?")),
                ).replace("<", "&lt;").replace(">", "&gt;"),
                sample=re.sub(
                    r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+",
                    " ",
                    scrub_text(str(entry.get("sample") or "")),
                )[:160].replace("<", "&lt;").replace(">", "&gt;"),
                fp=scrub_text(str(entry.get("fingerprint", ""))),
            )
        )
    return "\n".join(lines)
