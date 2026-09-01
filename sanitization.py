"""Recursive credential redaction and line-structure hygiene shared by the
evidence and persistence paths."""

import re
import unicodedata
from typing import Any

_REDACTED = "[REDACTED]"

# Every codepoint that can end a line, which is not the same set as "control
# characters". Two callers need the same answer for opposite purposes: core
# refuses these inside a skill or memory body, llm collapses them out of a value
# whose contract is to render as one prompt line. One list, because two lists
# drift and the drift is invisible -- each site keeps working while agreeing
# about a different set of characters.
#
# This is exactly the set ``str.splitlines()`` splits on;
# ``test_line_break_chars_match_str_splitlines`` holds the definition to that
# over the whole codepoint range, so a future codepoint cannot be missed by hand.
LINE_BREAK_CHARS = frozenset(
    "\n"        # U+000A LINE FEED
    "\v"        # U+000B LINE TABULATION
    "\f"        # U+000C FORM FEED
    "\r"        # U+000D CARRIAGE RETURN
    "\x1c"      # U+001C FILE SEPARATOR
    "\x1d"      # U+001D GROUP SEPARATOR
    "\x1e"      # U+001E RECORD SEPARATOR
    "\x85"      # U+0085 NEXT LINE            (category Cc)
    "\u2028"    # U+2028 LINE SEPARATOR       (category Zl)
    "\u2029"    # U+2029 PARAGRAPH SEPARATOR  (category Zp)
)

LINE_BREAK_RE = re.compile(
    "[" + "".join(re.escape(ch) for ch in sorted(LINE_BREAK_CHARS)) + "]+"
)

_FIXED_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"gh[pours]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    # Stripe issues secret AND restricted keys; only sk_ was covered, so a
    # live restricted key (rk_live_...) went out in the clear (audit 08-02).
    re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    # Stripe webhook signing secret -- a credential in its own right.
    re.compile(r"whsec_[A-Za-z0-9]{24,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"ntn_[A-Za-z0-9]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),
    re.compile(r"SG\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    re.compile(r"dop_v1_[a-f0-9]{60,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    # `[A-Z ]*` in the key-block header excluded digits, so an RFC 4716
    # `SSH2 ENCRYPTED PRIVATE KEY` block -- the `2` -- matched nothing and the
    # whole key body leaked (audit 08-03). Allow digits in the header words.
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----",
        re.S,
    ),
]

