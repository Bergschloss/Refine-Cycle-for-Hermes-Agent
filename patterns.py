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
    from .sanitization import LINE_BREAK_RE, scrub_text
except ImportError:
    from sanitization import LINE_BREAK_RE, scrub_text  # type: ignore

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
#
# Captured with ``_SEGMENT`` -- the same class the path rule tokenizes with --
# not a bare ``\S+``: a bare capture swallows trailing sentence punctuation a
# real path segment never would (a comma before "try again" is not part of a
# path either), which would keep two occurrences of the identical flag apart
# whenever one sentence happened to end differently than the other.
_CLI_FLAG = re.compile(rf"(?i)\b(option|flag|switch)\s+/({_SEGMENT})")

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


# An exit code, a port and a signal number are the semantic core of a failure
# the same way an HTTP status is: ``exit code 127`` (command not found) and
# ``exit code 1`` (any failure) carry different lessons, ``port 22`` and
# ``443`` are different services, and ``signal 9`` (SIGKILL) and ``15``
# (SIGTERM) are different deaths. The blanket ``\b\d+\b -> N`` rule erased all
# of them into one shape, so two unrelated failures fingerprinted as one seen
# twice -- exactly the recurrence the signal gate then trusts. Preserve the
# value by gluing it to a letter prefix (``exitcode127``), so the later
# ``\b\d+\b`` rule cannot reach the digits, mirroring ``httpstatus404``.
#
# Each rule is keyword-anchored so an incidental number nearby is NOT promoted:
# ``\bport\b`` will not fire inside ``reported``/``export``, and the count in
# ``the port was busy; 500 retries`` is not adjacent to the keyword so it still
# collapses. The colon-port form requires evidence of a HOST on its left (see
# the four rules below), never a bare ``:NN`` -- a clock (``12:34:56``) has only
# digits around its colons, and a port inside a path (``/v2/8080/items``) has no
# host:port colon, so both keep collapsing.
_EXIT_CODE_NUM = re.compile(
    r"(?i)\bexit(?:\s*(?:code|status)|code|status)\b\s*[:=]?\s*(\d+)"
)
_SIGNAL_NUM = re.compile(r"(?i)\bsignal\b\s*[:=]?\s*(\d+)")
_PORT_WORD_NUM = re.compile(r"(?i)\bport\b\s*[:=]?\s*(\d+)")
# A colon-port after tcp/udp (``tcp :22``); a trailing ``[:.]\d`` guard keeps it
# off a clock or a dotted-decimal.
_PORT_TCP_NUM = re.compile(r"(?i)\b(?:tcp|udp)\b\s*:\s*(\d{1,5})\b(?![:.]\d)")

# ``<token>:<digits>`` is NOT evidence of a port. The rule here used to be any
# letter-led token followed by ``:digits``, which promoted every source location
# (``file.py:42``), retry counter (``retries:500``) and bare key/value field to a
# port. That is this module's other failure direction: rather than fabricating
# recurrence it split ONE recurring failure into a fingerprint per line number,
# while a real IPv4 host:port was left to collapse because the host does not
# start with a letter. So each rule below needs actual host evidence:
#   * the literal ``localhost``;
#   * a dotted-quad IPv4 address;
#   * a dotted DNS name whose last label is not a source-file suffix -- that
#     guard is what separates ``api.example.com:443`` from ``file.py:42``;
#   * any host token introduced by explicit connection context
#     (``connect to db:5432``), which is the only case where a single-label host
#     can be told apart from a field name.
_PORT_LOCALHOST_NUM = re.compile(r"(?i)\blocalhost:(\d{1,5})\b(?![:.]\d)")
_PORT_IPV4_NUM = re.compile(
    r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}:(\d{1,5})\b(?![:.]\d)"
)
_PORT_DNS_NUM = re.compile(
    r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+([a-z]{2,24}):(\d{1,5})\b(?![:.]\d)"
)
_PORT_CONTEXT_NUM = re.compile(
    r"(?i)\b(?:connect(?:ing|ed)?\s+to|connection\s+to|dial(?:ing)?(?:\s+to)?"
    r"|upstream|proxy|hostname|host|server|address)\b\s*[:=]?\s*"
    r"[a-z][\w-]*(?:\.[\w-]+)*:(\d{1,5})\b(?![:.]\d)"
)
# ``name.py:12`` is a source location, not a host:port. Kept deliberately small:
# the suffixes that actually appear before a line number in tool output. A
# suffix missing from here only costs a false port preservation on that one
# shape; a TLD allowlist instead would silently stop preserving real hosts.
_SOURCE_FILE_SUFFIXES = frozenset({
    "py", "pyi", "js", "jsx", "mjs", "cjs", "ts", "tsx", "go", "rs", "rb", "php",
    "java", "kt", "kts", "swift", "c", "h", "cc", "cpp", "hpp", "cs", "sh", "bash",
    "ps1", "psm1", "sql", "yml", "yaml", "json", "toml", "ini", "cfg", "conf",
    "md", "rst", "txt", "log", "csv", "html", "htm", "css", "scss", "vue", "svelte",
    "tf", "lua", "pl", "r", "scala", "ex", "exs", "dart", "m", "mm", "asm", "s",
})


