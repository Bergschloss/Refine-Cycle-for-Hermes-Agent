"""Recursive credential redaction shared by evidence and persistence paths."""

import re
from typing import Any

_REDACTED = "[REDACTED]"

_FIXED_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"gh[pours]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"ntn_[A-Za-z0-9]{20,}"),
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"glpat-[A-Za-z0-9_-]{15,}"),
    re.compile(r"SG\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}"),
    re.compile(r"dop_v1_[a-f0-9]{60,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----",
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
_UNQUOTED_SECRET = re.compile(
    rf"(?i)(?P<prefix>{_SECRET_PREFIX})"
    r"(?P<value>(?:[\"']?bearer\s+\[REDACTED\][\"']?|[^\s,;\}\]\[]{6,}))"
)
_BEARER = re.compile(
    r"(?i)(?P<label>\bbearer\s+)(?P<quote>[\"']?)[A-Za-z0-9._~+/=-]{8,}(?P<close>[\"']?)"
)
# Preserve only already-canonical credential fields as units before marker
# splitting. Without this, a later boundary pass sees the pre-marker fragment
# (``credentials=Bearer ``) by itself and destroys the auth scheme.
_CANONICAL_BEARER_FIELD = re.compile(
    rf"(?i){_SECRET_PREFIX}(?:[\"']?bearer\s+)\[REDACTED\]"
    r"(?:[\"'](?=$|[\s,;\}\]])|(?=$|[\s,;\}\]]))"
)
# A marker inside a credential value is untrusted input, not proof that the
# rest of that value was scrubbed. Remove a token suffix before marker splitting
# so it cannot leak through a forged `[REDACTED]` fragment.
_FORGED_BEARER_MARKER_FIELD = re.compile(
    rf"(?ix)(?P<prefix>{_SECRET_PREFIX})(?:"
    r"(?P<quote>[\"'])bearer\s+\[REDACTED\][^\r\n\"']+(?P<close>(?P=quote))?"
    r"|bearer\s+\[REDACTED\](?:[^\s,;\}\]]+|\s+[^\s,;\}\]]+))"
)
_BEARER_SCHEME_KEYS = {"authorization", "auth", "credential", "credentials"}
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


def _key_from_prefix(prefix: str) -> str:
    """Extract one normalized key from a matched ``key[:=]`` prefix."""
    key = re.sub(r"\s*[:=]\s*$", "", prefix).strip()
    return key.strip("\"'").lower()


def _preserve_non_secret_token_field(prefix: str) -> bool:
    return _key_from_prefix(prefix) in _NON_SECRET_TOKEN_KEYS


def _replace_quoted(match: re.Match) -> str:
    prefix = match.group("prefix")
    key = _key_from_prefix(prefix)
    # The Bearer pass has already reduced this exact credential form to the
    # canonical marker. Do not let the generic auth-field pass erase its scheme
    # (or add a second marker) on the same scrub cycle.
    if (
        _preserve_non_secret_token_field(prefix)
        or (
            key in _BEARER_SCHEME_KEYS
            and match.group("value").lower() == f"bearer {_REDACTED}".lower()
        )
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
    if (
        _preserve_non_secret_token_field(prefix)
        or (
            key in _BEARER_SCHEME_KEYS
            and re.fullmatch(r"(?i)[\"']?bearer\s+\[REDACTED\][\"']?", value)
        )
        or value.lower() in _NON_SECRETS
        or (_is_number(value) and key in _NUMERIC_METRIC_KEYS)
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
    text = _BEARER.sub(
        lambda m: f"{m.group('label')}{m.group('quote')}{_REDACTED}{m.group('close')}",
        text,
    )
    text = _QUOTED_SECRET.sub(_replace_quoted, text)
    return _UNQUOTED_SECRET.sub(_replace_unquoted, text)


def scrub_text(text: str) -> str:
    """Redact credentials while preserving existing redaction markers exactly."""
    if not text:
        return text

    # Reject forged markers with a token suffix before preserving any canonical
    # field. The marker is not evidence that untrusted trailing text is safe.
    text = _FORGED_BEARER_MARKER_FIELD.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote') or ''}"
        f"Bearer {_REDACTED}{match.group('close') or ''}",
        text,
    )

    def scrub_unprotected(chunk: str) -> str:
        # Sanitized proposals cross several trust boundaries. Splitting around
        # ordinary markers keeps generic secret matching from consuming one,
        # making repeated scrubbing idempotent.
        return _REDACTED.join(_scrub_chunk(part) for part in chunk.split(_REDACTED))

    # A canonical Bearer field includes no secret, but it must stay intact as a
    # record. Protect it before the ordinary marker split so aliases such as
    # ``credentials`` retain the protocol scheme on subsequent boundaries.
    protected: list[str] = []
    position = 0
    for match in _CANONICAL_BEARER_FIELD.finditer(text):
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