# Match exact generic labels and common compounds such as client_secret,
# access-token, refreshToken, and github_api_key. The explicit aliases cover
# common credential-store fields without treating every ``key`` as secret.
_SECRET_KEY = (
    r"(?:authorization|bearer|credentials?|private[_-]?key|access[_-]?key|"
    r"auth|cookie|session[_-]?id|db[_-]?pass|"
    r"[A-Za-z0-9_-]*(?:api[_-]?key|password|passwd|secret|token)[A-Za-z0-9_-]*)"
)
# A quoted key must close with the same delimiter that opened it. The value
# alternatives are disjoint: escaped characters start with a backslash, while
# ordinary characters exclude both backslashes and the closing delimiter. This
# avoids the exponential ambiguity of ``(?:\\.|(?!quote).)+`` on attacker text.
_SECRET_PREFIX = (
    rf"(?:(?P<key_quote>[\"']){_SECRET_KEY}(?P=key_quote)|"
    rf"\b{_SECRET_KEY}\b)\s*[:=]\s*"
)
_QUOTED_SECRET = re.compile(
    rf"(?i)(?P<prefix>{_SECRET_PREFIX})"
    r"(?P<quote>[\"'])(?P<value>(?:\\[^\r\n]|(?!(?P=quote))[^\\\r\n])+)(?P=quote)"
)
# HTTP auth schemes that precede a bare credential. `bearer`/`basic` were the
# only two covered, so `Authorization: Token <hex>` leaked whole and
# `Authorization: ApiKey <key>` lost its scheme to the generic unquoted pass
# while the key itself survived (audit 08-04). One shared alternation keeps
# _UNQUOTED_SECRET, _AUTH_TOKEN and the two marker-preservation patterns below
# in agreement; they drift into a leak if edited separately.
_AUTH_SCHEME = r"(?:bearer|basic|token|apikey)"
_UNQUOTED_SECRET = re.compile(
    rf"(?i)(?P<prefix>{_SECRET_PREFIX})"
    rf"(?P<value>(?:[\"']?{_AUTH_SCHEME}\s+\[REDACTED\][\"']?|[^\s,;&\}}\]\[]{{6,}}))"
)
# The token may be quoted on EITHER side of the scheme: `Bearer "tok"` as well
# as `"Bearer tok"`. The old pattern only allowed a quote before the scheme, so
# `Bearer "tok"` fell through to the unquoted pass, which then mistook the word
# `Bearer` for the secret and produced `[REDACTED] "[REDACTED]"` (audit 08-01).
_AUTH_TOKEN = re.compile(
    rf"(?i)(?P<label>\b{_AUTH_SCHEME}\s+)(?P<quote>[\"']?)"
    r"[A-Za-z0-9._~+/=-]{8,}(?P<close>[\"']?)"
)
# Preserve only already-canonical auth fields as units before marker splitting.
# Without this, a later boundary pass sees the pre-marker fragment
# (``credentials=Bearer ``) by itself and destroys the auth scheme.
# The quote may sit before the scheme (``"Bearer [REDACTED]"``) or between the
# scheme and the marker (``Bearer "[REDACTED]"``). Both are canonical outputs of
# _AUTH_TOKEN and must be protected as a unit; without the second layout the
# unquoted pass matched the bare word ``Bearer`` as a 6-char secret and rewrote
# it to ``[REDACTED] "[REDACTED]"`` (audit 08-01).
_CANONICAL_AUTH_FIELD = re.compile(
    rf"(?i){_SECRET_PREFIX}[\"']?{_AUTH_SCHEME}\s+[\"']?\[REDACTED\]"
    r"(?:[\"'](?=$|[\s,;&\}\]])|(?=$|[\s,;&\}\]]))"
)
# A marker inside a credential value is untrusted input, not proof that the
# rest of that value was scrubbed. Remove a token suffix before marker splitting
# so it cannot leak through a forged `[REDACTED]` fragment. Whitespace only
# continues an unquoted credential when it is not starting another key/value
# field; otherwise repeated scrubbing would erase neighboring telemetry.
_FORGED_AUTH_MARKER_FIELD = re.compile(
    rf"(?ix)(?P<prefix>{_SECRET_PREFIX})(?:"
    rf"(?P<quote>[\"'])(?P<quoted_scheme>{_AUTH_SCHEME})\s+"
    r"\[REDACTED\][^\r\n\"']+(?P<close>(?P=quote))?"
    rf"|(?P<unquoted_scheme>{_AUTH_SCHEME})\s+\[REDACTED\](?:[^\s,;&\}}\]]+|"
    r"\s+(?![A-Za-z_][A-Za-z0-9_.-]*\s*[:=])[^\s,;&\}\]]+))"
)
_FORGED_SECRET_MARKER_FIELD = re.compile(
    rf"(?ix)(?P<prefix>{_SECRET_PREFIX})(?:"
    r"(?P<quote>[\"'])\[REDACTED\][^\r\n\"']+(?P<close>(?P=quote))?"
    r"|\[REDACTED\][^\s,;&\}\]]+)"
)
_AUTH_SCHEME_KEYS = {"authorization", "auth", "credential", "credentials"}
# The already-canonical `<scheme> [REDACTED]` values the quoted pass must leave
# alone, derived from the one scheme list so it cannot fall behind it.
_CANONICAL_SCHEME_MARKERS = {
    f"{scheme} {_REDACTED}".lower()
    for scheme in ("bearer", "basic", "token", "apikey")
}
_URL_CREDENTIALS = re.compile(
    r"(?<![A-Za-z0-9+.-])([a-zA-Z][a-zA-Z0-9+.-]*://)"
    r"[^\s/?#]+@(?=[^\s/?#]+(?:[/?#]|\s|$))"
)
_ENV_SECRET = re.compile(
    r"(?m)^(\s*(?:export\s+|set\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*\s*=\s*)\S+$"
)
_NON_SECRETS = {"true", "false", "null", "none", "enabled", "disabled"}
# Numeric values only avoid generic credential redaction for these exact telemetry
# field names. Broad classes such as "token" and "key" still cover numeric API
# credentials, which are just as sensitive as alphabetic ones.
_NUMERIC_METRIC_KEYS = {
    "max_tokens", "min_tokens", "total_tokens", "input_tokens", "output_tokens",
    "prompt_tokens", "completion_tokens", "context_tokens", "cached_tokens",
}
# These are ordinary parser/telemetry fields, not credential labels. They match
# the generic ``token`` compound rule above, so protect them by exact key name
# rather than weakening the credential grammar with a broad substring exception.
_NON_SECRET_TOKEN_KEYS = {"tokenizer", "token_count"}


def _normalize_compatibility_forms(text: str) -> str:
    """Canonicalize fullwidth/compatibility ASCII forms, nothing else."""
    if not any(0xFF01 <= ord(ch) <= 0xFF5E or ch == "\u3000" for ch in text):
        return text
    return "".join(
        unicodedata.normalize("NFKC", ch)
        if 0xFF01 <= ord(ch) <= 0xFF5E or ch == "\u3000"
        else ch
        for ch in text
    )


def _key_from_prefix(prefix: str) -> str:
    """Extract one normalized key from a matched ``key[:=]`` prefix."""
    key = re.sub(r"\s*[:=]\s*$", "", prefix).strip()
    return key.strip("\"'").lower()


def _preserve_non_secret_token_field(prefix: str) -> bool:
    return _key_from_prefix(prefix) in _NON_SECRET_TOKEN_KEYS