def _glue_group(match: "re.Match[str]", prefix: str, group: int = 1) -> str:
    """Prefix the digits captured by ``group`` inside the whole match.

    Span-based, not ``str.replace``: a host can repeat its own port's digits
    (``443.example.com:443``) and replacing the first occurrence would glue the
    prefix onto the host instead of the port.
    """
    start, end = match.span(group)
    offset = match.start()
    text = match.group(0)
    return f"{text[:start - offset]}{prefix}{match.group(group)}{text[end - offset:]}"


def _glue_number(match: "re.Match[str]", prefix: str) -> str:
    """Replace the captured number with ``<prefix><number>`` in the whole match,
    so the digits survive the later blanket-integer rule (see httpstatus)."""
    number = next(group for group in match.groups() if group)
    return match.group(0).replace(number, f"{prefix}{number}", 1)


def _glue_dns_port(match: "re.Match[str]") -> str:
    """Preserve a DNS host's port unless the "host" is really a source file."""
    if match.group(1).lower() in _SOURCE_FILE_SUFFIXES:
        return match.group(0)
    return _glue_group(match, "netport", 2)


def _preserve_semantic_numbers(text: str) -> str:
    """Shield exit codes, ports and signals before the blanket digit rule.

    Runs after ``_preserve_http_status`` and before ``_NORMALIZERS``: the glued
    token (``exitcode127``) carries no ``\\b\\d+\\b`` boundary, so the general
    integer rule leaves it alone while still erasing genuinely volatile numbers.
    """
    text = _EXIT_CODE_NUM.sub(lambda m: _glue_number(m, "exitcode"), text)
    text = _SIGNAL_NUM.sub(lambda m: _glue_number(m, "signal"), text)
    text = _PORT_WORD_NUM.sub(lambda m: _glue_number(m, "netport"), text)
    text = _PORT_TCP_NUM.sub(lambda m: _glue_number(m, "netport"), text)
    text = _PORT_LOCALHOST_NUM.sub(lambda m: _glue_group(m, "netport"), text)
    text = _PORT_IPV4_NUM.sub(lambda m: _glue_group(m, "netport"), text)
    text = _PORT_DNS_NUM.sub(_glue_dns_port, text)
    text = _PORT_CONTEXT_NUM.sub(lambda m: _glue_group(m, "netport"), text)
    return text


# Order matters: timestamps and paths must be replaced before bare integers,
# otherwise the digit rule eats the parts that make them recognizable.
_NORMALIZERS = [
    # ISO-ish timestamps and clock times
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "T"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "T"),
    # Single-quoted literals: keep the contents for the same reason as above
    # (``KeyError: 'user_id'`` is identified by the name, not by the quotes).
    # Require non-letter boundaries so the apostrophe inside a contraction
    # (``Don't``, ``doesn't``) is not treated as an opening/closing quote and
    # collapsed to ``dont``/``doesnt`` — that mangling silently re-partitioned
    # the fingerprint of any error phrased with a contraction.
    (re.compile(r"(?<![a-zA-Z])'([^']*)'(?![a-zA-Z])"), r"\1"),
    # CLI switches, before the path rule can mistake one for a single-segment
    # POSIX path — see _CLI_FLAG above. A trailing "." is stripped from the
    # flag itself (not just excluded from the match, the way a comma already
    # is by _SEGMENT): _SEGMENT admits "." mid-token for real filenames
    # ("/help.txt"), so a sentence-final period after a bare flag ("/help.")
    # is still captured, and must not survive into the replacement or "/help"
    # and "/help." would fingerprint apart for no reason.
    (_CLI_FLAG, lambda m: f"{m.group(1)} cliflag_{m.group(2).rstrip('.') or m.group(2)}"),
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
            # Find the block's terminal non-wrapper line, and its index, so a
            # multiline message can be joined back from it.
            terminal = ""
            terminal_index = None
            for rev_offset, line in enumerate(reversed(block)):
                if not line.strip():
                    continue
                if (
                    _TRACEBACK_WRAPPER_LINE.match(line.strip())
                    or _TRACEBACK_CHAIN_LINE.match(line.strip())
                    or _TRACEBACK_RUNNER_FOOTER_LINE.match(line.strip())
                ):
                    continue
                terminal = line
                terminal_index = len(block) - 1 - rev_offset
                break
            if terminal and _is_python_exception_line(terminal):
                # The terminal line is itself the exception (single-line
                # message). Indented or not, it is the whole thing -- this is
                # the path that already worked, and the indented case
                # (``ConnectionError: timed out`` as the last line) must keep
                # working.
                exception_line = terminal.strip()
            elif terminal_index is not None:
                # The terminal line is NOT an exception line: it may be the
                # continuation of a multiline exception whose message ran past
                # its type. A REAL printed exception line begins at column 0;
                # frames and typed source annotations (``ConnectionError:
                # annotation`` inside a frame) are indented, so requiring
                # column 0 here is exactly what keeps genuine terminal prose
                # (with only an indented, annotation-shaped line above it) from
                # being mistaken for the exception. Scan back for that column-0
                # exception line; a frame (``File "``) between it and the
                # terminal means the terminal is unrelated prose, not a
                # continuation, so stop.
                exception_start = None
                for offset in range(terminal_index - 1, -1, -1):
                    line = block[offset]
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if (
                        stripped.startswith('File "')
                        or _TRACEBACK_WRAPPER_LINE.match(stripped)
                        or _TRACEBACK_CHAIN_LINE.match(stripped)
                        or _TRACEBACK_RUNNER_FOOTER_LINE.match(stripped)
                    ):
                        break
                    if line[:1].isspace():
                        continue
                    # Keep the HIGHEST believed line in this block, and do not
                    # stop at the first one found from the bottom.
                    #
                    # _is_python_exception_line judges ``partition(":")[0]``, so
                    # a line with no colon is judged on its whole text and one
                    # bare word like ``retrying`` is a valid identifier. Stopping
                    # at the first believed line therefore stopped on that
                    # continuation and joined from there, discarding the real
                    # ``RateLimitError:`` above it -- so
                    #   RateLimitError: rate limited / retrying / gave up
                    #   PermissionError: permission denied / retrying / gave up
                    # both normalized to "retrying gave up" and aggregated into
                    # ONE pattern at count=2 over two sessions, a fabricated
                    # failure that passes the recurrence gate while both real
                    # ones disappear.
                    #
                    # Requiring a colon here instead was WRONG, and measurably:
                    # a message-less exception prints without one, so a chained
                    # traceback ending in ``KeyboardInterrupt`` plus a line of
                    # tool output found nothing in its own block and fell back to
                    # an EARLIER block -- reporting the root exception and
                    # collapsing two different terminal failures into it -- while
                    # the same message-less failure raised from two frames
                    # stopped collapsing at all. Both were correct before.
                    #
                    # Continuing the walk needs no colon and fixes all three: the
                    # frame, wrapper, chain and footer boundaries above already
                    # end it, and in a real traceback the exception line is
                    # preceded by a ``File "`` frame, so the highest believed line
                    # in the block IS the exception and everything below it is its
                    # message.
                    if _is_python_exception_line(line):
                        exception_start = offset
                    # A column-0 line that is not an exception line is another
                    # continuation: Python prints every line of a multi-line
                    # message at column 0, so a message with two or more
                    # continuation lines has several of them stacked above the
                    # terminal. Stopping at the first one found only the
                    # single-continuation shape and left every longer real
                    # message with its frames -- the same non-aggregation this
                    # branch exists to fix. Keep walking; the frame and wrapper
                    # boundaries above are what stop the walk.
                if exception_start is not None:
                    # Join the column-0 exception line with the continuation
                    # lines down to the terminal, dropping the frames above it
                    # exactly as the single-line path does. Stop at a new frame
                    # so an interleaved block cannot fold stack text back in.
                    message_lines = []
                    for line in block[exception_start:terminal_index + 1]:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if (
                            stripped.startswith('File "')
                            or _TRACEBACK_WRAPPER_LINE.match(stripped)
                            or _TRACEBACK_CHAIN_LINE.match(stripped)
                            or _TRACEBACK_RUNNER_FOOTER_LINE.match(stripped)
                        ):
                            break
                        message_lines.append(stripped)
                    exception_line = " ".join(message_lines)
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
    # Same idea, for exit codes / ports / signals: shield the semantically
    # meaningful number before the blanket integer rule collapses it.
    text = _preserve_semantic_numbers(text)

    for pattern, replacement in _NORMALIZERS:
        text = pattern.sub(replacement, text)

    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def fingerprint(tool_name: str, content: str) -> str:
    """Stable short id for an error shape, scoped by the tool that produced it.

    Hashes the full normalized text so errors sharing a long prefix but with
    different tails remain distinct patterns.

    "Stable" is stable only against unchanged ``normalize_error`` output --
    every rule change here changes the fingerprint of any message the rule
    touches. A ``pattern_fingerprint`` already written to the journal or
    ``skill_stats.json`` is compared against a freshly recomputed live window
    (``ledger.audit``'s ``by_fingerprint`` lookup), never against itself, so a
    rule change silently orphans any stored fingerprint it altered -- the row
    keeps its outcome but audit can no longer prove recurrence for it. Checked
    for the HTTP-status/CLI-flag rules added in H5: 0 of this install's stored
    fingerprints were affected, but that was a live measurement at the time,
    not a property this function enforces. A rule change wide enough to matter
    should re-check the same way before shipping.
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


# B1: a tool's own self-correcting refusal already states its remedy, so
# recording it as a "lesson learned" teaches nothing about the agent's
# behaviour. Measured on the live journal/state.db corpus, the only two
# examples that exist are ``tool_search``'s ``"query is required"`` and
# ``memory``'s ``"content is required for 'replace' action."``, both anchored
# within the message's first dozen characters (a tool's structured refusal,
# not prose deep in an unrelated error).
#
# The spec that motivated this also named two more shapes -- a "did you mean
# X" suggestion and a usage line. Both were measured against the same corpus
# and rejected: they matched Python's own ``AttributeError`` text (``... has
# no attribute 'x'. Did you mean: '__dir__'?``) inside unrelated tracebacks,
# which is a real recurring failure, not a self-correcting one. Shipping them
# would have suppressed genuine signal on data this project actually has, so
# only the measured rule ships; the other two can be added later against a
# real self-correcting example that needs them.
_SELF_CORRECTING_REQUIRED_PARAM_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9_]{1,40})\s+is\s+required\b"
)
# How close to the start of the message the match must land. A tool's own
# structured refusal names the missing parameter immediately; "is required"
# appearing only after this many characters is far more likely to be
# incidental prose inside a longer, unrelated failure.
_SELF_CORRECTING_ANCHOR_CHARS = 40
# "<word> is required" alone does not mean a parameter was omitted. The same
# sentence carries the whole missing-credential / missing-permission family --
# "Authentication is required", "Approval is required for this write",
# "GITHUB_TOKEN is required" -- and those are neither self-correcting nor
# unactionable: they are exactly the recurring failures a lesson should be
# written about (export the token, request the scope, expect the gate). The
# test that separates them is not grammar but whether the tool could have
# supplied the value by retrying: it can supply ``query`` or ``content``, and
# it cannot conjure a credential, a permission or an approval.
#
# Two forms, because the same missing secret is written both ways. An exact
# subject, and a suffix for the compound names that carry the same meaning
# (``github_token``, ``openai_api_key``, ``write_approval``) -- suffixes, not
# case, so ``GITHUB_TOKEN`` and ``github_token`` classify identically instead
# of splitting one failure two ways on capitalization. A trailing "s" is
# stripped before both lookups for the same reason, so a plural cannot classify
# opposite to its singular; listing both forms by hand is how that asymmetry
# gets in.
#
# One limit worth naming rather than discovering later: the regex captures the
# word immediately before "is required", so a compound phrase is judged by its
# last word -- "Authorization header is required" is classified on ``header``,
# not on ``authorization``. Widening that needs a different rule than a word
# list. The live corpus now has 6 rows of that exact shape, so it is no longer
# hypothetical; it is still a different rule and not a longer list.
#
# A hyphen is the third spelling axis, after casing and plurals, and it split
# the same way they used to. The regex stops at a word boundary, so
# "Re-authentication is required" is judged on ``authentication`` and released
# while "Reauthentication is required" is judged on ``reauthentication`` and was
# suppressed -- one failure, two classifications, decided by a hyphen. Suffix
# matching cannot close this the way ``_token`` closes ``github_token``: the
# list holds short words like ``key`` and ``auth``, and a bare-suffix rule would
# release "monkey is required". So the prefixed and modern spellings are listed
# explicitly below.
#
# Some of these words are also plausible parameter names -- a kv-store tool
# really does take ``key``, an auth tool ``session``. That direction is chosen
# deliberately, not overlooked: releasing a self-correcting refusal costs one
# unhelpful lesson candidate, which the proposer can still decline and which is
# visible in the evidence; suppressing a real failure costs a lesson that is
# never written and leaves no trace of the loss. When the two errors are not
# equally bad, take the visible one.
#
# On the live corpus (582 error rows) this list releases none of the rows the
# rule actually suppresses -- the three real cases are ``query``/``content``,
# both call parameters. That measurement describes today's corpus; it is not a
# guarantee about words this install has not seen yet.
_SELF_CORRECTING_NON_PARAM_SUBJECTS = frozenset({
    "access", "account", "apikey", "approval", "auth", "authentication",
    "authorisation", "authorization", "certificate", "confirmation", "consent",
    "credential", "credentials", "key", "keys", "license", "licence", "login",
    "password", "payment", "permission", "permissions", "scope", "scopes",
    "secret", "session", "signature", "subscription", "token", "tokens",
    "verification",
    # The same family, in the spellings a modern host actually emits. Each is a
    # credential or a re-authentication challenge by the definition already used
    # above -- nothing a tool can supply by retrying -- and each was suppressed
    # as a parameter refusal because the list stopped at the words that were
    # current when it was written. ``reauth*`` also closes the hyphen split.
    "mfa", "otp", "passkey", "reauth", "reauthentication",
    "reauthorisation", "reauthorization", "sso", "totp",
})
_SELF_CORRECTING_NON_PARAM_SUFFIXES = tuple(
    f"_{word}" for word in sorted(_SELF_CORRECTING_NON_PARAM_SUBJECTS)
)


def is_self_correcting_error(content: str) -> bool:
    """Whether an error message states its own remedy, so nothing is learned.

    Applied before the model call: the error text is already in the evidence
    by the time this runs, so gating it out here costs no tokens. A suppressed
    error is excluded from lesson candidacy (pattern/fingerprint aggregation)
    only -- callers keep it in whatever raw "we saw this" list they already
    maintain, so the pass can still say it happened.
    """
    if not content:
        return False
    # The two halves of the decision read different spans, deliberately.
    #
    # To SUPPRESS, a parameter-shaped subject must appear inside the anchor
    # window -- that is what makes it the tool's own structured refusal rather
    # than prose in a longer failure. To RELEASE, a non-parameter subject
    # anywhere in the message is enough: a credential or an approval named at
    # character 500 is still a real failure, and one message must not classify
    # two ways depending on which of its two subjects came first.
    matched_in_window = False
    for match in _SELF_CORRECTING_REQUIRED_PARAM_RE.finditer(content):
        subject = match.group(1).casefold()
        # Singular and plural must land on the same side, so both the word and
        # its de-pluralized form are looked up. "access" and "credentials"
        # are in the set in the form they are actually written, so stripping an
        # "s" that was never a plural costs nothing: the raw form is checked
        # first.
        forms = {subject}
        if subject.endswith("s"):
            forms.add(subject[:-1])
        if forms & _SELF_CORRECTING_NON_PARAM_SUBJECTS:
            return False
        if any(form.endswith(_SELF_CORRECTING_NON_PARAM_SUFFIXES) for form in forms):
            return False
        if match.start() <= _SELF_CORRECTING_ANCHOR_CHARS:
            matched_in_window = True
    return matched_in_window


FORMAT_PATTERNS_LIMIT = 8


def extract_patterns(
    items: Iterable[Dict[str, Any]],
    limit: Optional[int] = FORMAT_PATTERNS_LIMIT,
    suppressed_out: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Group error occurrences into counted patterns.

    ``limit=None`` returns every pattern and is used by the post-edit audit and
    signal gating. Prompt rendering separately retains the small interactive budget.

    ``suppressed_out``, when given a dict, is filled in place with
    ``self_correcting`` -- how many occurrences this call dropped from lesson
    candidacy. An out-parameter rather than a second return value or a module
    global: this module is pure and has several callers, and the automatic
    thread and an interactive call can run it at the same time. Without the
    count, a pass whose only evidence was suppressed journals the same
    ``no_op`` as a pass that saw nothing at all, which is the one thing this
    project requires to stay distinguishable afterwards.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    suppressed = 0

    for item in items:
        content = str(item.get("content") or "")
        if not content:
            continue
        if is_self_correcting_error(content):
            # Excluded from lesson candidacy only. The item still reached
            # here from evidence collection's raw error_items/tool_errors, so
            # a caller inspecting those (not this aggregation) can still see
            # it happened; it is simply never a repeated-failure pattern the
            # proposer is asked to fix.
            suppressed += 1
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
        # Keep WHICH sessions, not just how many. ``merge_patterns`` has to know
        # whether two windows describe the same occurrences or different ones, and
        # a bare count cannot answer that: it fell back to max(), so the same
        # failure seen once in each of two sessions merged to sessions_seen=1,
        # count=1 and the >=2 gate stayed shut on a genuinely chronic failure
        # (audit 06-01). Stored as a sorted list, not the working set, because
        # these entries reach ``evidence`` and must stay JSON-serializable.
        entry["_session_ids"] = sorted(sessions)
        out.append(entry)

    out.sort(key=lambda entry: (entry["sessions_seen"], entry["count"]), reverse=True)
    if suppressed_out is not None:
        suppressed_out["self_correcting"] = suppressed
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
            # Session identities decide whether these two windows overlap.
            #
            # max() alone is right for OVERLAPPING windows -- the current-session
            # window and the cross-session window can contain the same rows, and
            # summing would double-count them. It is wrong for DISJOINT ones: a
            # failure once in session A and once in session B is two occurrences
            # across two sessions, and max() reported one of each, holding the
            # >=2 gate shut on exactly the chronic cross-session failures this
            # plugin exists to find (audit 06-01).
            current_ids = current.get("_session_ids")
            entry_ids = entry.get("_session_ids")
            if isinstance(current_ids, list) and isinstance(entry_ids, list):
                union = sorted(set(current_ids) | set(entry_ids))
                current["_session_ids"] = union
                current["sessions_seen"] = max(1, len(union))
                if set(current_ids).isdisjoint(entry_ids):
                    current["count"] = current.get("count", 0) + entry.get("count", 0)
                else:
                    current["count"] = max(
                        current.get("count", 0), entry.get("count", 0))
            else:
                # An entry from an older caller carries no identities; the only
                # safe read is the conservative one this function always used.
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
                tool=LINE_BREAK_RE.sub(
                    " ", scrub_text(str(entry.get("tool") or "?"))
                ).replace("<", "&lt;").replace(">", "&gt;"),
                sample=LINE_BREAK_RE.sub(
                    " ", scrub_text(str(entry.get("sample") or ""))
                )[:160].replace("<", "&lt;").replace(">", "&gt;"),
                fp=scrub_text(str(entry.get("fingerprint", ""))),
            )
        )
    return "\n".join(lines)