def _replace_quoted(match: re.Match) -> str:
    prefix = match.group("prefix")
    key = _key_from_prefix(prefix)
    value = match.group("value")
    # The auth-token pass has already reduced this exact credential form to the
    # canonical marker. Do not let the generic auth-field pass erase its scheme
    # (or add a second marker) on the same scrub cycle.
    if (
        _preserve_non_secret_token_field(prefix)
        or (
            key in _AUTH_SCHEME_KEYS
            and value.lower() in _CANONICAL_SCHEME_MARKERS
        )
        or value.lower() in _NON_SECRETS
        or (_is_number(value) and key in _NUMERIC_METRIC_KEYS)
    ):
        return match.group(0)
    return (
        f"{prefix}{match.group('quote')}"
        f"{_REDACTED}{match.group('quote')}"
    )


def _is_number(value: str) -> bool:
    """Return True for integer and float literals."""
    try:
        float(value)
        return True
    except (ValueError, OverflowError):
        return False


def _replace_unquoted(match: re.Match) -> str:
    value = match.group("value")
    prefix = match.group("prefix")
    key = _key_from_prefix(prefix)
    literal = value
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        literal = value[1:-1]
    # `_AUTH_TOKEN` already reduced `Bearer "tok"` to `Bearer "[REDACTED]"`, and
    # the marker now sits inside the quote. This pass would otherwise match the
    # bare scheme word `Bearer` as a 6-char secret and rewrite it, producing
    # `[REDACTED] "[REDACTED]"` (audit 08-01). If the value is exactly an auth
    # scheme and the very next thing is a quoted-or-bare redaction marker, this
    # is finished canonical output -- leave it whole.
    if (
        key in _AUTH_SCHEME_KEYS
        and re.fullmatch(rf"(?i){_AUTH_SCHEME}", value)
        and re.match(r"\s*[\"']?\[REDACTED\]", match.string[match.end():])
    ):
        return match.group(0)
    if (
        _preserve_non_secret_token_field(prefix)
        or (
            key in _AUTH_SCHEME_KEYS
            and re.fullmatch(
                rf"(?i)[\"']?{_AUTH_SCHEME}\s+\[REDACTED\][\"']?",
                value,
            )
        )
        or literal.lower() in _NON_SECRETS
        or (_is_number(literal) and key in _NUMERIC_METRIC_KEYS)
    ):
        return match.group(0)
    # Canonical markers are protected before the ordinary marker split. Do not
    # exempt arbitrary credentials merely because a value names redaction.
    return f"{match.group('prefix')}{_REDACTED}"


def _scrub_chunk(text: str) -> str:
    for pattern in _FIXED_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)
    text = _ENV_SECRET.sub(r"\1[REDACTED]", text)
    text = _AUTH_TOKEN.sub(
        lambda m: f"{m.group('label')}{m.group('quote')}{_REDACTED}{m.group('close')}",
        text,
    )
    text = _QUOTED_SECRET.sub(_replace_quoted, text)
    return _UNQUOTED_SECRET.sub(_replace_unquoted, text)


def _replace_forged_auth_marker(match: re.Match) -> str:
    scheme = match.group("quoted_scheme") or match.group("unquoted_scheme")
    return (
        f"{match.group('prefix')}{match.group('quote') or ''}"
        f"{scheme.title()} {_REDACTED}{match.group('close') or ''}"
    )


def scrub_text(text: str) -> str:
    """Redact credentials while preserving existing redaction markers exactly."""
    if not text:
        return text

    # Normalize fullwidth/compatibility ASCII forms before matching (P0 02-01).
    # Scoped to the compatibility block so ordinary Unicode (typographic
    # punctuation, Cyrillic, CJK prose) passes through byte-identical.
    text = _normalize_compatibility_forms(text)

    # Reject forged markers with a token suffix before preserving any canonical
    # field. The marker is not evidence that untrusted trailing text is safe.
    text = _FORGED_SECRET_MARKER_FIELD.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote') or ''}"
        f"{_REDACTED}{match.group('close') or ''}",
        text,
    )
    text = _FORGED_AUTH_MARKER_FIELD.sub(_replace_forged_auth_marker, text)

    def scrub_unprotected(chunk: str) -> str:
        # Sanitized proposals cross several trust boundaries. Splitting around
        # ordinary markers keeps generic secret matching from consuming one,
        # making repeated scrubbing idempotent.
        return _REDACTED.join(_scrub_chunk(part) for part in chunk.split(_REDACTED))

    # A canonical auth field includes no secret, but it must stay intact as a
    # record. Protect it before the ordinary marker split so aliases such as
    # ``credentials`` retain the protocol scheme on subsequent boundaries.
    protected: list[str] = []
    position = 0
    for match in _CANONICAL_AUTH_FIELD.finditer(text):
        protected.append(scrub_unprotected(text[position:match.start()]))
        protected.append(match.group(0))
        position = match.end()
    protected.append(scrub_unprotected(text[position:]))
    return "".join(protected)


def sanitize(value: Any) -> Any:
    """Recursively scrub every string in a journal- or model-bound value."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (bytes, bytearray)):
        text = value.decode("utf-8", errors="surrogateescape")
        scrubbed = scrub_text(text)
        return type(value)(scrubbed.encode("utf-8", errors="surrogateescape"))
    if isinstance(value, dict):
        return {
            (scrub_text(key) if isinstance(key, str) else key): sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    if isinstance(value, set):
        return {sanitize(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(sanitize(item) for item in value)
    return value
