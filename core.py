"""Core refine orchestration: evidence, guardrails, durable apply, rollback."""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from agent.plugin_llm import PluginLlm

try:
    from . import config, journal, ledger, llm as _llm, patterns
    from .sanitization import sanitize, scrub_text
    from . import trace as _trace
except ImportError:
    import config, journal, ledger, llm as _llm, patterns  # noqa: F811
    from sanitization import sanitize, scrub_text  # noqa: F811
    _trace = None

logger = logging.getLogger(__name__)

# Retry only transient primary proposal failures. Provider permission/region
# refusals are terminal and must not be sent twice.
_PRIMARY_RETRY_FAILURES = frozenset({"llm_call_error", "no_final_text"})
_PRIMARY_NONRETRY_MARKERS = (
    "regionerror", "permissiondenied", "permission denied", "forbidden",
    "unauthorized", "http 401", "http 403",
)
_MAX_PRIMARY_ATTEMPTS = 2

_UNTRUSTED_TOOL_TAG = re.compile(
    r"<\s*/?\s*untrusted_tool_result[^>]*>", re.IGNORECASE
)


def _strip_untrusted_tags(text: str) -> str:
    """Remove forged boundary tags until nested syntax reaches a fixed point.

    Used only on the fingerprinting path (pattern extraction), never on the
    prompt-rendering path: changing what this function returns changes every
    fingerprint computed from its output, silently re-partitioning pattern
    history. Prompt-facing text is neutralized separately by
    ``_escape_foreign_tags``, which cannot change a fingerprint because
    fingerprinting never calls it.
    """
    previous = None
    while previous != text:
        previous = text
        text = _UNTRUSTED_TOOL_TAG.sub("", text)
    return text


def _escape_foreign_tags(text: str) -> str:
    """Neutralize every tag-like construct before text reaches the model.

    ``_strip_untrusted_tags`` only recognizes spellings of the plugin's own
    boundary tag. A tool result can carry any other tag — ``<system>``,
    ``<instruction>``, a zero-width-obfuscated variant of the boundary itself —
    and models routinely privilege an inner tag over the surrounding context.
    Escaping every ``<``/``>`` removes the ambiguity entirely: nothing inside
    the escaped text can be parsed as a tag, forged or genuine, while the
    literal boundary tags added by the caller (outside this function's input)
    remain real markup.
    """
    return text.replace("<", "&lt;").replace(">", "&gt;")


_RECORD_SEPARATOR = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+")
# The forms that name something an instruction could act on. Naming one of
# these is what turns a durable note from a statement into an operation, so both
# durable-context paths refuse them.
_RESOURCE_TARGET_FORMS = r"""
    (?: [a-z]+:// )                         # any URL scheme
    | (?: ~[/\\] | (?<![\w.])[/\\][\w.] )   # absolute or home-relative path
    | (?: [A-Za-z]: (?=\S) )                    # absolute or drive-relative Windows path
    # Environment expansion. The name must start like an identifier: ``$5`` is a
    # price in prose, not a variable, and a memory that says "the run cost $5"
    # references nothing. ``$1`` as a positional parameter only means anything
    # inside a script, which a memory body is not.
    | (?: \$\{?[A-Za-z_]\w* | %\w+% )       # environment expansion
"""
# Bare shell metacharacters. A prompt note is a single "When <condition>,
# <allowlisted action>." line, so none of these has any role in one and the
# character class is the right test there. A memory body is Markdown prose,
# where they are ordinary punctuation -- see ``_memory_resource_error``.
_SHELL_METACHARACTERS = r"[`|;&><$]"
_RESOURCE_TARGET = re.compile("(?ix)" + _RESOURCE_TARGET_FORMS)
_RESOURCE_REFERENCE = re.compile(
    "(?ix)" + _RESOURCE_TARGET_FORMS + "|" + _SHELL_METACHARACTERS
)
_RESOURCE_NETWORK_OR_SHELL = re.compile(r"(?ix)(?:[a-z]+://|[`|;&><$])")
# Forms that can only be a network target. No prose can make these innocent.
_UNAMBIGUOUS_HOST_FORMS = r"""
    \b(?:localhost|intranet)\b                            # common bare hostnames
    | \b(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}\b # IPv4
    | \[(?:[0-9a-f]{0,4}:){2,7}[0-9a-f:]*\]              # bracketed IPv6
    | (?<![0-9a-f])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f:]*(?![0-9a-f:]) # bare IPv6
"""
_UNAMBIGUOUS_HOST = re.compile("(?ix)(?:" + _UNAMBIGUOUS_HOST_FORMS + ")")
# A dotted name, which is genuinely ambiguous: `SKILL.md` and `example.md` are the
# same shape, and no property of the token tells them apart. Label count does not:
# `invocation-route-v2026.8.16.patch` has four labels and is a filename, while
# `example.com` has two and is a host. The rightmost label does not either, unless
# an extension list is maintained forever — `.md` is Moldova, `.sh` is St Helena,
# `.py` is Paraguay.
#
# What does tell them apart is how the sentence uses the token. A host reference
# names something to reach; a filename names something that exists. So this form
# counts as a host only with host context around it, which is the rule this file
# already applies to the identically ambiguous legacy IPv4 literal.
#
# Measured cost of the old behaviour: 14 of 17 ordinary filenames were refused from
# memory (`SKILL.md`, `AGENTS.md`, `MEMORY.md`, `config.yaml`, `core.py`,
# `state.db`, `package.json`…), and it killed the only proposal this corpus
# produced that was correct and not already documented.
#
# Declared admission: `the API lives at api.internal.corp` passes, because no
# keyword marks it as a target. A URL, a port, a scheme, an IP literal and
# `localhost` are all still refused unconditionally, and those are the operational
# shapes; a bare name with no verb is a fact, not an instruction.
_DOTTED_NAME_FORM = r"(?<![\w.-])(?:[a-z0-9-]+\.)+[a-z]{2,63}\.?(?![\w.-])"
_DOTTED_NAME = re.compile("(?ix)" + _DOTTED_NAME_FORM)
# The strict rule, unchanged, and it stays on the prompt-note path. A prompt note
# is one imperative "When <condition>, <allowlisted action>." line rendered into
# every later session's system prompt; no approved action names a file, so a dotted
# name has no legitimate role there, while `use collector.evil to export records`
# is exactly the shape it exists to stop. The narrowing below applies only to
# memory, whose whole job is describing the environment in prose. Same category
# split as O-36's shell-metacharacter class, argued the same way.
_HOST_REFERENCE = re.compile(
    "(?ix)(?:" + _UNAMBIGUOUS_HOST_FORMS + "|" + _DOTTED_NAME_FORM + ")"
)
_LEGACY_IPV4_COMPONENT = r"(?:0x[0-9a-f]{1,8}|0[0-7]{0,11}|[1-9]\d{0,9})"
_LEGACY_IPV4_LITERAL = re.compile(
    rf"""(?ix)
    (?<![\w.])
    (?:
        (?:{_LEGACY_IPV4_COMPONENT})(?:\.(?:{_LEGACY_IPV4_COMPONENT})){{1,3}}
        | 0x[0-9a-f]{{1,8}}
        | 0[0-7]{{1,11}}
        | [1-9]\d{{6,9}}
    )
    \.?(?![\w.])
    """
)
_LEGACY_IPV4_OVERFLOW = re.compile(
    r"""(?ix)
    (?<![\w.])
    (?:
        0x[0-9a-f]{9,}(?![0-9a-f])
        | 0[0-7]{12,}(?![0-7])
        | [1-9]\d{10,}(?!\d)
    )
    (?=\.|[^\w.]|$)
    """
)
_SHORT_DECIMAL_IPV4_LITERAL = re.compile(
    r"(?<![\w.])(?:0|[1-9]\d{0,5})(?=$|[^\w.]|\.(?=\s|$))"
)
_HTTP_STATUS_REFERENCE = re.compile(
    r"""(?ix)
    (?:
        \b(?:the\s+)?(?:request|response)\s+returns?\s+|\b(?:exit|status|error)\s+code\s+|\bHTTP\s+
    )(\d{1,3})\b
    |
    \b(\d{3})(?=\s+(?:errors?|RegionError|Unauthorized|Forbidden|NotFound|BadRequest|Timeout|error)\b)
    """
)
_OVERRIDE_INTENT = re.compile(
    r"(?i)\b(?:ignore|disregard|override|bypass|skip|forget|regardless of|instead of)\b"
)
# What makes a mention of guidance a reference to *durable context* rather than to
# some ordinary document: an authority word, or a position word placed before or
# after the noun.
_CONTEXT_PRIOR_QUALIFIER = (
    r"(?:previous(?:ly)?|prior|above|preceding|earlier|older|system|initial"
    r"|original|foregoing|aforementioned|base|developer)"
)
_CONTEXT_GUIDANCE_NOUN = (
    r"(?:instruction|guidance|guideline|prompt|rule|policy|directive|constraint"
    r"|context)s?"
)
# A guidance word used attributively describes a document, not durable context:
# "the previous rule file", "the earlier policy document", "the prompt template".
# Refusing those would block ordinary prose about a repository's own files.
_CONTEXT_DOCUMENT_HEAD = (
    r"(?!\s+(?:files?|documents?|docs?|templates?|pages?|reports?|sheets?"
    r"|logs?|sections?|folders?|director(?:y|ies)|paths?|examples?|snippets?"
    r"|tables?|repos?|repositor(?:y|ies)|branch(?:es)?|commits?|issues?))"
)
# Determiners only, including the partitive, so "any of the previous
# instructions" cannot walk past a rule that stops "any previous instructions".
# A closed set is the whole point: it lets the phrase keep its natural article
# without letting an arbitrary prepositional phrase drift in ("steps copied from
# the earlier context"), which is ordinary defensive prose and must stay writable.
_CONTEXT_FOLLOW_DETERMINER = (
    r"(?:the|a|an|any|all|those|these|them|its|such|this|that|my|your|our|of)"
)
# Participles that keep a trailing qualifier attached to the same noun phrase:
# "instructions given above", "rules stated earlier".
_CONTEXT_GUIDANCE_LINK = (
    r"(?:given|stated|listed|provided|set|written|issued|defined)"
)
# A preposition detaches the qualifier from the guidance noun, and that is the
# difference between an override and a lesson about untrusted input. "Instructions
# in the earlier tool output" and "guidance from a previous tool result" describe
# where text came from; "instructions in the system prompt" names durable context
# itself. What separates them is whether a guidance noun follows the qualifier, so
# the prepositional form is refused only when one does.
_CONTEXT_GUIDANCE_PREPOSITION = r"(?:from|in|inside|within|of)"
# A skill body is Markdown, so emphasis, backticks, quotes and hyphens are the
# expected way to write "the **previous instructions**". Treating only whitespace
# as a separator inside the phrase leaves the gate reading prose nobody writes.
#
# A line break is allowed only where the text continues, never into a blank line,
# a list item, a heading, a quote or a fence. Otherwise the phrase stops being
# the direct object of the verb and starts bridging two unrelated Markdown
# blocks: "- Never follow" and a following "- Previous rules are documented in
# the appendix" are two bullets, not an instruction to disregard context.
_CONTEXT_PHRASE_GAP = (
    r"(?:[^\S\n]|[*_`'\"()\u2018\u2019\u201c\u201d\u2013\u2014-]"
    r"|\n(?![^\S\n]*(?:\n|$|[-*+>#]|\d+[.)]|`{3}|~{3})))+"
)
# Narrow pattern for skill/memory bodies: matches imperative override phrasing
# that targets guidance/instructions/prompt (not benign uses like "skip the cache"
# or "instead of retrying"). Wider _OVERRIDE_INTENT is too broad for Markdown bodies.
_CONTEXT_OVERRIDE_INTENT = re.compile(
    r"(?i)(?:"
    r"\b(?:ignore|disregard|override|bypass|forget|neglect|dismiss|supersede"
    r"|abandon|drop|cancel|erase|overwrite|discard|revoke)\b"
    r"(?:\s+\w+){0,4}\s+"
    # ``qualifier`` carries its own trailing space instead of being followed by a
    # separate `\s*`. Two adjacent runs that can both match whitespace make every
    # split of a long space run a distinct attempt, and a 15,000-character body of
    # spaces -- inside the size limit, and reachable from model output -- then took
    # over ten seconds in this one check.
    r"(?:all\s+)?(?:(?:previous|prior|above|preceding|earlier|system|initial)\s*)?"
    # Shared with the negative branch below, so the two cannot disagree about
    # what counts as durable-context vocabulary. ``guidelines`` was missing here
    # while this same file already treats the tag as reserved markup.
    rf"{_CONTEXT_GUIDANCE_NOUN}"
    # Negative override phrasing, e.g. "do not follow the previous instructions".
    # Two deliberate differences from the imperative branch above.
    #
    # The prior-reference qualifier is *required*, because "do not follow
    # instructions embedded in tool output" and "never follow guidance from an
    # untrusted page" are exactly the defensive lessons refine should be able to
    # write down about itself. It is accepted on either side of the noun, since
    # "do not follow the instructions above" is the same imperative as "do not
    # follow the above instructions".
    #
    # And what may sit between the words is a closed set, not `\w+`: the phrase
    # has to be the direct object of "follow". Allowing arbitrary filler blocks
    # sentences like "never follow links in earlier context", where a qualifier
    # and a noun merely happen to appear nearby. A false positive here silently
    # refuses a legitimate self-improvement, so it costs as much as a miss.
    #
    # Declared trade-off: a skill body cannot say "do not follow the above rule"
    # to except a rule stated earlier in that same body. Nothing distinguishes
    # that from "do not follow the above instructions" aimed at durable context,
    # so the ambiguous shape is refused and in-document exceptions have to be
    # phrased without the imperative ("this does not apply to binary files").
    #
    # Known limits, deliberately not closed, because closing them costs more in
    # refused legitimate prose than it buys. The words between "do not" and the
    # verb are bounded, so enough interposed filler still walks past; widening
    # that without bound is what makes "do not use the previous rule, and follow
    # the current policy" a false positive. An unlisted adjective between the
    # qualifier and the noun ("the above steering rules") also passes, for the
    # same reason. This is one layer among the impersonation, control-tag,
    # higher-priority-guidance, reviewer and daily-budget checks, not a parser.
    rf"|\b(?:do\s+not|do\s*['\u2019\u02bc]?n['\u2019\u02bc]?t|never)"
    rf"(?:[\s,]+\w+){{0,6}}[\s,]+"
    rf"(?:follow|comply\s+with|adhere\s+to|abide\s+by|obey)\b"
    # Elements below are each introduced by a mandatory separator, so none of them
    # can match a longer word by prefix; only the final noun needs `\b`.
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_FOLLOW_DETERMINER}){{0,3}}"
    rf"(?:"
    # Pre-posed: "the previous instructions", "prior system guidance",
    # "the previously-stated instructions", "the system's instructions".
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_PRIOR_QUALIFIER}(?:['\u2019]s)?){{1,3}}"
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_LINK})?"
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_FOLLOW_DETERMINER})?"
    rf"{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_NOUN}\b{_CONTEXT_DOCUMENT_HEAD}"
    # Post-posed, qualifier attached to the same noun: "the instructions above",
    # "rules stated earlier".
    rf"|{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_NOUN}"
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_LINK})?"
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_PRIOR_QUALIFIER}){{1,3}}\b"
    # Prepositional, and only when durable context is named on both sides:
    # "the instructions in the system prompt", not "instructions in the earlier
    # tool output".
    rf"|{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_NOUN}"
    rf"{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_PREPOSITION}"
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_FOLLOW_DETERMINER}){{0,2}}"
    rf"(?:{_CONTEXT_PHRASE_GAP}{_CONTEXT_PRIOR_QUALIFIER}){{1,3}}"
    rf"{_CONTEXT_PHRASE_GAP}{_CONTEXT_GUIDANCE_NOUN}\b{_CONTEXT_DOCUMENT_HEAD}"
    rf")"
    r")"
)
_CONTEXT_CONTROL_TAGS = re.compile(
    r"(?i)(?:<\s*/??\s*(?:system|instruction|tool_result|untrusted_tool_result|assistant_response|assistant|developer|user|user_context|prompt|rules|guidelines|context|custom_instructions)[^>]*>"
    r"|<<\s*sys\s*>>"
    r"|<\|(?:im_start|im_end|system|user|assistant|begin_of_text|start_header_id|end_header_id|eot_id|start_of_turn|end_of_turn)\|>"
    r"|\[\s*/?\s*INST\s*\]"
    r"|<<\s*/?\s*SYS\s*>>)"
)
_AGENT_IMPERSONATION = re.compile(
    r"(?i)(?:^|\n)\s*(?:(?:note|remember)\s*:\s*)?"
    r"(?:you\s+are\s+now|from\s+now\s+on\s+you\s+are"
    r"|your\s+new\s+(?:role|identity|instruction)\s+is"
    r"|system\s*:\s*(?:you|ignore|disregard|override|execute))"
)
_HIGHER_PRIORITY_GUIDANCE = re.compile(
    r"(?i)\b(?:developer|system|prompt|instruction|guidance|constraint|policy|rule|guardrail)\b"
)
_PROMPT_NOTE_FORMAT = re.compile(r"(?i)^when\s+[^,\n]{3,200},\s+\S")
_PROMPT_NOTE_CONDITION = re.compile(r"(?i)^when\s+([^,\n]{3,200}),\s+\S")
_PROMPT_NOTE_ACTION = re.compile(r"(?i)^when\s+[^,\n]{3,200},\s+(.+?)\s*$")
_PROMPT_NOTE_SAFE_TARGET = r"""
(?:(?:the|this|its|an?|expected|relevant|exact)\s+)+
(?:endpoint|parameters?|target|response|result|output|value|shape|error|failure|tests?|request|details?)
(?:\s+(?:and|or|the|this|its|expected|relevant|exact|endpoint|parameters?|target|response|result|output|value|shape|error|failure|tests?|request|details?))*
"""
# What a "verify X by checking Y" action may name as the thing consulted.
# Deliberately a closed noun list: an earlier revision ended this clause with
# ``.*``, which made the allowlist accept any trailing text on the line and
# turned a bounded policy form into free-form injected guidance.
_PROMPT_NOTE_SAFE_SOURCE = r"""
(?:the\s+)?(?:active|current|expected|configured|actual)?\s*
(?:
    hermes\s+(?:config|status|logs)
    | config|status|logs|output|response|result|value|model|provider
    | error|failure|tests?|parameters?|details?|endpoint|target
)
"""
_PROMPT_NOTE_SAFE_ACTION = re.compile(
    rf"""(?ix)
    (?:
        (?:check|confirm|inspect|verify)\s+(?:{_PROMPT_NOTE_SAFE_TARGET})(?:\s+before\s+(?:acting|continuing)|\s+by\s+(?:checking|inspecting|verifying|confirming|running)\s+(?:{_PROMPT_NOTE_SAFE_SOURCE})(?:\s+and\s+(?:checking|inspecting|verifying|confirming)\s+(?:{_PROMPT_NOTE_SAFE_SOURCE})){{0,2}})?
        | confirm\s+it(?:\s+before\s+acting)?
        | confirm\s+it['’]s\s+clear,\s+concise,\s+and\s+accurate
        | avoid\s+(?:unsupported\s+claims|speculation|unnecessary\s+changes)
        | ask\s+(?:for\s+clarification|(?:a|one)\s+focused\s+question
          | what\s+(?:the\s+)?(?:correct|right|intended)\s+
            (?:command|input|path|value)(?:\s+(?:is|was))?)
          (?:\s+instead\s+of\s+(?:retrying|guessing|assuming|proceeding|continuing)
            (?:\s+(?:the\s+|this\s+|that\s+|a\s+)?(?:same\s+|exact\s+|correct\s+|right\s+)?
              (?:command|request|call|step|proposal|action)|\s+it)?)?
        | follow\s+(?:the\s+)?(?:old|current|existing|established)\s+(?:policy|guidance)
        | keep\s+(?:the\s+)?(?:response|result|scope|change|policy)\s+(?:narrow|concise|minimal|focused)
        | log\s+(?:the\s+)?(?:error|failure|outcome)
        | mention\s+(?:the\s+)?(?:limitation|uncertainty|assumption)(?:\s+plainly)?
        | prefer\s+(?:unified|concise|clear|minimal)\s+(?:format|response|summary)
        | redact\s+(?:credentials?|secrets?|sensitive\s+(?:data|values?)|api[_-]?key(?:\s*=\s*["']?\[REDACTED\]["']?)?)
        | reject\s+(?:it|the\s+(?:invalid\s+)?(?:target|request|response|result))
        | retry\s+(?:the\s+|this\s+)?(?:request|proposal)(?:\s+with\s+(?:(?:a\s+|an\s+)?(?:different|alternative|new)\s+\w+|'[^']+'\s+instead\s+of\s+'[^']+'))?[.]?
        | summarize\s+(?:the\s+)?(?:common\s+cause|error|failure|result|outcome)
        | use\s+the\s+(?:supplied|provided|exact)\s+(?:spelling|name|format)
        | wait\s+for\s+(?:clarification|confirmation|approval|input)
        | (?:always\s+)?(?:include|provide|supply|set|pass)\s+(?:both\s+|all\s+)?(?:the\s+)?(?:required\s+)?(?:missing\s+)?(?:fields?|arguments?|parameters?|values?|keys?)
        | (?:always\s+)?(?:include|provide|supply|set|pass)\s+(?:both\s+|all\s+)?(?:the\s+)?(?:required\s+)?['"\u2018\u2019][a-z_]{{1,30}}['"\u2018\u2019](?:\s*(?:,|and|or)\s*['"\u2018\u2019][a-z_]{{1,30}}['"\u2018\u2019])*\s+(?:fields?|arguments?|parameters?|values?|keys?)
        | (?:always\s+)?include\s+both\s+path\s+and\s+content\s+fields?
        | (?:always\s+)?include\s+both\s+required\s+fields?\s*:\s*path\s+and\s+content
        | ask\s+before\s+retrying(?:\s+(?:a|the)\s+third\s+time)?
        | stop\s+(?:retrying\s+)?and\s+ask\s+(?:
            for\s+(?:the\s+)?(?:correct\s+|right\s+|intended\s+)?(?:command|clarification|input|help)
          | what\s+(?:the\s+)?(?:correct|right|intended)\s+
            (?:command|input|path|value)(?:\s+(?:is|was))?
          )
          (?:\s+instead\s+of\s+(?:retrying|guessing|assuming|proceeding|continuing)
            (?:\s+(?:the\s+|this\s+|that\s+|a\s+)?(?:same\s+|exact\s+|correct\s+|right\s+)?
              (?:command|request|call|step|proposal|action)|\s+it)?)?
        | check\s+timing\s+assumptions\s+before\s+rerunning
        | mention\s+which\s+sections\s+were\s+skipped
    )\.?
    """
)

# A field-policy note may name the arguments a tool requires ("include both
# 'path' and 'content' fields"), but naming a credential-shaped field turns
# "supply the missing argument" into "put the password in the call". Such a note
# cannot exfiltrate on its own — URLs, hosts, paths and shell syntax are rejected
# above — yet it is persisted into the agent's own future system context, and both
# the field name and the condition originate in an untrusted trajectory. So the
# bounded identifier form stays, and credential words are kept out of it.
# Case-insensitive on purpose: the allowlist above is compiled with ``(?i)``, so it
# accepts ``'PASSWORD'`` exactly as it accepts ``'password'``. Extracting only
# lowercase names here would leave the guard bypassable by capitalization.
_PROMPT_NOTE_QUOTED_FIELD = re.compile(
    r"['\"\u2018\u2019]([A-Za-z_]{1,30})['\"\u2018\u2019]"
)
# Long enough to be unambiguous as a substring: ``session_id`` and ``x_csrf`` are
# credentials, ``designation`` is not caught by any of these.
_CREDENTIAL_FIELD_SUBSTRINGS = (
    "pass", "pwd", "secret", "token", "credential", "cred", "auth", "bearer",
    "cookie", "csrf", "xsrf", "hmac", "signature", "session", "private", "refresh",
    "nonce", "seed", "mnemonic", "digest", "recovery", "security", "twofactor",
    "backupcode",
)
# Too short to match as substrings (``sig`` is inside ``design``, ``pin`` inside
# ``pinned``), so these are compared against whole ``_``-separated parts.
_CREDENTIAL_FIELD_PARTS = frozenset({
    "sig", "pin", "otp", "otc", "totp", "mfa", "salt", "jwt", "pat",
})


def _prompt_note_credential_field(action: str) -> str:
    """Return the first credential-shaped field name an action names, if any.

    Every comparison also runs against the name with ``_`` removed, because the
    separators are free: without that, ``a_p_i_k_e_y`` and ``p_i_n`` walk straight
    past a list that stops ``api_key`` and ``pin``.
    """
    for raw in _PROMPT_NOTE_QUOTED_FIELD.findall(action):
        name = raw.lower()
        joined = name.replace("_", "")
        if any(word in name or word in joined for word in _CREDENTIAL_FIELD_SUBSTRINGS):
            return raw
        if ({joined} | set(name.split("_"))) & _CREDENTIAL_FIELD_PARTS:
            return raw
        # ``key`` on its own is an ordinary argument name; ``api_key``,
        # ``secret_key`` and ``accesskey`` are not.
        if ("key" in name or "key" in joined) and joined not in ("key", "keys"):
            return raw
    return ""


# One canonical action list drives both model guidance and validator anti-drift tests.
PROMPT_NOTE_ACTION_EXAMPLES = _llm.PROMPT_NOTE_ACTION_EXAMPLES



def _one_line(value: Any) -> str:
    """Normalize every Unicode line boundary before rendering one record."""
    return _RECORD_SEPARATOR.sub(" ", str(value)).strip()


def _has_host_context(text: str, match: re.Match) -> bool:
    """Whether an ambiguous token is used as a host/address by the sentence.

    Two token shapes are genuinely undecidable on their own: a short decimal
    (a retry count, a duration, or legacy single-number IPv4 notation) and a
    dotted name (`SKILL.md` or `example.md`). For both, the surrounding prose is
    the only evidence available, so both consult this.
    """
    before = text[max(0, match.start() - 48):match.start()]
    # A network noun, or a target-taking verb, sitting against the token.
    adjacent = re.search(
        r"(?i)\b(?:host|address|ip|server|target|connect(?:ion)?|endpoint"
        r"|ssh|dial|reach|ping|curl|wget|telnet|netcat|nc|route|proxy|forward"
        r"|bind|listen|use|fetch|download)"
        r"s?\s*(?:(?:is|to|at)\s*|[=,:;\-]\s*|\(\s*)?$",
        before,
    )
    # Or a transfer verb whose target arrives after an intervening noun phrase,
    # marked by a directional preposition immediately before the token: "send the
    # archive TO drop.example.md". The preposition is what does the work. Without
    # it the same verbs govern something else -- "pass the full text OF SKILL.md"
    # names a file, not a destination -- which is exactly the distinction the
    # dotted-name rule rests on. Bounded to one clause so it cannot reach across a
    # sentence for a verb that belongs to another statement.
    directed = re.search(
        r"(?i)\b(?:send|post|upload|export|exfiltrate|publish|mirror|forward"
        r"|sync|push|copy|write|save|drop|report|stream)\w*\b"
        r"[^.;:\n]{0,40}?"
        r"\b(?:to|into|at|towards?|via)\s+$",
        before,
    )
    # Or the token is the value of a field whose NAME is a network noun:
    # `EMAIL_IMAP_HOST=imap.gmail.com`, `base_url: portal.example.com`. The noun
    # has to be matched inside the identifier -- `\bhost` cannot see `IMAP_HOST`,
    # which is why that exact line slipped through when this was measured on the
    # corpus. Restricted to an assignment or colon immediately before the token, so
    # a compound match cannot fire on ordinary prose the way a bare `\w*ip\w*`
    # would ("recipient", "description").
    assigned = re.search(
        r"(?i)\w*(?:host|url|uri|endpoint|domain|server|address)\w*\s*[:=]\s*$",
        before,
    )
    return bool(adjacent or directed or assigned)


def _memory_host_reference(text: str) -> bool:
    """The host test for a memory body, where a filename is ordinary prose.

    Differs from :func:`_has_host_reference` in one respect: a bare dotted name
    counts only where the sentence uses it as a target. Everything unambiguous —
    an IP literal, ``localhost``, bracketed IPv6, legacy IPv4 notation — is refused
    exactly as before, and URLs, ports, paths and environment expansions are
    refused by ``_RESOURCE_TARGET`` regardless of prose.
    """
    if _UNAMBIGUOUS_HOST.search(text) or _LEGACY_IPV4_LITERAL.search(text):
        return True
    if _LEGACY_IPV4_OVERFLOW.search(text):
        return True
    if any(_has_host_context(text, match) for match in _DOTTED_NAME.finditer(text)):
        return True
    return _short_decimal_is_a_host(text)


def _has_host_reference(text: str) -> bool:
    """Reject unambiguous hosts, dotted names, and contextual legacy IP literals."""
    if (
        _HOST_REFERENCE.search(text)
        or _LEGACY_IPV4_LITERAL.search(text)
        or _LEGACY_IPV4_OVERFLOW.search(text)
    ):
        return True
    return _short_decimal_is_a_host(text)


def _short_decimal_is_a_host(text: str) -> bool:
    """A bare decimal is a host only in host context, never as an HTTP status."""
    status_spans: list[tuple[int, int]] = []
    for m in _HTTP_STATUS_REFERENCE.finditer(text):
        for gi in range(1, (m.lastindex or 0) + 1):
            if m.group(gi) is not None:
                status_spans.append(m.span(gi))
    return any(
        match.span() not in status_spans and _has_host_context(text, match)
        for match in _SHORT_DECIMAL_IPV4_LITERAL.finditer(text)
    )


# ── session identity ───────────────────────────────────────────────────────
# The host does not pass session_id to slash-command handlers (contract is
# fn(raw_args) -> str|None). But pre_llm_call and post_llm_call hooks do
# receive it every turn. This module remembers the last value seen, so that a
# manual /refine command running in the same process can resolve it.

_LAST_SESSION_ID = ""
_LAST_SESSION_LOCK = threading.Lock()
_AUTO_EVENTS: List[Dict[str, Any]] = []
_LAST_AUTO_EVENT_LOCK = threading.Lock()
_AUTO_EVENTS_MAX = 10
_PERSISTENCE_WARNING_BYTES = 100 * 1024 * 1024


def note_auto_event(code: str, message: str) -> None:
    """Remember bounded, scrubbed background events for /refine status."""
    event = {
        "code": _one_line(scrub_text(code))[:64],
        "message": _one_line(scrub_text(message))[:300],
        "ts": time.time(),
    }
    with _LAST_AUTO_EVENT_LOCK:
        _AUTO_EVENTS.append(event)
        del _AUTO_EVENTS[:-_AUTO_EVENTS_MAX]


def recent_auto_events() -> List[Dict[str, Any]]:
    with _LAST_AUTO_EVENT_LOCK:
        return [dict(event) for event in _AUTO_EVENTS]


def last_auto_event() -> Dict[str, Any]:
    events = recent_auto_events()
    return events[-1] if events else {}


def note_session_id(session_id: str) -> None:
    """Record the session id seen from a host hook. Thread-safe, one value."""
    global _LAST_SESSION_ID
    if not isinstance(session_id, str) or not session_id.strip():
        return
    clean = session_id.strip()
    # Reject anything that scrubbing would alter — it might be content, not an id.
    if scrub_text(clean) != clean or len(clean) > 128:
        return
    with _LAST_SESSION_LOCK:
        _LAST_SESSION_ID = clean


def _noted_session_id() -> str:
    with _LAST_SESSION_LOCK:
        return _LAST_SESSION_ID


def host_session_id() -> str:
    """Best-effort read of the host's current session id via ContextVar/env.

    Available in CLI and cron; returns "" in the gateway (which sets session_key,
    not session_id, into the context). Guarded: any failure → "".
    """
    try:
        from gateway.session_context import get_session_env
        value = get_session_env("HERMES_SESSION_ID", "")
        return value.strip() if isinstance(value, str) else ""
    except Exception:
        return ""


def resolve_session_id(explicit: str = "") -> Tuple[str, str]:
    """Resolve which session to analyse.

    Returns (session_id, how) where how ∈ {explicit, host_env, hook, unknown}.
    When unknown, the caller must refuse rather than guess.
    """
    if explicit and explicit.strip():
        return explicit.strip(), "explicit"
    env_id = host_session_id()
    if env_id:
        return env_id, "host_env"
    hook_id = _noted_session_id()
    if hook_id:
        return hook_id, "hook"
    return "", "unknown"


def scrub_proposal(proposal: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility wrapper for recursive shared sanitation."""
    return sanitize(proposal)


# ── trajectory collection ──────────────────────────────────────────────────


def _open_db() -> Optional[sqlite3.Connection]:
    path = config.state_db_path()
    if not path.is_file():
        return None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except Exception as exc:
        logger.warning("Cannot open state.db: %s", scrub_text(str(exc)))
        return None


def _get_session_source_status(session_id: str) -> Tuple[str, str]:
    """Return a scrubbed source plus ``ok``, ``missing``, or ``error``."""
    if not session_id:
        return "", "missing"
    connection = _open_db()
    if not connection:
        return "", "error"
    try:
        row = connection.execute(
            "SELECT source FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return "", "missing"
        return scrub_text(str(row["source"] or "")), "ok"
    except Exception as exc:
        logger.warning("Cannot read session source: %s", scrub_text(str(exc)))
        return "", "error"
    finally:
        connection.close()


def _get_session_source(session_id: str) -> str:
    """Compatibility wrapper for status and callers that only need the value."""
    return _get_session_source_status(session_id)[0]


def _capture_source_revision(session_id: str) -> Optional[frozenset]:
    """Capture an internal source-evidence revision token for a session.

    The token is the set of ``rowid`` values of the *active* current-session
    rows. Hermes rewind/rewrite archives or replaces those rows, so a stale
    row ceases to be active (either ``active`` flips off or the ``rowid`` is
    gone) while an ordinary append leaves every captured row active. Row
    identity is therefore a suitable revision marker for "was this evidence
    rewound?".

    The token is strictly internal: it is never sent to the LLM, journaled,
    echoed in a tool result, or included in a public evidence summary. It is
    consumed only by :func:`_source_revision_is_current` immediately before a
    mutation.

    Returns ``None`` on capture failure (unreadable/missing DB or query error),
    which the caller must treat as fail-closed. An empty session yields an
    empty frozenset (no rows to invalidate).
    """
    if not session_id:
        return None
    connection = _open_db()
    if not connection:
        return None
    try:
        rows = connection.execute(
            "SELECT rowid AS rowid FROM messages WHERE session_id = ? AND active = 1",
            (session_id,),
        ).fetchall()
        return frozenset(int(row["rowid"]) for row in rows)
    except Exception as exc:
        logger.warning("Cannot capture source revision token: %s", scrub_text(str(exc)))
        return None
    finally:
        connection.close()


def _source_revision_is_current(
    session_id: str, revision: Optional[frozenset]
) -> bool:
    """Return True only when every captured source row is still active.

    Re-opens the session DB through the same read-only path used for evidence
    collection and verifies each captured ``rowid`` still belongs to this
    session and is still active. A missing row, a replaced row, or a row whose
    ``active`` flag flipped means the evidence was rewound/regenerated and the
    proposal is grounded in an abandoned branch.

    Fail-closed semantics:
    * ``revision`` is ``None`` (capture failed) -> False;
    * a query fails -> False;
    * any captured row is absent or inactive -> False.

    An ordinary append does not touch captured rows, so it returns True and the
    pass proceeds.
    """
    if revision is None:
        return False
    if not revision:
        # No source rows to validate — nothing can be rewound out from under us.
        return True
    connection = _open_db()
    if not connection:
        return False
    try:
        placeholders = ",".join("?" for _ in revision)
        rows = connection.execute(
            f"SELECT rowid AS rowid FROM messages "
            f"WHERE rowid IN ({placeholders}) AND session_id = ? AND active = 1",
            tuple(revision) + (session_id,),
        ).fetchall()
        # Every captured row must still be active in the same session.
        return {int(row["rowid"]) for row in rows} == revision
    except Exception as exc:
        logger.warning("Cannot verify source revision token: %s", scrub_text(str(exc)))
        return False
    finally:
        connection.close()


# A tool may say what its own exit code means. ``terminal`` answers an empty grep
# with ``exit_code: 1`` and ``exit_code_meaning: "No matches found (not an error)"``,
# and a non-zero exit is then not a failure. Measured on the snapshot: 19 of 19
# results carrying that self-declaration were counted as failures -- 100% -- and they
# produced 9 distinct fingerprints of which 2 tripped the >=2 repeat gate. Same
# family as O-32's web_search false positive, from a field that states the answer.
_BENIGN_EXIT_MEANING = re.compile(
    r"""(?ix)
    ["']? exit_code_meaning ["']? \s* [:=] \s* ["'] [^"']* \bnot \s+ an \s+ error \b
    """
)
# One marker set, named once so the heuristic has a single definition.
#
# The prefix class deliberately does NOT admit a quote. Admitting one lets the
# marker match inside a JSON string, which sounds like an improvement -- a
# traceback does arrive as ``"output": "Traceback…"`` -- and was measured on 6,775
# real tool results to reclassify 107 of them, of which 52 were plain false
# positives: 28 successful ``read_file`` results and 24 successful ``search_files``
# results whose *returned content* merely contains the word "error" or "failed".
# A marker inside data the tool returned says nothing about whether the call
# worked, and a bogus repeated failure competes with real ones for the single
# proposal a run makes. That is the shape of the 398-hit pattern that once
# swallowed every real failure, so the narrow class stays.
_ERROR_MARKER = re.compile(
    r"""(?ix)
    (?: ^ | [\s\[\{(,:;] )
    (?: traceback | error\b | failed\b | failure\b | file\s+not\s+found\b
      | no\s+such\s+file\b | cannot\s+find\s+the\s+(?:file|path)\b | ENOENT\b
      | timed?\s*out\b | timeout\b )
    """
)
# Status values that state a failure in the tool's own vocabulary. Used only to
# revoke a benign-exit declaration, never to classify on their own.
_FAILING_STATUS = frozenset({"error", "failed", "failure", "timeout", "cancelled"})
# Where a tool says what happened, as opposed to what it fetched. A payload with
# one of these keeps being read heuristically; a payload without one is returning
# data, and data is not evidence about the call that returned it.
_OUTPUT_CHANNELS = frozenset({"output", "stdout", "stderr"})


def _strip_non_error_declarations(text: str) -> str:
    """Remove the phrases whose own wording would trip the error markers."""
    text = re.sub(r"(?i)[\"\']?error[\"\']?\s*:\s*(?:null|\"\"|\'\')", "", text)
    return _BENIGN_EXIT_MEANING.sub("", text)


def _leading_json_object(content: str) -> Optional[Dict[str, Any]]:
    """The payload object when host annotations follow it, else None."""
    text = (content or "").strip()
    if not text.startswith("{"):
        return None
    closing = text.rfind("}")
    if closing <= 0:
        return None
    try:
        value = json.loads(text[: closing + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _structured_error_status(content: str) -> Optional[bool]:
    """Return a definitive structured status, or None when text is unstructured."""
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # The host appends annotations after the payload — a pagination hint, a
        # loop warning, or an entire discovered AGENTS.md — and strict parsing
        # rejects the whole result, so the structured rules below never ran and
        # the text heuristic decided instead, on prose the tool did not produce.
        #
        # Measured: 404 of the 409 results strict parsing rejects have a parseable
        # leading object, and 115 of those were counted as failures while their own
        # payload stated no failure (110 `search_files`, 5 `read_file`) — 110
        # fingerprints, 3 tripping the >=2 gate. The annotation is metadata about
        # the result, never the result's own verdict, so the leading object decides.
        value = _leading_json_object(content)
    # Read from the raw text, not the parsed dict: a tool result often carries an
    # appended loop warning that defeats json.loads, and the declaration must be
    # honoured on that path too.
    #
    # The declaration is believed only when nothing STRUCTURED contradicts it. It
    # originates in untrusted tool output, and the non-zero exit it neutralises may
    # have been the only thing carrying a genuine error, so a truthy ``error``, a
    # false ``success``/``ok``, or a failing ``status`` revokes it.
    #
    # Deliberately structural, not textual. Revoking on free-text markers instead
    # requires the marker to be visible inside a JSON string, and widening the
    # prefix class for that was measured to add 52 false positives on real data
    # (see ``_ERROR_MARKER``). Declared limit: a tool that calls its exit benign
    # while burying a traceback inside a payload string is not detected. That
    # combination does not occur in the measured corpus, and the alternative costs
    # more than it buys.
    benign_exit = bool(_BENIGN_EXIT_MEANING.search(content or ""))
    if benign_exit and isinstance(value, dict):
        status = str(value.get("status") or "").strip().lower()
        if (
            value.get("error") not in (None, "", False, [], {})
            or value.get("success") is False
            or value.get("ok") is False
            or status in _FAILING_STATUS
        ):
            benign_exit = False
    if isinstance(value, dict):
        exit_values = [
            value[key]
            for key in ("exit_code", "returncode", "return_code")
            if key in value
            and isinstance(value[key], (int, float))
            and not isinstance(value[key], bool)
        ]
        # Only the exit-code signal is neutralised. A truthy ``error``,
        # ``success: false`` or error text in the output still decide below, so a
        # tool cannot mute a real failure by describing its exit code.
        if not benign_exit and any(code != 0 for code in exit_values):
            return True
        error = value.get("error")
        if error not in (None, "", False, [], {}):
            return True
        if value.get("success") is False or value.get("ok") is False:
            return True
        if exit_values and all(code == 0 for code in exit_values):
            return False
        if value.get("success") is True or value.get("ok") is True:
            return False
        # A payload that reports no failure AND carries no output channel is a
        # tool returning DATA, not a tool reporting an outcome. Its data is not
        # evidence about the call: `read_file` on a config that mentions "error",
        # or `search_files` matching a line containing "failed", is a success.
        #
        # Measured on 6,775 real tool results: 355 were classified as failures
        # while their own payload stated no failure at all -- 293 `read_file`, 34
        # `search_files` -- producing 325 distinct fingerprints of which 26 tripped
        # the >=2 repeat gate. A run makes one proposal, so 26 bogus repeated
        # failures compete with the real ones for it. O-32 found one such pattern
        # from web_search; this is the same defect two orders of magnitude larger.
        #
        # The discrimination is which field the marker sits in. `output`, `stdout`
        # and `stderr` are where a tool says what happened, so a payload carrying
        # one still falls through to the heuristic — `execute_code` reporting
        # `status: success` with an HTTP 400 in its output stays a failure.
        if not (_OUTPUT_CHANNELS & value.keys()):
            return False

    codes = [
        int(match)
        for match in re.findall(
            r"(?i)(?:\bexit[_ ]?code\b|\breturncode\b)\s*[:=]?\s*(-?\d+)",
            content,
        )
    ]
    if not benign_exit and any(code != 0 for code in codes):
        return True
    # An explicit exit-code 0 is evidence of success, but it must not mask
    # textual error markers (Traceback, Error:, etc.) that appear alongside it.
    # Return None so _is_error_content falls through to the heuristic.
    return None


def _is_error_content(content: str) -> bool:
    """Classify structured status first, then bounded head/tail error text."""
    if not content:
        return False
    
    structured = _structured_error_status(content)
    if structured is not None:
        return structured
    
    # Only sample for heuristic if the content is large
    sample = (
        content
        if len(content) <= 4000
        else content[:1000] + "\n…\n" + content[-3000:]
    )
    
    # Strip the phrases whose own wording would otherwise trip the markers: a
    # quoted ``error: null``, and the tool's ``not an error`` declaration. Only
    # those are removed, so markers elsewhere in the output still count -- that is
    # what stops an untrusted tool muting its own failure.
    sample = _strip_non_error_declarations(sample)
    
    # Narrow, known success forms that must NOT be classified as errors
    # These are complete runner summaries, not prose that happens to include "failed"
    if (
        re.search(
            r"(?i)^\s*\d+\s+passed,\s*0\s+failed(?:\s+in\s+\S+)?\s*$",
            sample,
        )
        or re.search(
            r"(?is)^\s*ran\s+\d+\s+tests?\s+in\s+[^\n]+\n\s*ok\s*$",
            sample,
        )
        or (  # Canonical Cargo success: "test result: ok ... 0 failed ..."
            re.search(
                r"(?is)^\s*test\s+result:\s*ok.*?0\s+failed.*$",
                sample,
            )
        )
    ):
        return False
    
    # For Jest/Vitest, only include if we have a canonical form (e.g., "0 tests  passed")
    # We could add more Jest-specific patterns if we have examples
    
    # The rest of the function remains unchanged to catch real errors
    return bool(_ERROR_MARKER.search(sample))

def _is_correction(
    content: str, *, has_prior_assistant_response: bool = False
) -> bool:
    """Recognize explicit agent corrections, not routine instructions."""
    if len(content.strip()) < 12:
        return False
    text = re.sub(r"\s+", " ", content.strip().lower())
    unambiguous = (
        r"\b(?:that(?:'s| is) (?:wrong|not right)|you (?:are|were) wrong|wrong answer|incorrect)\b",
        r"\b(?:неправильно|ти помилив|ви помилили)\b",
        r"\bце не так(?:\s*[:;,.]\s*|\s+)(?:перероби|виправ|зміни|використай|замість)\b",
        r"\b(?:you used|ти використав|ви використали)\b.{0,120}\b(?:use|instead|замість)\b",
    )
    if any(re.search(pattern, text) for pattern in unambiguous):
        return True
    prospective_scope = re.search(
        r"\b(?:for|on|in)\s+(?:(?:this|the|a|an|your|our)\s+)?"
        r"(?:new|next|future|upcoming|another)\s+"
        r"(?:tasks?|requests?|turns?|exercises?|conversations?|"
        r"answers?|responses?|replies?|files?|documents?|templates?|projects?|configs?)\b|"
        r"\bfor\s+(?:all\s+)?future\s+(?:answers?|responses?|replies?)\b|"
        r"\b(?:going forward|from now on|next time)\b",
        text,
    )
    classification_text = text
    if prospective_scope:
        prefix = text[:prospective_scope.start()].rstrip()
        if not re.search(r"[.!?;:]$", prefix):
            return False
        classification_text = prefix
    if re.search(
        r"^(?:no|ні|нет)[,;:]\s+.{0,100}"
        r"\b(?:wrong|not right|не так|неправильно|instead|замість)\b",
        classification_text,
    ):
        return True
    if not has_prior_assistant_response:
        return False
    correction_lead = re.search(
        r"^(?:(?:no|ні|нет)[,;:]|(?:please\s+)?"
        r"(?:replace|revise|redo|rewrite|reformat|change|fix|correct)\b)",
        classification_text,
    )
    corrective_action = re.search(
        r"\b(?:replace|revise|redo|rewrite|reformat|change|fix|correct|use)\b",
        classification_text,
    )
    prior_output_reference = re.search(
        r"\b(?:previous|prior|old|earlier|last)\b.{0,30}"
        r"\b(?:answer|response|reply|format)\b|"
        r"\b(?:your|that)\s+(?:answer|response|reply)\b",
        classification_text,
    )
    return bool(correction_lead and corrective_action and prior_output_reference)


def count_session_messages(
    session_id: Optional[str] = None, *, limit: int
) -> Dict[str, Any]:
    """Count up to ``limit`` active rows without reading trajectory payloads.

    Session-end only needs to know whether the minimum-message gate is reached.
    Selecting content there duplicated the real refine pass, exposed private rows
    to needless processing, and let an unrelated reviewer threshold make the
    preflight arbitrarily large. This query stays on the shared read-only DB path
    and preserves the same distinguishable collection failures as evidence.
    """
    result: Dict[str, Any] = {
        "count": 0,
        "session_id": "",
        "session_id_source": "unknown",
        "collection_status": "session_unknown",
        "collection_error": "",
    }
    resolved, how = resolve_session_id(session_id or "")
    result["session_id"] = resolved
    result["session_id_source"] = how
    if not resolved:
        return result
    db_path = config.state_db_path()
    if not db_path.is_file():
        result["collection_status"] = "db_absent"
        return result
    connection = _open_db()
    if not connection:
        result["collection_status"] = "db_unavailable"
        return result
    try:
        sql = (
            "SELECT COUNT(*) AS n FROM ("
            "SELECT 1 FROM messages m "
            "LEFT JOIN sessions s ON s.id = m.session_id "
            "WHERE m.session_id = ? AND m.active = 1"
        )
        params: List[Any] = [resolved]
        skipped_sources = config.skip_session_sources()
        if skipped_sources:
            placeholders = ",".join("?" for _ in skipped_sources)
            sql += f" AND (s.source IS NULL OR LOWER(s.source) NOT IN ({placeholders}))"
            params.extend(skipped_sources)
        sql += " LIMIT ?)"
        params.append(max(1, int(limit)))
        row = connection.execute(sql, tuple(params)).fetchone()
        result["count"] = int(row["n"] or 0) if row else 0
        result["collection_status"] = "ok"
        return result
    except Exception as exc:
        safe_error = scrub_text(str(exc))
        logger.warning("Current-session count query failed: %s", safe_error)
        result["collection_status"] = "query_error"
        result["collection_error"] = safe_error[:300]
        return result
    finally:
        connection.close()


def collect_evidence(session_id: Optional[str] = None, limit: int = 60) -> Dict[str, Any]:
    empty = {
        "messages": [],
        "error_count": 0,
        "tool_errors": [],
        "error_patterns": [],
        "user_corrections": [],
        "session_id": "",
        "session_id_source": "unknown",
        "collection_status": "session_unknown",
        "collection_error": "",
    }
    resolved, how = resolve_session_id(session_id or "")
    if not resolved:
        empty["session_id_source"] = how
        return empty
    db_path = config.state_db_path()
    if not db_path.is_file():
        empty["session_id"] = resolved
        empty["session_id_source"] = how
        empty["collection_status"] = "db_absent"
        return empty
    connection = _open_db()
    if not connection:
        empty["session_id"] = resolved
        empty["session_id_source"] = how
        empty["collection_status"] = "db_unavailable"
        return empty
    try:
        sql = (
            "SELECT m.rowid AS message_order, m.role, m.content, m.tool_name, "
            "m.timestamp FROM messages m "
            "LEFT JOIN sessions s ON s.id = m.session_id "
            "WHERE m.session_id = ? AND m.active = 1"
        )
        params: List[Any] = [resolved]
        skipped_sources = config.skip_session_sources()
        if skipped_sources:
            placeholders = ",".join("?" for _ in skipped_sources)
            sql += f" AND (s.source IS NULL OR LOWER(s.source) NOT IN ({placeholders}))"
            params.extend(skipped_sources)
        sql += " ORDER BY m.timestamp DESC, m.rowid DESC LIMIT ?"
        params.append(limit)
        rows = connection.execute(sql, tuple(params)).fetchall()
        chronological_rows = list(reversed(rows))
        previous_was_assistant_response = False
        if chronological_rows:
            oldest = chronological_rows[0]
            predecessor = connection.execute(
                "SELECT role, CASE WHEN TRIM(COALESCE(content, '')) <> '' "
                "THEN 1 ELSE 0 END AS has_content FROM messages "
                "WHERE session_id = ? AND active = 1 "
                "AND role IN ('user', 'assistant') "
                "AND (timestamp < ? OR (timestamp = ? AND rowid < ?)) "
                "ORDER BY timestamp DESC, rowid DESC LIMIT 1",
                (
                    resolved,
                    oldest["timestamp"],
                    oldest["timestamp"],
                    oldest["message_order"],
                ),
            ).fetchone()
            if predecessor:
                predecessor_role = _one_line(
                    scrub_text(str(predecessor["role"] or ""))
                )[:32].lower()
                previous_was_assistant_response = bool(
                    predecessor_role == "assistant" and predecessor["has_content"]
                )
        # Failure counting reads the session; the excerpt above renders it.
        #
        # These were one query, so `limit` bounded both, and a repeated failure
        # outside the newest 60 rows was invisible. Measured on the snapshot: 40 of
        # 69 repeated-failure groups (58%) had their first failure before the
        # window opened, and it tracks session length — sessions over 300 rows had
        # 1 group visible and 19 not. Long sessions are exactly where a failure
        # gets repeated eight or nineteen times. The `skill_manage` case failed 14
        # times in a 1015-row session and arrived as `errors=1, patterns=1`.
        #
        # `cross_session_max_rows` already declares itself as the row budget for a
        # pass, so it governs here too rather than a second number being invented.
        # Only `role='tool'` rows are read, and `extract_patterns` still returns at
        # most `FORMAT_PATTERNS_LIMIT` patterns, so the prompt does not grow — only
        # the counts stop being wrong.
        failure_sql = (
            "SELECT m.content, m.tool_name, m.timestamp FROM messages m "
            "LEFT JOIN sessions s ON s.id = m.session_id "
            "WHERE m.session_id = ? AND m.active = 1 AND m.role = 'tool'"
        )
        failure_params: List[Any] = [resolved]
        if skipped_sources:
            placeholders = ",".join("?" for _ in skipped_sources)
            failure_sql += (
                f" AND (s.source IS NULL OR LOWER(s.source) NOT IN ({placeholders}))"
            )
            failure_params.extend(skipped_sources)
        failure_sql += " ORDER BY m.timestamp DESC, m.rowid DESC LIMIT ?"
        failure_params.append(max(int(limit), config.cross_session_max_rows()))
        failure_rows = list(
            reversed(connection.execute(failure_sql, tuple(failure_params)).fetchall())
        )

        messages: List[Dict[str, Any]] = []
        tool_errors: List[Dict[str, Any]] = []
        corrections: List[Dict[str, Any]] = []
        error_items: List[Dict[str, Any]] = []
        for row in failure_rows:
            content = scrub_text(str(row["content"] or ""))
            if not _is_error_content(content):
                continue
            tool_name = _one_line(scrub_text(str(row["tool_name"] or "")))[:120]
            bounded = (
                content
                if len(content) <= 4000
                else content[:1000] + "\n…\n" + content[-3000:]
            )
            pattern_content = _strip_untrusted_tags(bounded)
            tool_errors.append({"tool": tool_name, "snippet": pattern_content[:300]})
            error_items.append({
                "tool": tool_name,
                "content": pattern_content,
                "session_id": resolved,
                "ts": row["timestamp"] or 0,
            })

        for row in chronological_rows:
            # Every string from SQLite is scrubbed at this single extraction
            # boundary so evidence, journals, and returned tool results inherit it.
            role = _one_line(scrub_text(str(row["role"] or "")))[:32].lower()
            if role not in {"user", "assistant", "tool", "system"}:
                role = "unknown"
            content = scrub_text(str(row["content"] or ""))
            tool_name = _one_line(
                scrub_text(str(row["tool_name"] or ""))
            )[:120]
            shown = content[:400] + ("…" if len(content) > 400 else "")
            messages.append({"role": role, "content": shown, "tool_name": tool_name})
            # Tool failures are collected above, over the whole session; collecting
            # them here as well would count every windowed failure twice.
            if role == "user" and _is_correction(
                content,
                has_prior_assistant_response=previous_was_assistant_response,
            ):
                corrections.append({"snippet": content[:300]})
            if role == "assistant":
                previous_was_assistant_response = bool(content.strip())
            elif role == "user":
                previous_was_assistant_response = False
        return {
            "messages": messages[-limit:],
            "error_count": len(tool_errors),
            "tool_errors": tool_errors[-10:],
            "error_patterns": patterns.extract_patterns(error_items),
            "user_corrections": corrections[-5:],
            "session_id": resolved,
            "session_id_source": how,
            "collection_status": "ok",
            "collection_error": "",
        }
    except Exception as exc:
        safe_error = scrub_text(str(exc))
        logger.warning("Current-session evidence query failed: %s", safe_error)
        empty["session_id"] = resolved
        empty["session_id_source"] = how
        empty["collection_status"] = "query_error"
        empty["collection_error"] = safe_error[:300]
        return empty
    finally:
        connection.close()


def collect_cross_session_patterns(
    days: Optional[int] = None,
    max_rows: Optional[int] = -1,
    *,
    since_ts: Optional[float] = None,
    max_sessions: Optional[int] = None,
    strict: bool = False,
) -> List[Dict[str, Any]]:
    if not config.cross_session_enabled():
        if strict:
            raise IOError("Cross-session pattern collection is disabled")
        return []
    connection = _open_db()
    if not connection:
        if strict:
            raise IOError("Cross-session database is unavailable")
        return []
    if max_rows == -1:
        max_rows = config.cross_session_max_rows()
    since = (
        since_ts
        if since_ts is not None
        else time.time() - ((days or config.cross_session_days()) * 86400)
    )
    sql = (
        "SELECT m.session_id, m.tool_name, m.content, m.timestamp FROM messages m "
        "LEFT JOIN sessions s ON s.id = m.session_id "
        "WHERE m.role = 'tool' AND m.active = 1 AND m.timestamp >= ?"
    )
    params: List[Any] = [since]
    skipped_sources = config.skip_session_sources()
    if skipped_sources:
        placeholders = ",".join("?" for _ in skipped_sources)
        sql += f" AND (s.source IS NULL OR LOWER(s.source) NOT IN ({placeholders}))"
        params.extend(skipped_sources)
    sql += " ORDER BY m.timestamp DESC"
    if max_rows is not None:
        sql += " LIMIT ?"
        params.append(max_rows)
    try:
        cursor = connection.execute(sql, tuple(params))
        session_cap = (
            config.cross_session_max_sessions()
            if max_sessions is None and since_ts is None
            else max_sessions
        )
        seen: set = set()
        rows_seen = 0

        def iter_items():
            nonlocal rows_seen
            for row in cursor:
                rows_seen += 1
                sid = scrub_text(str(row["session_id"] or ""))
                if sid and sid not in seen:
                    if session_cap is not None and len(seen) >= session_cap:
                        continue
                    seen.add(sid)
                content = scrub_text(str(row["content"] or ""))
                if not _is_error_content(content):
                    continue
                bounded = (
                    content
                    if len(content) <= 4000
                    else content[:1000] + "\n…\n" + content[-3000:]
                )
                yield {
                    "tool": _one_line(
                        scrub_text(str(row["tool_name"] or ""))
                    )[:120],
                    "content": _strip_untrusted_tags(bounded),
                    "session_id": sid,
                    "ts": row["timestamp"] or 0,
                }

        # Signal gating must see every observed pattern. Prompt construction applies
        # FORMAT_PATTERNS_LIMIT separately at its rendering boundary, so truncating
        # here cannot hide a qualifying lower-ranked failure from has_signal().
        result = patterns.extract_patterns(iter_items(), limit=None)
        if max_rows is not None and rows_seen >= max_rows:
            logger.warning(
                "Cross-session row limit reached (%d); interactive evidence may be truncated",
                max_rows,
            )
        return result
    except Exception as exc:
        safe_error = scrub_text(str(exc))
        logger.warning("Cross-session query failed: %s", safe_error)
        if strict:
            raise IOError(f"Cross-session query failed: {safe_error}") from exc
        return []
    finally:
        connection.close()


# ── host context ───────────────────────────────────────────────────────────


def _skill_items() -> List[Any]:
    """Read the host's one skill listing without opening individual skills."""
    try:
        from tools.skills_tool import skills_list

        raw = skills_list()
        result = raw if not isinstance(raw, str) else json.loads(raw)
        skills = result.get("skills", []) if isinstance(result, dict) else result
        return skills if isinstance(skills, list) else []
    except Exception as exc:
        logger.warning("Cannot retrieve skill items: %s", scrub_text(str(exc)))
        return []


def list_skill_names() -> List[str]:
    names: List[str] = []
    for item in _skill_items():
        raw_name = item.get("name", "") if isinstance(item, dict) else item
        name = scrub_text(str(raw_name)).strip()
        if name:
            names.append(name)
    return names


def list_skill_entries() -> List[Dict[str, Any]]:
    """Return safe host metadata with a local version when the ledger knows it."""
    try:
        stats = ledger.load_stats()
    except Exception:
        stats = {}
    entries: List[Dict[str, Any]] = []
    for item in _skill_items():
        raw_name = item.get("name", "") if isinstance(item, dict) else item
        name = scrub_text(str(raw_name)).strip()
        if not name:
            continue
        entry: Dict[str, Any] = {
            "name": name,
            "description": scrub_text(str(item.get("description", ""))).strip()
            if isinstance(item, dict)
            else "",
            "category": scrub_text(str(item.get("category", ""))).strip()
            if isinstance(item, dict)
            else "",
        }
        metadata = stats.get(name) if isinstance(stats, dict) else None
        if isinstance(metadata, dict):
            try:
                version = int(metadata.get("version", 0) or 0)
            except (TypeError, ValueError):
                version = 0
            if version >= 1:
                entry["version"] = version
        entries.append(entry)
    return entries


def list_memory_snippets() -> List[str]:
    try:
        from tools.memory_tool import MemoryStore

        store = MemoryStore()
        store.load_from_disk()
        return [
            scrub_text(str(entry))[:120]
            for entry in (store.memory_entries + store.user_entries)[-20:]
        ]
    except Exception as exc:
        logger.warning("Cannot read memory snippets: %s", scrub_text(str(exc)))
        return []


def _unused_skills_safe() -> List[str]:
    try:
        return ledger.unused_skills()
    except Exception as exc:
        logger.debug("Cannot compute unused skills: %s", exc)
        return []


def _reconcile_pending() -> List[Dict[str, Any]]:
    """Reconcile durable approval states and mirror transitions to the ledger."""
    changed = journal.reconcile()
    for entry in changed:
        try:
            ledger.record_journal_state(entry)
        except Exception as exc:
            logger.warning("Cannot mirror reconciled state in ledger: %s", scrub_text(str(exc)))
    return changed


def auto_cooldown_remaining_minutes() -> float:
    """Minutes left on the automatic-attempt cooldown; ``0.0`` when elapsed.

    Single owner of this arithmetic so the hook gate and the status report can
    never disagree about whether the cooldown has passed.
    """
    last_attempt = journal.last_attempt_ts()
    if last_attempt is None:
        return 0.0
    remaining = config.auto_cooldown_minutes() * 60 - (time.time() - last_attempt)
    return remaining / 60 if remaining > 0 else 0.0


_JOURNAL_DIR_STATE_TEXT = {
    "ok": "usable",
    "missing_creatable": "does not exist yet, will be created on first write",
    "not_a_directory": "path exists but is not a directory",
    "unwritable": "not writable",
    "unknown": "could not be inspected",
}


def _journal_dir_state(directory: Path) -> str:
    """Classify the journal directory without creating or writing anything.

    ``missing_creatable`` walks up to the nearest existing ancestor, because a
    configured path several levels deep is still creatable on first use.
    """
    try:
        if directory.is_dir():
            return "ok" if os.access(str(directory), os.W_OK) else "unwritable"
        if directory.exists():
            return "not_a_directory"
        for ancestor in directory.parents:
            if not ancestor.exists():
                continue
            if not ancestor.is_dir():
                return "not_a_directory"
            return (
                "missing_creatable"
                if os.access(str(ancestor), os.W_OK)
                else "unwritable"
            )
        return "unwritable"
    except Exception:
        return "unknown"


def _persistence_snapshot(directory: Path) -> Dict[str, Any]:
    """Inspect plugin runtime storage without creating, mutating, or pruning it."""
    def file_metric(path: Path) -> Dict[str, Any]:
        try:
            if not path.exists():
                return {"state": "absent", "bytes": 0}
            if not path.is_file():
                return {"state": "unknown", "bytes": None}
            return {"state": "ok", "bytes": path.stat().st_size}
        except Exception:
            return {"state": "unknown", "bytes": None}

    journal_metric = file_metric(journal.journal_read_path())
    journal_metric.update({"physical_lines": 0, "logical_entries": 0})
    if journal_metric["state"] == "ok":
        try:
            with journal.journal_read_path().open("r", encoding="utf-8", errors="replace") as handle:
                journal_metric["physical_lines"] = sum(1 for line in handle if line.strip())
            loaded, state = journal._load_entries_safe()
            journal_metric["state"] = state
            journal_metric["logical_entries"] = len(loaded) if state == "ok" else None
        except Exception:
            journal_metric["state"] = "unknown"
            journal_metric["logical_entries"] = None

    backup_metric: Dict[str, Any] = {"state": "absent", "count": 0, "bytes": 0}
    backup_dir = directory / "backups"
    try:
        if backup_dir.exists() and not backup_dir.is_dir():
            backup_metric = {"state": "unknown", "count": None, "bytes": None}
        elif backup_dir.is_dir():
            files = [path for path in backup_dir.iterdir() if path.is_file()]
            backup_metric = {
                "state": "ok",
                "count": len(files),
                "bytes": sum(path.stat().st_size for path in files),
            }
    except Exception:
        backup_metric = {"state": "unknown", "count": None, "bytes": None}

    ledger_metric = file_metric(ledger.stats_read_path())
    if ledger_metric["state"] == "ok":
        try:
            ledger.load_stats()
            ledger_metric["readable"] = True
        except IOError:
            ledger_metric.update({"state": "unreadable", "readable": False})
    else:
        ledger_metric["readable"] = ledger_metric["state"] == "absent"

    prompt_metric = file_metric(journal.prompt_notes_read_path())
    if prompt_metric["state"] == "ok":
        notes = journal.load_prompt_notes()
        prompt_metric["readable"] = notes is not None
        if notes is None:
            prompt_metric.update({"state": "unreadable", "not_injected_count": None})
        else:
            prompt_metric["not_injected_count"] = sum(
                1
                for note in notes
                if _stored_prompt_note_content_error(note["content"])
            )
    else:
        prompt_metric["readable"] = prompt_metric["state"] == "absent"
        prompt_metric["not_injected_count"] = 0 if prompt_metric["readable"] else None
    metrics = (journal_metric, backup_metric, ledger_metric, prompt_metric)
    total_complete = all(isinstance(metric.get("bytes"), int) for metric in metrics)
    byte_values = [
        metric.get("bytes")
        for metric in metrics
        if isinstance(metric.get("bytes"), int)
    ]
    total_bytes = sum(byte_values)
    return {
        "journal": journal_metric,
        "backups": backup_metric,
        "ledger": ledger_metric,
        "prompt_notes": prompt_metric,
        "total_bytes": total_bytes,
        "total_bytes_complete": total_complete,
        "total_bytes_is_lower_bound": not total_complete,
        "warning_threshold_bytes": _PERSISTENCE_WARNING_BYTES,
        "over_warning_threshold": total_bytes >= _PERSISTENCE_WARNING_BYTES,
    }


def refine_status() -> Dict[str, Any]:
    """Report why automatic refinement will or will not run.

    Strictly read-only: it creates no directory, writes no journal record,
    consumes no daily budget, and never calls a model. It also does not
    reconcile pending approvals, so an unresolved staged edit still counts
    toward the budget it reports.
    """
    config_readable = config.config_available()
    auto = config.auto_enabled()
    interval = config.auto_turn_interval()
    max_edits = config.max_edits_per_day()
    jdir = config.journal_dir()
    jdir_state = _journal_dir_state(jdir)
    migration = journal.migration_status()
    persistence = _persistence_snapshot(jdir)

    # The effective model belongs in this report. A pinned model that no provider
    # serves turns every pass into an ordinary no_op, and without it here the
    # report would answer "blockers: none" while nothing can possibly succeed.
    try:
        target = config.effective_llm_target()
    except Exception:
        # "unknown", not "host_default": a config key or override file may still
        # pin something, and this report must not claim a resolution it failed to
        # perform.
        target = {
            "provider": "", "model": "", "source": "unknown",
            "issues": ["the effective model could not be resolved"],
        }
    try:
        model_allowed = config.llm_allow_model_override()
        provider_allowed = config.llm_allow_provider_override()
    except Exception:
        model_allowed = provider_allowed = False

    # Read journal-derived numbers only when a journal actually exists, so a
    # mistyped journal_dir is reported rather than silently created.
    journal_present = False
    journal_readable = True
    edits_today = 0
    last_ts: Optional[float] = None
    cooldown_remaining = 0.0
    last_model_substituted = False
    try:
        journal_path = journal.journal_read_path()
        journal_present = journal_path.is_file()
        if journal_present:
            entries, state = journal._load_entries_safe()
            if state != "ok":
                raise IOError(f"journal state is {state}")
            edits_today = journal.count_today_applied()
            last_ts = journal.last_attempt_ts()
            cooldown_remaining = auto_cooldown_remaining_minutes()
            # Surface whether the most recent refine pass ran on a substituted
            # model, so status does not imply a clean no_op when the reviewer
            # verdict was produced by a model other than the configured target.
            last_model_substituted = False
            for entry in reversed(entries or []):
                meta = entry.get("llm_meta") if isinstance(entry, dict) else None
                if isinstance(meta, dict) and meta.get("model_substituted"):
                    last_model_substituted = True
                    break
                if isinstance(meta, dict) and meta.get("reported_model"):
                    break
    except Exception as exc:
        journal_readable = False
        last_model_substituted = False
        logger.warning("Cannot read refine journal for status: %s", scrub_text(str(exc)))

    blockers: List[Dict[str, str]] = []
    if not config_readable:
        blockers.append({
            "code": "config_unreadable",
            "message": (
                "Hermes config could not be read, so automatic refinement stays "
                "off rather than overriding a setting that cannot be confirmed"
            ),
        })
    elif not auto:
        blockers.append({
            "code": "auto_disabled",
            "message": "Automatic refinement is disabled in the config",
        })
    if edits_today >= max_edits:
        blockers.append({
            "code": "budget_exhausted",
            "message": f"Daily edit budget is used up ({edits_today}/{max_edits})",
        })
    cooldown_shown = round(cooldown_remaining, 1)
    if cooldown_remaining > 0:
        blockers.append({
            "code": "cooldown_active",
            # Reuse the rounded value the report prints, so the blocker and the
            # cooldown line can never contradict each other.
            "message": f"Cooldown still active ({cooldown_shown} min left)",
        })
    if jdir_state in ("unwritable", "not_a_directory"):
        blockers.append({
            "code": "journal_dir_unusable",
            "message": (
                "Journal directory is not usable "
                f"({_JOURNAL_DIR_STATE_TEXT.get(jdir_state, jdir_state)})"
            ),
        })
    if not journal_readable:
        blockers.append({
            "code": "journal_unreadable",
            "message": "The journal exists but could not be read",
        })

    warnings: List[Dict[str, str]] = []
    if not persistence["total_bytes_complete"]:
        warnings.append({
            "code": "persistence_size_unknown",
            "message": (
                "One or more runtime stores could not be sized; the displayed "
                "storage value is only a lower bound"
            ),
        })
    if persistence["over_warning_threshold"]:
        warnings.append({
            "code": "persistence_growth",
            "message": (
                "Refine runtime data uses "
                f"{persistence['total_bytes']} bytes, above the read-only status "
                f"warning threshold of {persistence['warning_threshold_bytes']} bytes"
            ),
        })
    for store_name in ("ledger", "prompt_notes"):
        if persistence[store_name].get("state") == "unreadable":
            warnings.append({
                "code": f"{store_name}_unreadable",
                "message": f"The refine {store_name.replace('_', '-')} store is unreadable",
            })
    invalid_prompt_notes = persistence["prompt_notes"].get("not_injected_count")
    if isinstance(invalid_prompt_notes, int) and invalid_prompt_notes:
        warnings.append({
            "code": "prompt_notes_invalid",
            "message": (
                f"{invalid_prompt_notes} stored prompt note(s) do not meet the current "
                "injection policy and will not be injected"
            ),
        })
    if migration.get("outcome") == "failed":
        warnings.append({
            "code": "journal_migration_failed",
            "message": (
                "Runtime-data migration failed; refine is using the intact legacy "
                f"store at {migration.get('active_dir') or jdir}"
            ),
        })
    if migration.get("rename_warning"):
        warnings.append({
            "code": "journal_migration_rename_failed",
            "message": "Runtime data migrated, but the legacy directory could not be renamed",
        })
    plugin_source_collision = False
    try:
        plugin_source_collision = (jdir / "plugin.yaml").is_file()
    except Exception as exc:
        logger.debug("Cannot inspect plugin source collision: %s", scrub_text(str(exc)))
    if plugin_source_collision:
        warnings.append({
            "code": "journal_dir_is_plugin_source",
            "message": (
                "Journal directory holds the plugin source; "
                "'hermes plugins install --force' would delete runtime data"
            ),
        })
    if not interval:
        warnings.append({
            "code": "turn_trigger_disabled",
            "message": (
                "Turn trigger is off (auto_turn_interval=0); the session-end "
                "fallback still runs"
            ),
        })
    if jdir_state == "unknown":
        warnings.append({
            "code": "journal_dir_unknown",
            "message": (
                "The journal directory could not be inspected, so this report "
                "cannot confirm refinement is able to run"
            ),
        })
    for subsystem in _host_write_approval_enabled():
        warnings.append({
            "code": f"{subsystem}_write_approval_enabled",
            "message": (
                f"Host {subsystem} write approval is on, so every {subsystem} write "
                "waits in the host's pending queue -- refine's and the agent's own. "
                f"Drain it with '/{subsystem} pending' or turn the setting off; refine "
                "expects it off"
            ),
        })
    target_issues = [str(item) for item in target.get("issues", []) if item]
    if target_issues:
        # A discarded value must not be visible only in a log line: the file or
        # config key still pins something while this report names another target.
        warnings.append({
            "code": "model_target_issue",
            "message": "; ".join(target_issues),
        })
    if target["source"] == "command":
        warnings.append({
            "code": "model_override_active",
            # Deliberately does not say the override pinned each field: when it
            # sets only one, the other comes from the config and survives
            # '/refine model auto'. Claiming otherwise would describe a state
            # this report did not verify.
            "message": (
                "A '/refine model' override is in force; the effective target is "
                f"{target['model'] or '(host default)'}"
                + (f" on provider {target['provider']}" if target["provider"] else "")
                + ". '/refine model auto' removes the override; any value also set "
                  "in plugins.entries.refine.llm stays in effect after that"
            ),
        })
    # A value the host will refuse is dropped before the call. Use the same
    # messages the run journals so status cannot drift from failure diagnostics.
    for field, message in config.llm_target_trust_denials(target).items():
        warnings.append({
            "code": f"{field}_override_trust_denied",
            "message": message,
        })

    # Session identity — what /refine would analyse if triggered now.
    sid, sid_source = resolve_session_id()
    session_message_count = 0
    if sid:
        try:
            conn = _open_db()
            if conn:
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) n FROM messages WHERE session_id=? AND active=1",
                        (sid,),
                    ).fetchone()
                    session_message_count = row["n"] if row else 0
                finally:
                    conn.close()
        except Exception as exc:
            logger.warning("Cannot read session message count: %s", scrub_text(str(exc)))
    if sid_source == "unknown":
        blockers.append({
            "code": "session_unknown",
            "message": (
                "Cannot identify the current session. Neither the host environment "
                "nor a recent hook provided a session id."
            ),
        })

    return {
        "config_readable": config_readable,
        "auto_enabled": auto,
        "auto_turn_interval": interval,
        "turn_trigger_enabled": bool(interval),
        "auto_min_messages": config.auto_min_messages(),
        "auto_cooldown_minutes": config.auto_cooldown_minutes(),
        "last_attempt_ts": last_ts,
        "cooldown_remaining_minutes": cooldown_shown,
        "edits_today": edits_today,
        "max_edits_per_day": max_edits,
        "journal_present": journal_present,
        "journal_readable": journal_readable,
        "journal_dir": str(jdir),
        "journal_dir_state": jdir_state,
        "journal_dir_state_text": _JOURNAL_DIR_STATE_TEXT.get(jdir_state, jdir_state),
        "journal_dir_is_plugin_source": plugin_source_collision,
        "persistence": persistence,
        "last_auto_event": last_auto_event(),
        "recent_auto_events": recent_auto_events(),
        "migration": migration,
        "migration_outcome": migration.get("outcome", "not_checked"),
        "session_id": sid,
        "session_id_source": sid_source,
        "session_message_count": session_message_count,
        "session_source": _get_session_source(sid) if sid else "",
        "skip_session_sources": config.skip_session_sources(),
        "llm_model": target["model"],
        "llm_provider": target["provider"],
        "llm_target_source": target["source"],
        "llm_target_issues": target_issues,
        "llm_model_allowed": model_allowed,
        "llm_provider_allowed": provider_allowed,
        "last_model_substituted": last_model_substituted,
        "blockers": blockers,
        "blocker_codes": [b["code"] for b in blockers],
        "warnings": warnings,
        "warning_codes": [w["code"] for w in warnings],
    }


def refine_audit() -> Dict[str, Any]:
    try:
        with journal.mutation_lock():
            _reconcile_pending()
            journal_entries = journal.entries()
    except Exception as exc:
        safe_error = scrub_text(str(exc))
        logger.error("Audit journal read failed: %s", safe_error)
        return {
            "success": False,
            "complete": False,
            "rows": [],
            "report": "Audit incomplete: the refine journal is unreadable; no conclusions were drawn.",
        }
    try:
        ledger_earliest = ledger.earliest_created_ts()
    except IOError as exc:
        safe_error = scrub_text(str(exc))
        logger.error("Audit ledger read failed: %s", safe_error)
        return {
            "success": False,
            "complete": False,
            "rows": [],
            "report": "Audit incomplete: the refine ledger is unreadable; no conclusions were drawn.",
        }

    journal_times = [
        float(entry.get("ts", 0))
        for entry in journal_entries
        if entry.get("outcome") == "applied"
        and isinstance(entry.get("proposal"), dict)
        and entry["proposal"].get("action") in ("create", "patch")
        and entry.get("ts")
    ]
    earliest_candidates = [value for value in [ledger_earliest, *journal_times] if value]
    earliest = min(earliest_candidates) if earliest_candidates else None

    complete = True
    current: Optional[List[Dict[str, Any]]] = []
    if earliest:
        try:
            current = collect_cross_session_patterns(
                since_ts=earliest,
                max_rows=None,
                max_sessions=None,
                strict=True,
            )
        except Exception as exc:
            logger.error("Audit pattern collection failed: %s", scrub_text(str(exc)))
            current = None
            complete = False

    # Pattern collection can be unbounded, so never hold the mutation lock over
    # it. Refresh afterward and capture every attribution input under one lock:
    # a concurrent refine then cannot publish a newer skill/journal generation
    # between the intended-content snapshot and the target baseline read.
    try:
        with journal.mutation_lock():
            journal_entries = journal.entries()
            stats_snapshot = ledger.load_stats()
            skill_baselines = ledger.snapshot_skill_baselines(journal_entries)
    except Exception as exc:
        safe_error = scrub_text(str(exc))
        logger.error("Audit attribution snapshot failed: %s", safe_error)
        return {
            "success": False,
            "complete": False,
            "rows": [],
            "report": (
                "Audit incomplete: refinement state could not be read "
                "consistently; no conclusions were drawn."
            ),
        }

    rows = ledger.audit(
        current,
        journal_entries=journal_entries,
        stats_snapshot=stats_snapshot,
        skill_baselines=skill_baselines,
    )
    report = ledger.format_audit(rows)
    if not complete:
        report = (
            "⚠ Audit incomplete: trajectory recurrence could not be measured; "
            "recurrence-dependent verdicts remain unknown.\n\n" + report
        )
    return {"success": True, "complete": complete, "rows": rows, "report": report}


# ── proposal validation and apply ──────────────────────────────────────────


def _skill_content_error(name: str, content: str) -> Optional[str]:
    if not content.startswith("---"):
        return "Skill content must start with YAML frontmatter"
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.S)
    if not match:
        return "Skill content has incomplete YAML frontmatter"
    frontmatter = match.group(1)
    name_match = re.search(r"(?m)^name\s*:\s*[\"']?([^\n\"']+)", frontmatter)
    if not name_match or name_match.group(1).strip() != name:
        return "Skill frontmatter name must exactly match the target name"
    if not re.search(r"(?m)^description\s*:\s*\S", frontmatter):
        return "Skill frontmatter requires a non-empty description"
    if not content[match.end():].strip():
        return "Skill content requires a Markdown body"
    return None


def _skill_or_memory_injection_error(content: str) -> Optional[str]:
    """Reject skill/memory content that could restructure future agent context.

    This is deliberately narrower than the prompt-note gate: skill bodies
    legitimately contain code, URLs, angle-bracket generics, and words like
    "skip" or "instead of". We reject only:
    1. Context-control tags that a model would parse as structural markup.
    2. Imperative override phrasing targeted at guidance/instructions.
    3. Agent-impersonation patterns.
    4. Unicode categories that can restructure rendering (Cc/Cf/Cs/Co/Cn),
       except newline/tab/carriage-return which are normal in Markdown.

    Compatibility normalization is inspection-only. Persisted content keeps its
    original bytes so validation cannot silently rewrite a skill or memory.
    """
    normalized = unicodedata.normalize("NFKC", content)
    if _CONTEXT_CONTROL_TAGS.search(content) or _CONTEXT_CONTROL_TAGS.search(normalized):
        return "Content contains context-control markup that could restructure agent context"
    if _CONTEXT_OVERRIDE_INTENT.search(content) or _CONTEXT_OVERRIDE_INTENT.search(normalized):
        return "Content contains imperative override phrasing targeting prior guidance"
    if _AGENT_IMPERSONATION.search(content) or _AGENT_IMPERSONATION.search(normalized):
        return "Content contains agent-impersonation or role-reassignment phrasing"
    for ch in content:
        if ch in ("\n", "\r", "\t"):
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            return "Content contains control or non-character codepoints"
    return None


def _resource_reference_kind(text: str) -> Optional[str]:
    """Classify a durable-context resource reference without rewriting it.

    NFKC is inspection-only: persisted memory and prompt-note bytes stay intact,
    while compatibility forms such as full-width URL punctuation cannot bypass the
    same resource policy applied to their ASCII equivalents.
    """
    inspected = unicodedata.normalize("NFKC", text)
    if not (_RESOURCE_REFERENCE.search(inspected) or _has_host_reference(inspected)):
        return None
    if _RESOURCE_NETWORK_OR_SHELL.search(inspected):
        return "network_or_shell"
    if _has_host_reference(inspected):
        return "host"
    return "path_or_environment"


def _memory_resource_error(content: str) -> Optional[str]:
    """Reject operational resources in memory, which is future behavioral context.

    Skills may legitimately document commands and URLs. A memory is injected as
    durable guidance instead, so a URL, host, path or environment expansion --
    a target the agent could act on -- has no safe operational role there.

    Bare shell metacharacters are deliberately not part of this test, unlike the
    prompt-note path that shares the target forms. A memory body is Markdown
    prose, and ``;``, ``&``, ``$``, ``<``, ``>`` and backticks all occur in
    ordinary English and ordinary Markdown; testing for the character rejected
    sentences that name no resource at all. Measured on a real run, that is what
    discarded the only useful lesson eleven real sessions produced -- on one
    prose semicolon, in a body whose subject was a missing argument.

    Dropping the character class costs no protection that this rule was for: a
    shell construct only becomes operational once it names a target, and every
    such target is still refused by the URL, host, path and environment clauses
    below. What it stops costing is the false positive on prose.

    NFKC is inspection-only: persisted memory bytes stay intact, while
    compatibility forms such as full-width URL punctuation cannot bypass the same
    policy applied to their ASCII equivalents.
    """
    inspected = unicodedata.normalize("NFKC", content)
    if _RESOURCE_TARGET.search(inspected) or _memory_host_reference(inspected):
        return (
            "Memory content cannot reference resources, hosts, URLs, paths, or "
            "environment variables"
        )
    return None


def _prompt_note_content_error(
    content: str, *, check_rendered_size: bool = True
) -> Optional[str]:
    """Keep globally injected notes narrow, declarative, and renderable as one block."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not journal.prompt_note_content_is_structurally_safe(content):
        return "Prompt note cannot contain markup or context-control characters"
    if not 1 <= len(lines) <= 2:
        return "Prompt note must contain one or two non-empty policy lines"
    if any(
        line.startswith(("-", "*", "#")) or re.match(r"^\d+[.)]\s", line)
        for line in lines
    ):
        return "Prompt note must be a policy, not a list or procedure"
    if not _PROMPT_NOTE_FORMAT.match(lines[0]):
        return "Prompt note must use 'When <specific condition>, <one action>.'"
    if len(lines) > 1 and not _PROMPT_NOTE_FORMAT.match(lines[1]):
        return "Every line of a prompt note must use 'When <specific condition>, <one action>.'"
    resource_kinds = {_resource_reference_kind(line) for line in lines}
    resource_kinds.discard(None)
    if resource_kinds:
        if "network_or_shell" in resource_kinds:
            return "Prompt note cannot reference URLs, commands, or shell syntax"
        if "host" in resource_kinds:
            return "Prompt note cannot reference hosts"
        return "Prompt note cannot reference file paths or environment variables"
    if any(_CONTEXT_OVERRIDE_INTENT.search(line) for line in lines):
        return "Prompt note cannot override prior guidance"
    for line in lines:
        condition_match = _PROMPT_NOTE_CONDITION.match(line)
        if not condition_match or _HIGHER_PRIORITY_GUIDANCE.search(condition_match.group(1)):
            return "Prompt note condition cannot refer to higher-priority guidance"
        action_match = _PROMPT_NOTE_ACTION.match(line)
        if not action_match or not _PROMPT_NOTE_SAFE_ACTION.fullmatch(action_match.group(1)):
            return "Prompt note action must match an approved behavioral policy"
        # The whole line, not just the action: the condition is free text up to
        # 200 characters, so "When the 'api_key' field is missing, include the
        # required fields." carries the same instruction one clause to the left.
        if _prompt_note_credential_field(line):
            return "Prompt note cannot name a credential field to supply"
    rendered = "Refine notes:\n- " + content
    per_note_limit = max(
        1, config.prompt_notes_max_chars() // config.prompt_notes_max_count()
    )
    if check_rendered_size and len(rendered) > per_note_limit:
        return (
            f"Prompt note is too large for its per-note rendered context budget ({len(rendered)} chars; max "
            f"{per_note_limit})"
        )
    return None


def _stored_prompt_note_content_error(content: Any) -> Optional[str]:
    """Return the semantic injection error for a structurally stored note."""
    safe_content = scrub_text(str(content)).strip()
    if not safe_content:
        return "Prompt note is empty after scrubbing"
    return _prompt_note_content_error(safe_content, check_rendered_size=False)


def _validate_proposal(proposal: Dict[str, Any]) -> Optional[str]:
    action = str(proposal.get("action", "no_op"))
    if action == "no_op":
        return None
    if action not in ("create", "patch"):
        return f"Unsupported action: {action}"
    kind = str(proposal.get("kind", ""))
    if kind not in ("skill", "memory", "prompt"):
        return f"Unsupported kind: {kind}"
    name = str(proposal.get("name", "")).strip()
    content = str(proposal.get("content", ""))
    if not content.strip():
        return f"{action.title()} requires non-empty content"
    if len(content) > _llm.MAX_CONTENT_CHARS:
        return f"Content too large ({len(content)} chars; max {_llm.MAX_CONTENT_CHARS})"
    if kind in ("skill", "memory"):
        injection_error = _skill_or_memory_injection_error(content)
        if injection_error:
            return injection_error
        if kind == "memory":
            resource_error = _memory_resource_error(content)
            if resource_error:
                return resource_error
    if kind == "prompt":
        if not config.prompt_notes_enabled():
            return "Prompt notes are disabled"
        if action != "create":
            return "Prompt notes support create only"
        content_error = _prompt_note_content_error(content)
        if content_error:
            return content_error
        scope = proposal.get("scope", "global")
        if scope not in ("global", "session"):
            return "Prompt-note scope must be global or session"
        if scope == "session" and not journal.normalize_prompt_note_session_id(
            proposal.get("session_id", "")
        ):
            return "Session-scoped prompt notes require a verified session ID"
        duplicate = journal.prompt_note_content_exists(content)
        if duplicate is None:
            return "Prompt-note store is unavailable"
        if duplicate:
            return "Identical active prompt note already exists"
    else:
        if not name:
            return "Proposal missing name"
    if kind == "skill":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
            return "Skill name must use lowercase letters, digits, hyphens, or underscores"
        format_error = _skill_content_error(name, content)
        if format_error:
            return format_error
        if name.startswith("hermes-"):
            return f"Skill '{name}' has reserved prefix"
        if action == "create" and name in list_skill_names():
            return f"Skill '{name}' already exists — use patch, not create"
        if action == "patch" and config.only_agent_created():
            try:
                from tools.skill_usage import is_agent_created

                if not is_agent_created(name):
                    return f"Skill '{name}' is bundled/hub-installed (denied by only_agent_created)"
            except ImportError:
                return "Cannot import skill_usage module"
    fingerprint = str(proposal.get("pattern_fingerprint", "") or "")
    if fingerprint and not re.fullmatch(r"[0-9a-f]{12}", fingerprint):
        return "pattern_fingerprint must be the complete 12-character fingerprint"
    if kind == "memory" and _memory_content_splits(str(proposal.get("content", ""))):
        # The host joins and splits entries on its delimiter, so content carrying
        # it is stored as one entry and read back as several. The target check
        # then fails for a write that did land: journaled as an error,
        # un-rollbackable, absent from the audit, and permanent in the prompt.
        return "Memory content cannot contain the host's entry delimiter"
    if journal.was_applied_recently(proposal, config.dedup_window_days()):
        return f"Identical edit already applied within {config.dedup_window_days()} day(s)"
    return None


def _preview_guardrail_error(proposal: Dict[str, Any]) -> Optional[str]:
    """The verdict the apply path would reach, for a run that applies nothing.

    The dry-run branch used to return before ``_validate_proposal`` ran at all, so
    a preview presented a proposal the apply would refuse and said nothing about
    it. That is not cosmetic. The Part-2 yield census was measured entirely
    through dry runs, which made every guardrail rejection invisible to it and
    counted proposals that could never land; the two rejections it did report had
    to be reconstructed afterwards by calling the validator by hand.

    Every check reachable from here reads state and never writes it, including the
    prompt-note store and recent-apply lookups, so running it on a dry run keeps
    the "applies nothing" promise. A transaction stops at its first failing edit,
    so the preview names that edit rather than summarising.

    One declared inaccuracy, in the transaction case only. ``_apply_edit`` checks
    each edit against live state, so edit 1 is judged after edit 0 has landed;
    nothing has landed during a preview, so an edit that depends on an earlier one
    in the same transaction (patching a skill the transaction itself creates)
    previews as rejected and would apply. Erring toward "would be rejected" is the
    right way round for a preview, and the shape the model is told to use --- a new
    skill plus the memory entry that records when to reach for it --- has no such
    dependency.
    """
    if str(proposal.get("action", "")) != "multi":
        return _validate_proposal(proposal)
    edits = [edit for edit in proposal.get("edits", []) if isinstance(edit, dict)]
    if not edits:
        return "Transaction contained no usable edit"
    for index, edit in enumerate(edits):
        error = _validate_proposal(edit)
        if error:
            return f"edit {index}: {error}"
    return None


def _memory_entry_delimiter() -> str:
    """The host's entry delimiter, read from the host rather than restated here.

    Two constants that must agree are a defect waiting to happen, so this asks the
    host and keeps a literal only as the fallback for a host that stops exporting
    it.
    """
    try:
        from tools.memory_tool import ENTRY_DELIMITER

        if isinstance(ENTRY_DELIMITER, str) and ENTRY_DELIMITER:
            return ENTRY_DELIMITER
    except Exception:
        logger.debug("Host exports no memory entry delimiter", exc_info=True)
    return "\n\u00a7\n"


def _memory_content_splits(content: str) -> bool:
    """Whether the host would read this content back as more than one entry.

    Checking for the delimiter alone is not enough: the host joins entries with
    it, so content whose own trailing edge completes the sequence once a
    neighbour is joined -- ``...\\n§`` -- splits just as surely while
    round-tripping clean on its own, which means no drift is reported either. A
    *leading* ``§\\n`` is safe, because splitting is greedy left to right and
    non-overlapping, so it survives the cycle intact. Rather than reason about
    which edge is which, this probes what the join/split cycle actually does.
    """
    delimiter = _memory_entry_delimiter()
    stripped = content.strip()
    if delimiter in stripped:
        return True
    # Probe the actual round trip with neighbours on both sides.
    joined = delimiter.join(["before", stripped, "after"])
    return joined.split(delimiter) != ["before", stripped, "after"]


def _host_write_approval_enabled() -> List[str]:
    """Host subsystems whose write approval gate is on, if the host has one.

    Refine expects the gate off, which is the host default. It cannot turn it off
    -- the setting is the host's -- so the only honest thing it can do is say so
    where the user already looks. A host with no gate at all reports nothing.
    """
    try:
        from tools import write_approval as wa
    except Exception:
        return []
    enabled = []
    for subsystem in ("skills", "memory"):
        try:
            if wa.write_approval_enabled(subsystem):
                enabled.append(subsystem)
        except Exception:
            logger.debug("Cannot read %s write approval state", subsystem, exc_info=True)
    return enabled


def _host_tool_result(raw: Any) -> Dict[str, Any]:
    """Normalize a host tool result, which is either a dict or a JSON string."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "error": str(raw)}
    # A JSON scalar or list is not a result; returning it would make every caller
    # that does ``.get("success")`` raise outside its own error handling.
    return parsed if isinstance(parsed, dict) else {"success": False, "error": str(raw)}


def _apply_skill(proposal: Dict[str, Any]) -> Dict[str, Any]:
    from tools.skill_manager_tool import skill_manage

    action = "edit" if proposal["action"] == "patch" else proposal["action"]
    raw = skill_manage(
        action=action,
        name=proposal["name"],
        content=proposal["content"],
        category=proposal.get("category") or None,
    )
    return _host_tool_result(raw)


def _apply_memory(proposal: Dict[str, Any]) -> Dict[str, Any]:
    # ``memory_tool`` is the entry point that owns the host's ``write_approval``
    # gate; ``MemoryStore.add`` performs the write with no gate at all. Calling
    # the store directly made refine the one writer that could never be staged,
    # so a host configured to require approval for memory was silently bypassed.
    from tools.memory_tool import MemoryStore, memory_tool

    # ``kind`` is constrained to skill/memory/prompt by REFINE_PROPOSAL_SCHEMA's
    # enum, so a proposal reaching this function is always kind="memory" and the
    # store target is always "memory". A "user" memory target existed only in
    # dead branches nothing could reach; removed rather than resurrected, since
    # there is no schema path that lets the model ever request it.
    target = "memory"
    if proposal.get("action") not in ("create", "patch"):
        return {"success": False, "error": f"Unknown memory action: {proposal.get('action')}"}
    store = MemoryStore()
    store.load_from_disk()
    # With the gate on and a foreground CLI callback registered, this call can
    # prompt the user inline while the refine pass holds the mutation lock, so an
    # unanswered prompt blocks other callers until their own lock wait expires.
    # The automatic path is unaffected: it takes the lock non-blockingly.
    #
    # No second save here. The host's ``add`` re-reads under its own file lock,
    # appends, and persists inside that lock. A save from out here would rewrite
    # the whole file without the lock and drop anything another session appended
    # in between -- which is exactly the drift the host guards against.
    return _host_tool_result(
        memory_tool(action="add", target=target, content=proposal["content"], store=store)
    )


def _apply_prompt_note(note: Dict[str, str]) -> Dict[str, Any]:
    """Persist a plugin-owned prompt note; no host write or approval is involved."""
    return journal.add_prompt_note(note)


def _skill_baseline_conflict(
    proposal: Dict[str, Any], observed_sha: str = ""
) -> Optional[str]:
    """Return a conflict message when the patch target no longer matches planning.

    Returns None (no conflict) only when baseline is a well-formed dict with
    exists=True and a valid 64-hex-char sha256 that matches the current state.

    Returns an error string when:
      - baseline is absent or not a dict (unsafe: patch was built without
        verifying the target content);
      - baseline has invalid structure (exists != True, or sha256 malformed);
      - the current state diverges from the planning baseline.
    """
    import re as _re

    baseline = proposal.get("refine_baseline")
    name = str(proposal.get("name", ""))
    if not isinstance(baseline, dict):
        return (
            f"Skill '{name}': patch requires a locally grounded refine_baseline "
            "(absent or not a dict)"
        )
    exists = baseline.get("exists")
    sha = str(baseline.get("sha256", ""))
    if exists is not True or not _re.fullmatch(r"[0-9a-f]{64}", sha):
        return (
            f"Skill '{name}': patch has malformed refine_baseline "
            f"(exists={exists!r}, sha256 valid={bool(_re.fullmatch(r'[0-9a-f]{{64}}', sha))})"
        )
    name = str(proposal.get("name", ""))
    if observed_sha:
        # Check B: compare against the sha from prepare_skill_recovery snapshot.
        if observed_sha != sha:
            return (
                f"Skill '{name}': entry changed during refinement planning "
                f"(baseline {sha[:12]}… vs current {observed_sha[:12]}…)"
            )
        return None
    # Check A: read current state from host before backup.
    current = journal.skill_baseline(name)
    if current is None:
        return (
            f"Skill '{name}': entry changed during refinement planning "
            "(cannot confirm target state)"
        )
    if not current.get("exists"):
        return (
            f"Skill '{name}': entry changed during refinement planning "
            "(target was deleted after planning)"
        )
    if current["sha256"] != sha:
        return (
            f"Skill '{name}': entry changed during refinement planning "
            f"(baseline {sha[:12]}… vs current {current['sha256'][:12]}…)"
        )
    return None


def _skill_patch_matches_baseline(proposal: Dict[str, Any]) -> bool:
    """Whether a verified patch would write the exact bytes it was planned from."""
    baseline = proposal.get("refine_baseline")
    return bool(
        isinstance(baseline, dict)
        and journal.content_digest(str(proposal.get("content", "")))
        == str(baseline.get("sha256", ""))
    )


def _journal_nonmutation(**kwargs: Any) -> Optional[str]:
    """Write a non-mutating journal entry. Accepts all journal.log kwargs including llm_meta."""
    try:
        return journal.log(**kwargs)
    except Exception as exc:
        logger.error("Cannot write refine journal: %s", scrub_text(str(exc)))
        return None


def _terminal_result(
    *,
    outcome: str,
    success: bool,
    message: str,
    trigger: str = "",
    safe_reason: str = "",
    session: str = "",
    proposal: Optional[Dict[str, Any]] = None,
    group: Optional[Dict[str, Any]] = None,
    llm_meta: Optional[Dict[str, Any]] = None,
    evidence: Optional[Dict[str, Any]] = None,
    error: str = "",
    failure: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a terminal refine result and enforce the no-silent-no_op invariant.

    The base invariant is *no failure may be indistinguishable from ``no_op``*.
    Hand-carrying that on every exit is what let a ``no_op`` carry raw evidence
    back to the model once; a new exit is a fresh chance to forget it. This
    constructor centralizes the shape so the invariant is structural, not
    discipline:

    * ``outcome`` is required — an exit with no outcome is a bug here, not a
      silent ``no_op``.
    * If a ``failure`` code is present it refuses to build ``outcome='no_op'``;
      a recognised failure maps to its distinct outcome instead.
    * When a journal entry is wanted (``trigger`` given) it is written through
      ``_journal_nonmutation`` and the id is attached, so the durable record and
      the returned result always agree.

    Returns the terminal dict; also attaches ``record_id``/``journal_id`` when a
    journal id was produced.
    """
    if not outcome:
        raise ValueError("_terminal_result requires an outcome")
    normal_or_failure = not failure
    if not normal_or_failure and outcome == "no_op":
        raise ValueError(
            f"refuse to build outcome='no_op' with failure='{failure}'"
        )
    entry_id: Optional[str] = None
    if trigger:
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or message,
            session_id=session,
            proposal=proposal or {"action": "no_op", "reason": message, "expected_outcome": ""},
            outcome=outcome,
            error=error or message,
            group=group,
            llm_meta=llm_meta,
        )
    base: Dict[str, Any] = {
        "success": success,
        "outcome": outcome,
        "message": message,
        "reversible": bool(success and outcome not in ("no_op",)),
    }
    if entry_id:
        base["journal_id"] = entry_id
        base["record_id"] = entry_id
    if proposal is not None:
        base["proposal"] = proposal
    if evidence is not None:
        base["evidence"] = evidence
    if llm_meta is not None:
        base["llm_meta"] = llm_meta
    if failure:
        base["failure"] = failure
    if extra:
        base.update(extra)
    return base


def record_evidence_failure(
    session_id: str,
    collection_status: str,
    collection_error: str = "",
    *,
    trigger: str = "auto",
    timeout: float = 30.0,
) -> Optional[str]:
    """Wait off the host callback, then durably record unavailable evidence."""
    safe_status = _one_line(scrub_text(collection_status))[:64] or "unknown"
    safe_error = _one_line(scrub_text(collection_error))[:300]
    message = f"Current-session evidence is unavailable ({safe_status})."
    try:
        with journal.mutation_lock(timeout=timeout):
            _, state = journal._load_entries_safe()
            if state == "unreadable":
                raise IOError("journal is unreadable")
            return journal.log(
                trigger=trigger,
                reason=message,
                session_id=session_id,
                proposal={
                    "action": "no_op",
                    "reason": message,
                    "expected_outcome": "",
                },
                outcome="evidence_unavailable",
                error=safe_error or message,
            )
    except Exception as exc:
        logger.error(
            "Cannot durably record evidence failure: %s", scrub_text(str(exc))
        )
        return None


def _reviewer_cooldown_elapsed() -> bool:
    """Keep reviewer calls independently rate-limited across processes."""
    last_review = journal.last_attempt_ts(trigger="reviewer")
    if last_review is None:
        return True
    return time.time() - last_review >= config.reviewer_cooldown_minutes() * 60


def _render_evidence_text(evidence: Dict[str, Any]) -> str:
    """Render collected messages into the prompt evidence block.

    Every role is untrusted control-text-in-waiting: tool metadata, assistant
    echoes of tool output, and user/system records all get the same
    plugin-owned boundary plus tag escaping, so no forged <system> or closing
    boundary can become structure during later truncation. Escaping happens
    only here, on the prompt-rendering path — never on the fingerprinting
    path — so pattern history is unaffected.
    """
    lines: List[str] = []
    for message in evidence.get("messages", []):
        role = _one_line(message["role"])[:32].lower()
        if role not in {"user", "assistant", "tool", "system"}:
            role = "unknown"
        content = _one_line(str(message["content"])[:400])
        if role == "tool":
            tool_name = _one_line(message.get("tool_name", ""))[:120]
            record = f"tool={tool_name or '?'} | {content}"
        else:
            record = content
        safe_record = _escape_foreign_tags(_strip_untrusted_tags(record))
        lines.append(
            f"[{role}] <untrusted_tool_result>{safe_record}</untrusted_tool_result>"
        )
    return "\n".join(lines)


def _model_substituted(
    requested_provider: str, requested_model: str,
    reported_provider: str, reported_model: str,
) -> bool:
    """Return True when the model that actually served differs from the model
    the plugin intended to use.

    A bound (invocation_bound) facade cannot transmit a provider/model, so the
    host may resolve the call onto the active conversation route or onto the
    fallback model. That is a *substitution* with respect to what the refine
    config pinned, and any verdict produced by a substituted model must not be
    trusted as if it came from the configured target.
    """
    if requested_model and reported_model and requested_model != reported_model:
        return True
    if requested_provider and reported_provider and requested_provider != reported_provider:
        return True
    return False


def _handle_no_signal(
    llm: Any,
    evidence: Dict[str, Any],
    evidence_text: str,
    session: str,
    trigger: str,
    safe_reason: str,
    min_pattern_count: int,
    run_target: Dict[str, str],
    run_target_source: str,
    run_target_issues: Any,
    run_target_unusable: bool,
    intended_target: Dict[str, str],
) -> Union[Dict[str, Any], Tuple[str, str]]:
    """Handle a gate-closed pass: reviewer fallback or journaled no_op.

    Returns either a response dict that the caller must return unchanged, or
    ("reviewer_instructions", "reviewer_approved") when the reviewer opened
    the gate and the primary proposal call should proceed.
    """
    _signal_path = "no_signal"
    should_review = (
        config.reviewer_fallback_enabled()
        and len(evidence.get("messages", [])) >= config.reviewer_min_messages()
        and _reviewer_cooldown_elapsed()
    )
    if should_review:
        reviewer = _llm.review_fallback(llm, evidence_text, target=run_target)
        reviewer_call_meta = _llm.last_call_meta()
        # Reviewer fallback is a single bounded call (schema fallback is
        # transport fallback, not a second reviewer attempt). Keep the
        # attempt telemetry consistent with primary proposal calls.
        reviewer_llm_meta = {
            "requested_provider": intended_target.get("provider", ""),
            "requested_model": intended_target.get("model", ""),
            "target_source": run_target_source,
            "primary_attempts": 1,
            **{k: v for k, v in reviewer_call_meta.items() if k in (
                "reported_provider", "reported_model", "latency_ms",
                "output_tokens", "output_mode"
            )},
        }
        reviewer_substituted = _model_substituted(
            intended_target.get("provider", ""), intended_target.get("model", ""),
            str(reviewer_call_meta.get("reported_provider", "")),
            str(reviewer_call_meta.get("reported_model", "")),
        )
        reviewer_llm_meta["model_substituted"] = reviewer_substituted
        if run_target_issues:
            reviewer_llm_meta["target_issues"] = run_target_issues
        rationale = scrub_text(str(reviewer.get("rationale", "")))
        decision = "approved" if reviewer.get("should_refine") else "declined"
        reviewer_reason = f"Reviewer {decision}: {rationale}"
        reviewer_failure = scrub_text(str(reviewer.get("failure", "")).strip())
        reviewer_target_issue = bool(
            not reviewer_failure
            and run_target_unusable
            and not reviewer.get("should_refine")
        )
        reviewer_outcome = (
            (
                "llm_incomplete"
                if reviewer_failure in {"malformed", "truncated", "no_final_text", "budget_exhausted"}
                else "llm_error"
            )
            if reviewer_failure
            else (
                "target_issue"
                if reviewer_target_issue
                else ("model_substituted" if reviewer_substituted else "no_op")
            )
        )
        reviewer_error = (
            (
                "The reviewer returned an incomplete or malformed verdict."
                if reviewer_failure in {"malformed", "truncated", "no_final_text", "budget_exhausted"}
                else (
                    "The host trust policy denied the reviewer model call."
                    if reviewer_failure == "llm_trust_denied"
                    else "The reviewer model call failed."
                )
            )
            if reviewer_failure
            else (
                "The configured refine model target is unusable."
                if reviewer_target_issue
                else (
                    "The reviewer ran on a model different from the configured "
                    "target; its 'no change' verdict is not trustworthy."
                    if reviewer_substituted
                    else ""
                )
            )
        )
        reviewer_entry_id = _journal_nonmutation(
            trigger="reviewer",
            reason=reviewer_reason,
            session_id=session,
            proposal={
                "action": "no_op",
                "reason": reviewer_reason,
                "expected_outcome": "",
            },
            outcome=reviewer_outcome,
            error=reviewer_error,
            llm_meta=reviewer_llm_meta,
        )
        if not reviewer_entry_id:
            return {
                "success": False,
                "message": "Reviewer decision could not be journaled.",
                "llm_called": True,
                "reviewer": decision,
                "evidence": evidence,
                "reversible": False,
            }
        if reviewer_failure:
            return {
                "success": False,
                "outcome": reviewer_outcome,
                "failure": reviewer_failure,
                "message": reviewer_error,
                "journal_id": reviewer_entry_id,
                "llm_called": True,
                "reviewer": "failed",
                "evidence": evidence,
                "llm_meta": reviewer_llm_meta,
                "reversible": False,
            }
        if not reviewer.get("should_refine"):
            proposal = {
                "action": "no_op",
                "reason": reviewer_reason,
                "expected_outcome": "",
            }
            if reviewer_target_issue:
                return {
                    "success": False,
                    "outcome": "target_issue",
                    "failure": "target_configuration",
                    "message": "The configured refine model target is unusable.",
                    "journal_id": reviewer_entry_id,
                    "proposal": proposal,
                    "llm_called": True,
                    "reviewer": "declined",
                    "evidence": evidence,
                    "llm_meta": reviewer_llm_meta,
                    "reversible": False,
                }
            if reviewer_substituted:
                return {
                    "success": False,
                    "outcome": "model_substituted",
                    "failure": "model_substituted",
                    "message": (
                        "The reviewer ran on a model different from the configured "
                        "target; its 'no change' verdict is not trustworthy and is "
                        "not recorded as a clean no_op."
                    ),
                    "journal_id": reviewer_entry_id,
                    "proposal": proposal,
                    "llm_called": True,
                    "reviewer": "declined",
                    "evidence": evidence,
                    "llm_meta": reviewer_llm_meta,
                    "reversible": False,
                }
            return {
                "success": True,
                "message": f"No actionable improvement found. {reviewer_reason}",
                "journal_id": reviewer_entry_id,
                "proposal": proposal,
                "llm_called": True,
                "reviewer": "declined",
                "evidence": evidence,
                "reversible": False,
            }
        reviewer_instructions = scrub_text(str(reviewer.get("instructions", "")))
        return reviewer_instructions, "reviewer_approved"
    else:
        proposal = {
            "action": "no_op",
            "reason": f"No repeated failure (min {min_pattern_count}x) and no explicit correction.",
            "expected_outcome": "",
        }
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or proposal["reason"],
            session_id=session,
            proposal=proposal,
            outcome="no_op",
        )
        if not entry_id:
            return {
                "success": False,
                "message": "No edit was needed, but the journal write failed.",
                "evidence": evidence,
            }
        return {
            "success": True,
            "message": f"No actionable improvement found. {proposal['reason']}",
            "journal_id": entry_id,
            "llm_called": False,
            "evidence": evidence,
            "reversible": False,
        }


def _refine_once(
    llm: Optional[PluginLlm],
    *,
    reason: str = "",
    session_id: Optional[str] = None,
    auto: bool = False,
    dry_run: bool = False,
    explicit_session: bool = False,
    session_ending: bool = False,
) -> Dict[str, Any]:
    trigger = "auto" if auto else "manual"
    started = time.time()
    safe_reason = scrub_text(reason)

    # Fail closed when journal is unreadable: without history the budget, dedup,
    # and context guards are all bypassed. Must be distinguishable from no_op.
    _, journal_state = journal._load_entries_safe()
    if journal_state == "unreadable":
        return _terminal_result(
            outcome="journal_unreadable",
            success=False,
            message="Journal could not be read; refine did not run to avoid bypassing budget limits.",
        )

    resolved_session, resolved_source = resolve_session_id(session_id or "")
    if not resolved_session:
        evidence = {
            "messages": [],
            "error_count": 0,
            "tool_errors": [],
            "error_patterns": [],
            "user_corrections": [],
            "session_id": "",
            "session_id_source": resolved_source,
        }
        return _terminal_result(
            outcome="session_unknown",
            success=False,
            message="Cannot identify the current session; refine did not run.",
            evidence=evidence,
        )

    # Resolve machine-generated sources before reading any private trajectory.
    session_db_source, source_lookup_status = _get_session_source_status(
        resolved_session
    )
    skip_sources = config.skip_session_sources()
    if session_db_source and session_db_source.lower() in skip_sources:
        proposal = {
            "action": "no_op",
            "reason": f"Session source '{session_db_source}' is configured to be skipped.",
            "expected_outcome": "",
        }
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or proposal["reason"],
            session_id=resolved_session,
            proposal=proposal,
            outcome="skipped_session_source",
        )
        response = {
            "success": bool(entry_id),
            "outcome": "skipped_session_source",
            "message": (
                f"Session source '{session_db_source}' is in skip_session_sources; "
                "refine did not run."
            ),
            "evidence": {
                "messages": [],
                "session_id": resolved_session,
                "session_id_source": resolved_source,
                "session_source": session_db_source,
                "source_lookup_status": source_lookup_status,
            },
            "reversible": False,
        }
        if entry_id:
            response["journal_id"] = entry_id
        else:
            response["message"] += " The skip decision could not be journaled."
        return response

    if llm is None:
        failure_message = (
            "No invocation-bound host LLM is available; refine did not send "
            "trajectory evidence. If this host lacks the invocation-route "
            "core patch, run install.sh from the plugin directory."
        )
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or failure_message,
            session_id=resolved_session,
            proposal={
                "action": "no_op",
                "reason": failure_message,
                "expected_outcome": "",
            },
            outcome="llm_invocation_unavailable",
            error=failure_message,
            llm_meta={"target_source": "invocation_bound", "primary_attempts": 0},
        )
        response = {
            "success": False,
            "outcome": "llm_invocation_unavailable",
            "failure": "llm_invocation_unavailable",
            "message": failure_message,
            "evidence": {
                "session_id": resolved_session,
                "session_id_source": resolved_source,
                "session_source": session_db_source,
                "source_lookup_status": source_lookup_status,
            },
            "reversible": False,
        }
        if entry_id:
            response["journal_id"] = entry_id
        return response

    if not dry_run and journal.daily_limit_reached():
        return {
            "success": False,
            "message": f"Daily edit limit reached ({config.max_edits_per_day()}). "
            f"Applied/pending/prepared today: {journal.count_today_applied()}.",
            "evidence": {
                "session_id": resolved_session,
                "session_id_source": resolved_source,
                "session_source": session_db_source,
                "source_lookup_status": source_lookup_status,
            },
        }

    # Resolve the LLM target once per pass so every unbound compatibility call
    # is deterministic and attributable. An invocation-bound facade already
    # carries the gateway's exact route, so persisted refine overrides must not
    # be expanded into provider/model kwargs.
    _invocation_bound = bool(getattr(llm, "invocation_bound", False))
    # The model the plugin *intended* to use. The plugin must ALWAYS run on the
    # CURRENT host route — the model actually active in the session — never a
    # stale config/live default. The facade exposes the current provider/model, so
    # it is the source of truth for attribution; config.effective_llm_target() is
    # only a fallback when the facade carries none. Keeping requested_* populated
    # lets a host resolution onto another route or the fallback model be flagged
    # as a substitution.
    _intended_target: Dict[str, str] = {"provider": "", "model": ""}
    try:
        _effective = config.effective_llm_target()
        _run_target: Dict[str, str] = {}
        if _invocation_bound:
            _run_target_source = "invocation_bound"
            _run_target_issues: List[str] = []
        else:
            _run_target_source = _effective.get("source", "host_default")
            # Only targets the user explicitly chose are sent to the host.  A "live"
            # target is the host's own current model; re-sending it converts an
            # implicit working resolution into an explicit one that can fail.
            if _run_target_source in ("command", "config"):
                if _effective.get("provider") and config.llm_allow_provider_override():
                    _run_target["provider"] = _effective["provider"]
                if _effective.get("model") and config.llm_allow_model_override():
                    _run_target["model"] = _effective["model"]
            _run_target_issues = [str(i) for i in _effective.get("issues", []) if i]
            _run_target_issues.extend(
                config.llm_target_trust_denials(_effective).values()
            )
        # Current model is authoritative: prefer the live facade route.
        _facade_provider = str(getattr(llm, "provider", "") or "")
        _facade_model = str(getattr(llm, "model", "") or "")
        if _facade_provider or _facade_model:
            _intended_target["provider"] = _facade_provider
            _intended_target["model"] = _facade_model
        elif isinstance(_effective, dict):
            _intended_target["provider"] = str(_effective.get("provider", "") or "")
            _intended_target["model"] = str(_effective.get("model", "") or "")
    except Exception:
        _run_target = {}
        _run_target_source = "invocation_bound" if _invocation_bound else "unknown"
        _run_target_issues = [] if _invocation_bound else ["the effective model could not be resolved"]
        _intended_target = {"provider": "", "model": ""}
    _run_target_unusable = bool(
        _run_target_issues
        and not _run_target
        and _run_target_source in ("unknown", "host_default", "command", "config")
    )

    # Trace emission at actual invocation boundary — uses only fields that host exposes
    if _trace is not None:
        _trace.emit_trace(
            _trace.build_trace(
                session_id=session_id,
                source="tool" if not auto else "autorun",
                operation="refine_run",
                route_state="invocation_bound" if _invocation_bound else "llm_invocation_unavailable",
                provider=getattr(llm, "provider", None),
                model=getattr(llm, "model", None),
                output_tokens=None,
            ),
            journal_append=None,
        )

    _min_signal_required = config.min_signal_required()
    _min_pattern_count = config.min_pattern_count()
    evidence_limit = 60
    if _min_signal_required and config.reviewer_fallback_enabled():
        evidence_limit = max(evidence_limit, config.reviewer_min_messages())
    evidence = collect_evidence(session_id=resolved_session, limit=evidence_limit)
    evidence["session_id_source"] = resolved_source
    evidence["session_source"] = session_db_source
    evidence["source_lookup_status"] = source_lookup_status
    session = resolved_session
    session_source = resolved_source
    collection_status = str(evidence.get("collection_status", "ok"))
    if collection_status != "ok":
        failure_message = (
            "Current-session evidence is unavailable "
            f"({collection_status}); refine did not infer an empty session."
        )
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or failure_message,
            session_id=session,
            proposal={
                "action": "no_op",
                "reason": failure_message,
                "expected_outcome": "",
            },
            outcome="evidence_unavailable",
            error=failure_message,
        )
        response = {
            "success": False,
            "outcome": "evidence_unavailable",
            "failure": collection_status,
            "message": failure_message,
            "evidence": evidence,
            "reversible": False,
        }
        if entry_id:
            response["journal_id"] = entry_id
        else:
            response["message"] += " The failure could not be journaled."
        return response
    if len(evidence.get("messages", [])) < 3:
        return {
            "success": True,
            "message": "Not enough messages in this session to analyze.",
            "evidence": evidence,
        }

    # Capture an internal source-evidence revision token from the exact active
    # current-session rows used for the proposal. Row identities are suitable
    # because a rewind/rewrite archives or replaces them, while an ordinary
    # append leaves them active. The token never leaves this function: it is
    # not sent to the LLM, journaled, echoed in a tool result, or included in
    # a public evidence summary. It is consumed only by the fail-closed check
    # immediately before any host mutation in _apply_edit/_apply_transaction.
    source_revision = _capture_source_revision(session)
    if source_revision is None:
        error = (
            "Current-session source evidence could not be versioned; refine "
            "refused to risk applying a proposal grounded in rewound evidence. "
            "This prevents a stale commit; it cannot reclaim the already-spent "
            "model call."
        )
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or error,
            session_id=session,
            proposal={"action": "no_op", "reason": error, "expected_outcome": ""},
            outcome="evidence_invalidated",
            error=error,
        )
        response = {
            "success": False,
            "outcome": "evidence_invalidated",
            "message": error,
            "evidence": {
                "session_id": session,
                "session_id_source": session_source,
                "session_source": session_db_source,
                "source_lookup_status": source_lookup_status,
            },
            "reversible": False,
        }
        if entry_id:
            response["journal_id"] = entry_id
        return response

    # An explicitly selected session is a strict trajectory boundary. Do not
    # query or echo patterns derived from any other session.
    cross_session_patterns = (
        [] if explicit_session else collect_cross_session_patterns()
    )
    all_error_patterns = patterns.merge_patterns(
        evidence.get("error_patterns", []), cross_session_patterns
    )
    # Keep the complete aggregation local to signal selection. Evidence is
    # returned through several tool-result paths and rendered to the proposal
    # model, so it must remain bounded and include the pattern that opens the
    # gate instead of exposing an arbitrary full cross-session history.
    error_patterns = patterns.prioritize_signal_patterns(
        all_error_patterns,
        min_count=_min_pattern_count,
        session_cap=config.cross_session_max_sessions(),
    )
    evidence["error_patterns"] = error_patterns
    corrections = evidence.get("user_corrections", [])
    evidence_text = _render_evidence_text(evidence)
    proposal_context = safe_reason
    reviewer_context = ""
    _signal_path = "gate_disabled"
    if _min_signal_required and not patterns.has_signal(
        error_patterns, corrections, min_count=_min_pattern_count,
        session_cap=config.cross_session_max_sessions(),
    ):
        _handled = _handle_no_signal(
            llm=llm, evidence=evidence, evidence_text=evidence_text,
            session=session, trigger=trigger, safe_reason=safe_reason,
            min_pattern_count=_min_pattern_count, run_target=_run_target,
            run_target_source=_run_target_source,
            run_target_issues=_run_target_issues,
            run_target_unusable=_run_target_unusable,
            intended_target=_intended_target,
        )
        if isinstance(_handled, dict):
            return _handled
        reviewer_context, _signal_path = _handled


    if _signal_path == "gate_disabled" and _min_signal_required:
        _signal_path = "gate_opened"

    _primary_attempts = 0
    _primary_llm_meta: Dict[str, Any] = {}
    _primary_attempt_limit = 1 if _invocation_bound else _MAX_PRIMARY_ATTEMPTS
    for _primary_attempt in range(_primary_attempt_limit):
        _primary_attempts = _primary_attempt + 1
        proposal = _llm.propose(
            llm=llm,
            evidence_text=evidence_text,
            existing_skills=list_skill_entries(),
            existing_memories=list_memory_snippets(),
            error_patterns=error_patterns,
            user_corrections=[item.get("snippet", "") for item in corrections],
            unused_skills=_unused_skills_safe(),
            # Journal entries can contain trajectory-derived summaries. Exact
            # historical-session analysis must not import those global records.
            refinement_history=(
                []
                if explicit_session
                else journal.recent_refinements(config.history_max_entries())
            ),
            purpose="refine",
            run_context=proposal_context,
            reviewer_context=reviewer_context,
            skill_content_loader=journal.read_skill_content,
            target=_run_target,
        )
        # propose() resets its per-call metadata at the start of every outer
        # attempt. Snapshot immediately so retry costs remain attributable to
        # this one refine pass while provider/model/mode describe the final try.
        call_meta = _llm.last_call_meta()
        if isinstance(call_meta, dict):
            for key in ("latency_ms", "output_tokens"):
                value = call_meta.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    _primary_llm_meta[key] = int(_primary_llm_meta.get(key, 0) or 0) + int(value)
            for key in ("reported_provider", "reported_model", "output_mode"):
                if call_meta.get(key):
                    _primary_llm_meta[key] = call_meta[key]
        _primary_failure = str(proposal.get("failure", "") or "")
        if (
            _primary_failure not in _PRIMARY_RETRY_FAILURES
            or any(marker in str(proposal.get("reason", "")).lower() for marker in _PRIMARY_NONRETRY_MARKERS)
            or _primary_attempt >= _primary_attempt_limit - 1
        ):
            break
        logger.warning(
            "Refine primary backend returned %s (attempt %d/%d); retrying",
            _primary_failure,
            _primary_attempt + 1,
            _primary_attempt_limit,
        )
    # Metadata was snapshotted after every outer proposal attempt above;
    # reading it here would only return the final attempt after its predecessors
    # were reset by propose().
    llm_meta = _primary_llm_meta
    _run_llm_meta = {
        "requested_provider": _intended_target.get("provider", ""),
        "requested_model": _intended_target.get("model", ""),
        "target_source": _run_target_source,
        "signal_path": _signal_path,
        **{k: v for k, v in llm_meta.items() if k in (
            "reported_provider", "reported_model", "latency_ms",
            "output_tokens", "output_mode"
        )},
        "primary_attempts": _primary_attempts,
    }
    _run_llm_meta["model_substituted"] = _model_substituted(
        _intended_target.get("provider", ""), _intended_target.get("model", ""),
        str(llm_meta.get("reported_provider", "")),
        str(llm_meta.get("reported_model", "")),
    )
    if _run_target_issues:
        _run_llm_meta["target_issues"] = _run_target_issues
    proposal = sanitize(proposal)
    proposal = dict(
        proposal,
        expected_outcome=_llm.normalize_expected_outcome(
            proposal.get("expected_outcome")
        ),
    )
    if "summary" in proposal:
        proposal["summary"] = _llm.normalize_summary(proposal["summary"])
    _offered_fps = {
        str(pattern.get("fingerprint", ""))
        for pattern in error_patterns[:patterns.FORMAT_PATTERNS_LIMIT]
        if pattern.get("fingerprint")
    }
    _proposal_fp = str(proposal.get("pattern_fingerprint", "") or "")
    _run_llm_meta["fingerprint_offered"] = len(_offered_fps)
    _run_llm_meta["grounded"] = bool(
        _proposal_fp and _proposal_fp in _offered_fps
    )
    evidence_summary = {
        "session_id": session,
        "session_id_source": session_source,
        "session_source": session_db_source,
        "source_lookup_status": source_lookup_status,
        "messages": len(evidence.get("messages", [])),
        "errors": evidence.get("error_count", 0),
        "fingerprint_offered": _run_llm_meta["fingerprint_offered"],
        "grounded": _run_llm_meta["grounded"],
    }
    failure = scrub_text(str(proposal.get("failure", "")).strip())
    if failure:
        failure_messages = {
            "truncated": "The refine proposal was cut off before it completed.",
            "malformed": "The refine proposal was malformed and could not be read.",
            "no_final_text": (
                "The model returned only reasoning and no final refine proposal."
            ),
            "budget_exhausted": (
                "The model used its full output budget before a final refine proposal."
            ),
            "llm_call_error": "The refine model call failed.",
            "llm_route_error": "The active host route is unavailable for refine.",
            "llm_transport_unsupported": (
                "The active host route uses a transport refine cannot safely use."
            ),
            "llm_trust_denied": "The host trust policy denied the refine model call.",
            "local_safety": scrub_text(str(proposal.get("reason", "")))
            or "The refine proposal could not be completed safely.",
        }
        failure_message = failure_messages.get(
            failure, "The refine proposal could not be completed."
        )
        if failure in (
            "llm_call_error",
            "llm_route_error",
            "llm_transport_unsupported",
            "llm_trust_denied",
        ):
            failure_outcome = "llm_error"
        elif failure == "local_safety":
            failure_outcome = "safety_blocked"
        else:
            failure_outcome = "llm_incomplete"
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or failure_message,
            session_id=session,
            proposal=proposal,
            outcome=failure_outcome,
            error=failure_message,
            llm_meta=_run_llm_meta,
        )
        response = {
            "success": False,
            "outcome": failure_outcome,
            "message": failure_message,
            "llm_called": True,
            "failure": failure,
            "proposal": proposal,
            "evidence": evidence_summary,
            "llm_meta": _run_llm_meta,
            "reversible": False,
        }
        if entry_id:
            response["journal_id"] = entry_id
        return response
    if _run_target_unusable and proposal.get("action") == "no_op":
        failure_message = "The configured refine model target is unusable."
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or failure_message,
            session_id=session,
            proposal=proposal,
            outcome="target_issue",
            error=failure_message,
            llm_meta=_run_llm_meta,
        )
        response = {
            "success": False,
            "outcome": "target_issue",
            "failure": "target_configuration",
            "message": failure_message,
            "proposal": proposal,
            "evidence": evidence_summary,
            "llm_called": True,
            "llm_meta": _run_llm_meta,
            "reversible": False,
        }
        if entry_id:
            response["journal_id"] = entry_id
        return response

    # ── Dry-run exit: show what would happen, apply nothing ────────────────
    if dry_run:
        import difflib as _difflib

        dry_proposal = proposal
        if proposal.get("action") == "multi":
            # Normalize each edit so the user sees the final form.
            edits = [
                _normalize_edit(
                    sanitize(edit), session, explicit_session=explicit_session,
                    session_ending=session_ending,
                )
                for edit in proposal.get("edits", [])
                if isinstance(edit, dict)
            ]
            dry_proposal = dict(proposal, edits=edits)
        else:
            dry_proposal = _normalize_edit(
                proposal, session, explicit_session=explicit_session,
                session_ending=session_ending,
            )

        # Build a diff for patch proposals.
        diff_text = ""
        max_diff_chars = _llm.MAX_CONTENT_CHARS
        truncated = False

        def _build_diff(name: str, new_content: str) -> str:
            old_content = journal.read_skill_content(name) or ""
            old_lines = old_content.splitlines()
            new_lines = new_content.splitlines()
            diff_lines = list(_difflib.unified_diff(
                old_lines, new_lines, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""
            ))
            return "\n".join(diff_lines)

        if dry_proposal.get("action") == "patch" and dry_proposal.get("kind") == "skill":
            name = str(dry_proposal.get("name", ""))
            content = str(dry_proposal.get("content", ""))
            if name and content:
                raw_diff = _build_diff(name, content)
                if len(raw_diff) > max_diff_chars:
                    diff_text = scrub_text(raw_diff[:max_diff_chars]) + "\n… [truncated]"
                    truncated = True
                else:
                    diff_text = scrub_text(raw_diff)
        elif dry_proposal.get("action") == "multi":
            diff_parts = []
            for edit in dry_proposal.get("edits", []):
                if edit.get("action") == "patch" and edit.get("kind") == "skill":
                    name = str(edit.get("name", ""))
                    content = str(edit.get("content", ""))
                    if name and content:
                        diff_parts.append(_build_diff(name, content))
            if diff_parts:
                combined = "\n".join(diff_parts)
                if len(combined) > max_diff_chars:
                    diff_text = scrub_text(combined[:max_diff_chars]) + "\n… [truncated]"
                    truncated = True
                else:
                    diff_text = scrub_text(combined)

        # What the apply would decide. A preview that shows a proposal without
        # saying it is unapplyable is worse than no preview: it reads as approval.
        would_reject = _preview_guardrail_error(dry_proposal) or ""

        # Journal the dry run so /refine audit shows it was considered, and record
        # the verdict, so a previewed-but-unapplyable proposal is distinguishable
        # afterwards from one that would have landed.
        dry_run_entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or "dry-run",
            session_id=session,
            proposal=dry_proposal,
            outcome="dry_run",
            error=would_reject,
            llm_meta=_run_llm_meta,
        )
        if not dry_run_entry_id:
            return _terminal_result(
                outcome="journal_error",
                success=False,
                message="Dry-run proposal was generated, but its journal write failed.",
                proposal=dry_proposal,
                evidence=evidence_summary,
                llm_meta=_run_llm_meta,
                extra={"llm_called": True, "edits_applied": 0},
            )

        return _terminal_result(
            outcome="dry_run",
            success=True,
            message=(
                "Dry run: proposal shown, nothing applied. An apply would be "
                f"rejected by guardrails: {would_reject}"
                if would_reject
                else "Dry run: proposal shown, nothing applied."
            ),
            proposal=dry_proposal,
            llm_meta=_run_llm_meta,
            evidence=evidence_summary,
            extra={
                "journal_id": dry_run_entry_id,
                "diff": diff_text,
                "diff_truncated": truncated,
                "llm_called": True,
                "edits_applied": 0,
                # The preview's verdict, as data rather than prose, so a census
                # can count what would actually land.
                "would_apply": not would_reject,
                "guardrail_error": would_reject,
            },
        )

    if proposal.get("action") == "multi":
        # Acquire the mutation lock only around the apply (mutation), which has
        # already been produced by the proposal call above. The lock serializes
        # mutations, not the LLM call, so a slow provider cannot block the host.
        with journal.mutation_lock():
            transaction = _apply_transaction(
                proposal,
                trigger=trigger,
                safe_reason=safe_reason,
                session=session,
                started=started,
                llm_meta=_run_llm_meta,
                explicit_session=explicit_session,
                session_ending=session_ending,
                source_revision=source_revision,
            )
        transaction["evidence"] = evidence_summary
        transaction["llm_meta"] = _run_llm_meta
        return transaction

    proposal = _normalize_edit(
        proposal, session, explicit_session=explicit_session,
        session_ending=session_ending,
    )

    if proposal.get("action") == "no_op":
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or proposal.get("reason", ""),
            session_id=session,
            proposal=proposal,
            outcome="no_op",
            llm_meta=_run_llm_meta,
        )
        if not entry_id:
            return {
                "success": False,
                "message": "Proposal was no_op, but the journal write failed.",
                "proposal": proposal,
            }
        return {
            "success": True,
            "message": f"No actionable improvement found. {proposal.get('reason', '')}",
            "journal_id": entry_id,
            "proposal": proposal,
            "evidence": evidence_summary,
            "reversible": False,
            "llm_meta": _run_llm_meta,
        }

    # Single-edit apply: serialize the mutation, not the proposal call above.
    with journal.mutation_lock():
        if journal.daily_limit_reached():
            return _terminal_result(
                outcome="rejected",
                success=False,
                message=f"Daily edit limit reached ({config.max_edits_per_day()}).",
                proposal=proposal,
                extra={"edits_applied": 0},
            )
        response = _apply_edit(
            proposal,
            trigger=trigger,
            safe_reason=safe_reason,
            session=session,
            started=started,
            llm_meta=_run_llm_meta,
            source_revision=source_revision,
        )
    response["evidence"] = evidence_summary
    response["llm_meta"] = _run_llm_meta
    return response


def _apply_edit(
    proposal: Dict[str, Any],
    *,
    trigger: str,
    safe_reason: str,
    session: str,
    started: float,
    group: Optional[Dict[str, Any]] = None,
    llm_meta: Optional[Dict[str, Any]] = None,
    source_revision: Optional[frozenset] = None,
    source_session: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate, back up, apply, and finalize exactly one edit.

    Guardrails read live host and journal state, so an edit inside a transaction
    is checked against the edits that were already applied before it.

    ``source_revision`` is the internal evidence-version token captured from the
    active current-session rows used to build the proposal. When provided (a
    real Refine run), it is verified fail-closed before any backup,
    ``journal.prepare``, or host mutation: if the evidence was rewound or the DB
    is unreadable, the edit fails closed with ``evidence_invalidated``. When
    ``None`` (no token captured, e.g. a direct unit call), the check is skipped;
    ``_refine_once`` already fails closed on a capture failure before reaching
    here. This prevents a stale commit from an abandoned branch; it cannot
    reclaim the already-spent model call.
    """
    if source_revision is not None and not _source_revision_is_current(
        source_session or session, source_revision
    ):
        error = (
            "Source evidence was rewound or is unreadable; refine did not apply "
            "a proposal grounded in stale session rows."
        )
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or error,
            session_id=session,
            proposal=proposal,
            outcome="evidence_invalidated",
            error=error,
            group=group,
            llm_meta=llm_meta,
        )
        result = {
            "success": False,
            "outcome": "evidence_invalidated",
            "message": error,
            "proposal": proposal,
            "reversible": False,
            "edits_applied": 0,
        }
        if entry_id:
            result["record_id"] = entry_id
        return result
    guardrail_error = _validate_proposal(proposal)
    if guardrail_error:
        entry_id = _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason,
            session_id=session,
            proposal=proposal,
            outcome="rejected",
            error=guardrail_error,
            group=group,
            llm_meta=llm_meta,
        )
        result = {
            "success": False,
            "message": f"Proposal rejected by guardrails: {guardrail_error}",
            "proposal": proposal,
            "reversible": False,
            "edits_applied": 0,
        }
        if entry_id:
            result["record_id"] = entry_id
        return result

    kind = proposal["kind"]
    action = proposal["action"]
    name = proposal.get("name", "")
    backup_path = ""
    snapshot: Optional[Dict[str, Any]] = None
    recovery: Dict[str, Any] = {}
    prompt_note: Optional[Dict[str, str]] = None
    if kind == "skill" and action == "patch":
        # Check A: refuse before backup if planning baseline is stale.
        conflict_a = _skill_baseline_conflict(proposal)
        if conflict_a:
            entry_id = _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=proposal,
                outcome="conflict",
                error=conflict_a,
                group=group,
                llm_meta=llm_meta,
            )
            result = {
                "success": False,
                "message": conflict_a,
                "proposal": proposal,
                "reversible": False,
                "edits_applied": 0,
            }
            if entry_id:
                result["record_id"] = entry_id
            return result
        if _skill_patch_matches_baseline(proposal):
            unchanged_error = (
                f"Skill '{name}': patch content already matches the verified "
                "current content"
            )
            entry_id = _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=proposal,
                outcome="rejected",
                error=unchanged_error,
                group=group,
                llm_meta=llm_meta,
            )
            result = {
                "success": False,
                "outcome": "rejected",
                "message": f"Proposal rejected by guardrails: {unchanged_error}",
                "proposal": proposal,
                "reversible": False,
                "edits_applied": 0,
            }
            if entry_id:
                result["record_id"] = entry_id
            return result
        captured = journal.prepare_skill_recovery(name)
        if captured is None:
            error = f"Cannot create durable backup for skill '{name}'; mutation aborted"
            _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=proposal,
                outcome="error",
                error=error,
                group=group,
                llm_meta=llm_meta,
            )
            return {
                "success": False,
                "message": error,
                "proposal": proposal,
                "reversible": False,
                "edits_applied": 0,
            }
        # Check B: verify the backup snapshot came from the planning baseline.
        conflict_b = _skill_baseline_conflict(
            proposal, observed_sha=captured["snapshot"]["before_sha256"]
        )
        if conflict_b:
            # The recovery capture wrote a raw backup before discovering the
            # conflict. A conflict is never reversible, so remove that copy;
            # if cleanup fails, retain its path in the journal for auditability.
            conflict_backup = Path(str(captured["backup_path"]))
            retained_backup_path = ""
            try:
                conflict_backup.unlink(missing_ok=True)
            except OSError as exc:
                retained_backup_path = str(conflict_backup)
                logger.warning(
                    "Cannot remove unused conflict backup for skill '%s': %s",
                    name,
                    scrub_text(str(exc)),
                )
            entry_id = _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=proposal,
                outcome="conflict",
                backup_path=retained_backup_path,
                error=conflict_b,
                group=group,
                llm_meta=llm_meta,
            )
            result = {
                "success": False,
                "message": conflict_b,
                "proposal": proposal,
                "reversible": False,
                "edits_applied": 0,
            }
            if entry_id:
                result["record_id"] = entry_id
            return result
        backup_path = str(captured["backup_path"])
        snapshot = captured["snapshot"]
        recovery = {"type": "skill_patch", "name": name}
    elif kind == "skill":
        recovery = {"type": "skill_create", "name": name}
    elif kind == "prompt":
        prompt_note = journal.new_prompt_note(
            proposal["content"],
            scope=str(proposal.get("scope", "global")),
            session_id=str(proposal.get("session_id", "")),
        )
        if prompt_note is None:
            error = "Cannot access plugin-owned prompt-note storage; mutation aborted"
            _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=proposal,
                outcome="error",
                error=error,
                group=group,
                llm_meta=llm_meta,
            )
            return {
                "success": False,
                "message": error,
                "proposal": proposal,
                "reversible": False,
                "edits_applied": 0,
            }
        proposal = dict(proposal, name=prompt_note["id"], note_id=prompt_note["id"])
        name = prompt_note["id"]
        recovery = {"type": "prompt_note", "note_id": prompt_note["id"]}
    else:
        # kind is validated to "memory" here; see _apply_memory for why "user"
        # is unreachable rather than a second real target.
        target = "memory"
        # The host stores the stripped form. Recording anything else as the
        # recovery content makes the post-apply check compare against a string
        # that cannot exist on the host, which reports a landed edit as failed
        # and leaves it un-rollbackable and absent from the audit.
        proposal = dict(proposal, content=str(proposal.get("content", "")).strip())
        memory_recovery = journal.memory_recovery(target, proposal["content"])
        if memory_recovery is None:
            error = f"Cannot capture {target} memory recovery state; mutation aborted"
            _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=proposal,
                outcome="error",
                error=error,
                group=group,
                llm_meta=llm_meta,
            )
            return {
                "success": False,
                "message": error,
                "proposal": proposal,
                "reversible": False,
                "edits_applied": 0,
            }
        recovery = memory_recovery

    try:
        entry_id = journal.prepare(
            trigger=trigger,
            reason=safe_reason,
            session_id=session,
            proposal=proposal,
            backup_path=backup_path,
            recovery=recovery,
            group=group,
            snapshot=snapshot,
            llm_meta=llm_meta,
        )
    except Exception as exc:
        return {
            "success": False,
            "message": f"Journal preparation failed; mutation aborted: {scrub_text(str(exc))}",
            "proposal": proposal,
            "reversible": False,
            "edits_applied": 0,
        }

    try:
        if kind == "skill":
            apply_result = _apply_skill(proposal)
        elif kind == "prompt":
            apply_result = _apply_prompt_note(prompt_note or {})
        else:
            apply_result = _apply_memory(proposal)
    except Exception as exc:
        apply_result = {"success": False, "error": scrub_text(str(exc))}
    apply_result = sanitize(apply_result)

    staged = bool(apply_result.get("success") and apply_result.get("staged"))
    pending_id = scrub_text(str(apply_result.get("pending_id", ""))) if staged else ""
    if staged and not pending_id:
        # The host may already have durably queued this write. Without its ID we
        # cannot claim pending_approval, but terminalizing as error would release
        # budget while a later approval could still mutate the target. Keep the
        # prepared intent consumed and let conservative queue reconciliation
        # recover it or leave it unresolved.
        return {
            "success": False,
            "outcome": "prepared",
            "message": (
                "Host staged the mutation without a pending_id; retained the "
                f"prepared recovery id {entry_id} for reconciliation"
            ),
            "journal_id": entry_id,
            "proposal": proposal,
            "result": sanitize(apply_result),
            "backup_path": backup_path,
            "reversible": False,
            "edits_applied": 1,
        }
    if apply_result.get("success") and not staged:
        prepared_entry = journal.get_entry(entry_id) or {
            "proposal": proposal,
            "recovery": recovery,
            "backup_path": backup_path,
            "snapshot": snapshot or {},
        }
        if not journal.target_matches_applied(prepared_entry):
            apply_result = {
                "success": False,
                "error": "Host reported success but the target state does not match the proposal",
            }
    outcome = (
        "pending_approval"
        if staged
        else ("applied" if apply_result.get("success") else "error")
    )
    try:
        finalized = journal.finalize(
            entry_id,
            outcome,
            error=scrub_text(str(apply_result.get("error", ""))),
            pending_id=pending_id if staged else None,
        )
    except Exception as exc:
        if apply_result.get("success"):
            return {
                "success": False,
                "message": f"Mutation completed but journal finalization failed; recovery id: {entry_id}. Error: {scrub_text(str(exc))}",
                "journal_id": entry_id,
                "proposal": proposal,
                "result": sanitize(apply_result),
                "backup_path": backup_path,
                "reversible": not staged,
                # The mutation landed and its prepared record already consumed
                # budget, so this edit still owns a recovery id even though the
                # run must stop.
                "edits_applied": 1,
            }
        return {
            "success": False,
            "message": f"Apply failed and journal finalization also failed: {scrub_text(str(exc))}",
            "proposal": proposal,
            "result": sanitize(apply_result),
            "reversible": False,
            "edits_applied": 0,
        }

    if outcome in ("applied", "pending_approval"):
        try:
            ledger.record_edit(
                proposal,
                entry_id,
                outcome=outcome,
                pending_id=pending_id,
                llm_meta=llm_meta,
            )
        except Exception as exc:
            logger.warning(
                "Ledger unreadable; edit was applied but attribution was skipped: %s",
                scrub_text(str(exc)),
            )

    message = (
        f"done ({time.time() - started:.1f}s) | action={action} kind={kind} "
        f"name={name} | outcome={outcome}"
    )
    if kind == "prompt":
        # A prompt note's lifetime is part of what was applied, so it is reported
        # rather than left to be discovered in the store. Say so explicitly when
        # the configured scope could not be honoured: a note the user expected to
        # expire at session end is instead permanent.
        note_scope = str(proposal.get("scope", "global"))
        message += f" | scope={note_scope}"
        if note_scope == "global" and config.prompt_notes_default_scope() == "session":
            message += " (session scope needs the live session; kept permanent)"
            # The automatic end-of-session pass throws its result away, so the
            # message alone would leave the one trigger that fires every session
            # reporting a permanent note nowhere but in the journal file.
            note_auto_event(
                "prompt_note_kept_global",
                "A session-scoped note could not bind to the analysed session, "
                "so it was stored permanently instead.",
            )
    if staged and pending_id:
        message += f" | pending_id={pending_id}"
    if apply_result.get("error"):
        message += f" | error={scrub_text(str(apply_result['error']))[:100]}"

    success = bool(apply_result.get("success"))
    response: Dict[str, Any] = {
        "success": success,
        "message": message,
        "proposal": proposal,
        "result": sanitize(apply_result),
        "backup_path": backup_path,
        "reversible": bool(
            success and outcome == "applied" and journal.is_reversible(finalized)
        ),
        "outcome": outcome,
        # The daily budget counts edits, so a transaction reports each applied or
        # reserved edit rather than one proposal.
        "edits_applied": 1 if success else 0,
    }
    if success:
        response["journal_id"] = entry_id
    else:
        response["record_id"] = entry_id
    return response


def _session_can_hold_a_note(
    note_session: str, *, explicit_session: bool, session_ending: bool
) -> bool:
    """Whether a session-scoped note bound to ``note_session`` can still do anything.

    A session note is injected only while that session is current and is deleted
    when it ends, so binding one to a session that is not live wastes a daily edit
    on something that is either never injected or removed within the same call:

    * ``session_ending`` — the automatic end-of-session pass. The session-note
      cleanup in the same worker deletes such a note seconds after it is written.
    * ``explicit_session`` — ``/refine session <id>``. The user named a session to
      *analyse*, normally a past one; the live session id is consulted only here,
      so an automatic pass never reads that process-global value and cannot be
      derailed by another channel writing its own id in between.
    * an empty id — the note store cannot represent it, so nothing would match it.
    """
    if not note_session or session_ending:
        return False
    if not explicit_session:
        return True
    live_session, _ = resolve_session_id()
    return note_session == journal.normalize_prompt_note_session_id(live_session)


def _normalize_edit(
    proposal: Dict[str, Any],
    session: str,
    *,
    explicit_session: bool = False,
    session_ending: bool = False,
) -> Dict[str, Any]:
    """Apply the boundary normalization every edit needs before guardrails run."""
    normalized = dict(
        proposal,
        expected_outcome=_llm.normalize_expected_outcome(
            proposal.get("expected_outcome")
        ),
    )
    if normalized.get("kind") == "prompt":
        scope = config.prompt_notes_default_scope()
        note_session = (
            journal.normalize_prompt_note_session_id(session)
            if scope == "session"
            else ""
        )
        if scope == "session" and not _session_can_hold_a_note(
            note_session,
            explicit_session=explicit_session,
            session_ending=session_ending,
        ):
            scope = "global"
            note_session = ""
        normalized = dict(
            normalized,
            content=journal.normalize_prompt_note_content(normalized.get("content", "")),
            scope=scope,
            session_id=note_session,
        )
    return normalized


def _apply_transaction(
    proposal: Dict[str, Any],
    *,
    trigger: str,
    safe_reason: str,
    session: str,
    started: float,
    llm_meta: Optional[Dict[str, Any]] = None,
    explicit_session: bool = False,
    session_ending: bool = False,
    source_revision: Optional[frozenset] = None,
) -> Dict[str, Any]:
    """Apply one multi-edit proposal as a sequence of independent durable edits.

    Each edit keeps its own journal record, recovery metadata, and rollback id, so
    the existing single-edit rollback and approval machinery is reused unchanged.
    Edits are applied in order and the run stops at the first failure, leaving a
    journal that states exactly which edits applied and which did not.

    ``source_revision`` is the internal evidence-version token captured from the
    active current-session rows used to build the proposal. When provided (a
    real Refine run), it is verified once, fail-closed, before the first edit is
    applied so a proposal grounded in rewound evidence never creates a backup or
    mutates host state. When ``None`` (a direct unit call), the check is skipped.
    """
    edits = [edit for edit in proposal.get("edits", []) if isinstance(edit, dict)]
    if not edits:
        error = "Transaction contained no usable edit"
        _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or error,
            session_id=session,
            proposal=proposal,
            outcome="rejected",
            error=error,
            llm_meta=llm_meta,
        )
        return {
            "success": False,
            "outcome": "failed",
            "message": error,
            "proposal": proposal,
            "results": [],
            "recoveries": [],
            "journal_ids": [],
            "reversible": False,
            "edits_applied": 0,
        }
    group_id = uuid.uuid4().hex[:12]
    summary = _llm.normalize_summary(proposal.get("summary", ""))
    shared_reason = scrub_text(str(proposal.get("reason", "")))
    shared_expected = _llm.normalize_expected_outcome(proposal.get("expected_outcome"))
    shared_fingerprint = str(proposal.get("pattern_fingerprint", "") or "")
    dropped = int(proposal.get("dropped_edits", 0) or 0)

    def edit_proposal(edit: Dict[str, Any]) -> Dict[str, Any]:
        """Give one edit the transaction's shared justification, then normalize it."""
        merged = dict(edit)
        if not str(merged.get("reason", "")).strip():
            merged["reason"] = shared_reason
        if not str(merged.get("expected_outcome", "") or "").strip():
            merged["expected_outcome"] = shared_expected
        if not str(merged.get("pattern_fingerprint", "") or ""):
            merged["pattern_fingerprint"] = shared_fingerprint
        return _normalize_edit(
            sanitize(merged), session, explicit_session=explicit_session,
            session_ending=session_ending,
        )

    def edit_group(index: int) -> Dict[str, Any]:
        group = {
            "id": group_id,
            "index": index,
            "size": len(edits),
            "summary": summary,
        }
        if dropped:
            group["dropped"] = dropped
        return group

    results: List[Dict[str, Any]] = []
    stop_reason = ""

    # ── Overlap preflight: two patches to the same skill share a mutable
    # target, so their independent baselines cannot both remain valid. Refuse
    # the whole transaction before any backup, host mutation, or budget use.
    patch_targets: Dict[str, List[int]] = {}
    for index, edit in enumerate(edits):
        normalized = edit_proposal(edit)
        if (
            normalized.get("kind") == "skill"
            and normalized.get("action") == "patch"
        ):
            target = str(normalized.get("name", "")).strip()
            if target:
                patch_targets.setdefault(target, []).append(index)
    overlapping = {
        target: indexes
        for target, indexes in patch_targets.items()
        if len(indexes) > 1
    }
    if overlapping:
        rendered_targets = ", ".join(
            f"{target!r} at index {indexes}"
            for target, indexes in sorted(overlapping.items())
        )
        conflict_msg = (
            "Transaction rejected: overlapping edits in this transaction "
            f"({rendered_targets})"
        )
        for index, edit in enumerate(edits):
            _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=edit_proposal(edit),
                outcome="rejected",
                error=conflict_msg,
                group=edit_group(index),
                llm_meta=llm_meta,
            )
        return {
            "success": False,
            "outcome": "failed",
            "message": conflict_msg,
            "proposal": proposal,
            "results": [],
            "recoveries": [],
            "journal_ids": [],
            "reversible": False,
            "edits_applied": 0,
        }

    # ── Stale-plan preflight: reject the entire transaction if any skill patch
    # was built from content that no longer matches the live host state. This
    # prevents a partial apply where edit #1 succeeds but edit #2 would conflict.
    # Also rejects patches with missing or malformed baselines (fail closed).
    # Also classify verified no-op patches here: discovering one only inside
    # _apply_edit could let an earlier edit land before this inseparable
    # transaction is rejected.
    stale_edits: List[int] = []
    unchanged_edits: List[int] = []
    for index, edit in enumerate(edits):
        normalized = edit_proposal(edit)
        if (
            normalized.get("kind") == "skill"
            and normalized.get("action") == "patch"
        ):
            conflict = _skill_baseline_conflict(normalized)
            if conflict:
                stale_edits.append(index)
            elif _skill_patch_matches_baseline(normalized):
                unchanged_edits.append(index)
    if stale_edits:
        conflict_msg = (
            f"Transaction rejected: entry changed during refinement planning "
            f"(stale edit(s) at index {stale_edits})"
        )
        for index, edit in enumerate(edits):
            normalized = edit_proposal(edit)
            if index in stale_edits:
                _journal_nonmutation(
                    trigger=trigger,
                    reason=safe_reason,
                    session_id=session,
                    proposal=normalized,
                    outcome="conflict",
                    error=conflict_msg,
                    group=edit_group(index),
                    llm_meta=llm_meta,
                )
            else:
                _journal_nonmutation(
                    trigger=trigger,
                    reason=safe_reason,
                    session_id=session,
                    proposal=normalized,
                    outcome="rejected",
                    error=conflict_msg,
                    group=edit_group(index),
                    llm_meta=llm_meta,
                )
        return {
            "success": False,
            "outcome": "failed",
            "message": conflict_msg,
            "proposal": proposal,
            "results": [],
            "recoveries": [],
            "journal_ids": [],
            "reversible": False,
            "edits_applied": 0,
        }

    if unchanged_edits:
        unchanged_msg = (
            "Transaction rejected: patch content already matches the verified "
            f"target (unchanged edit(s) at index {unchanged_edits})"
        )
        for index, edit in enumerate(edits):
            _journal_nonmutation(
                trigger=trigger,
                reason=safe_reason,
                session_id=session,
                proposal=edit_proposal(edit),
                outcome="rejected",
                error=unchanged_msg,
                group=edit_group(index),
                llm_meta=llm_meta,
            )
        return {
            "success": False,
            "outcome": "failed",
            "message": unchanged_msg,
            "proposal": proposal,
            "results": [],
            "recoveries": [],
            "journal_ids": [],
            "reversible": False,
            "edits_applied": 0,
        }

    # Fail-closed evidence gate: verify the source rows used to build the
    # proposal are still active before applying the first edit. A rewind or
    # rewrite archives/replaces those rows; a guard here prevents a proposal
    # grounded in an abandoned branch from creating any backup or consuming any
    # daily edit. This cannot reclaim the already-spent model call.
    if source_revision is not None and not _source_revision_is_current(session, source_revision):
        error = (
            "Source evidence was rewound or is unreadable; refine did not apply "
            "a multi-edit transaction grounded in stale session rows."
        )
        _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason or error,
            session_id=session,
            proposal=proposal,
            outcome="evidence_invalidated",
            error=error,
            llm_meta=llm_meta,
        )
        return {
            "success": False,
            "outcome": "evidence_invalidated",
            "message": error,
            "proposal": proposal,
            "results": [],
            "recoveries": [],
            "journal_ids": [],
            "reversible": False,
            "edits_applied": 0,
        }

    for index, edit in enumerate(edits):
        # Re-read the durable budget between edits: it counts edits, so a long
        # transaction can legitimately exhaust it part way through.
        if journal.daily_limit_reached():
            stop_reason = (
                f"Daily edit limit reached ({config.max_edits_per_day()}) "
                "before this edit was attempted"
            )
            break
        item = _apply_edit(
            edit_proposal(edit),
            trigger=trigger,
            safe_reason=safe_reason,
            session=session,
            started=started,
            group=edit_group(index),
            llm_meta=llm_meta,
            source_revision=source_revision,
            source_session=session,
        )
        results.append(item)
        if not item.get("success"):
            stop_reason = (
                f"An earlier edit of transaction {group_id} did not complete"
            )
            break
        if item.get("outcome") == "pending_approval":
            stop_reason = (
                "An earlier edit is pending host approval; remaining edits "
                "cannot proceed until the approval is resolved"
            )
            break

    # Every edit of a transaction leaves a durable trace, so a partial
    # application is readable from the journal alone rather than only from a
    # message that automatic runs discard. ``rejected`` consumes no daily budget.
    for index in range(len(results), len(edits)):
        _journal_nonmutation(
            trigger=trigger,
            reason=safe_reason,
            session_id=session,
            proposal=edit_proposal(edits[index]),
            outcome="rejected",
            error=stop_reason or "Edit was not attempted",
            group=edit_group(index),
            llm_meta=llm_meta,
        )

    # "Recoverable" is deliberately wider than "successful": an edit whose host
    # mutation landed but whose journal finalization then failed still owns a
    # recovery id and must appear in the list the message points the user at.
    recoverable = [item for item in results if int(item.get("edits_applied", 0) or 0)]
    succeeded = [item for item in results if item.get("success")]
    recoveries = _recoveries_for(recoverable)
    skipped = len(edits) - len(results)
    elapsed = time.time() - started

    has_pending = any(item.get("outcome") == "pending_approval" for item in results)
    if len(succeeded) == len(edits) and not dropped and not has_pending:
        success, outcome = True, "completed"
        message = (
            f"transaction {group_id}: {len(succeeded)} edit(s) applied or reserved "
            f"({elapsed:.1f}s)"
        )
    elif recoverable:
        success, outcome = False, "partial_success"
        message = (
            f"PARTIAL SUCCESS: transaction {group_id} applied or reserved "
            f"{len(recoverable)} of {len(edits)} edit(s) and then stopped. "
            "Use the recovery IDs listed below, newest first."
        )
    else:
        success, outcome = False, "failed"
        message = f"transaction {group_id}: no edit was applied"
    if results and not results[-1].get("success"):
        message += f" | stopped: {scrub_text(str(results[-1].get('message', '')))[:160]}"
    elif skipped:
        rendered_stop = scrub_text(stop_reason)[:160] or "edits were not attempted"
        if rendered_stop.startswith("Daily "):
            rendered_stop = "daily " + rendered_stop[6:]
        message += f" | stopped: {rendered_stop}; {skipped} edit(s) not attempted"
    if dropped:
        message += f" | {dropped} proposed edit(s) discarded before apply"
    if summary:
        message += f" | {summary}"

    return {
        "success": success,
        "outcome": outcome,
        "message": message,
        "proposal": proposal,
        "results": results,
        "recoveries": recoveries,
        "journal_ids": [item["journal_id"] for item in recoveries],
        "reversible": any(item.get("reversible") for item in recoveries),
        "edits_applied": len(recoverable),
    }


def _completed_targets(result: Dict[str, Any]) -> List[str]:
    """Name what a pass already reserved, so the next pass cannot repeat it."""
    items = result.get("results")
    proposals = (
        [
            item.get("proposal", {})
            for item in items
            if isinstance(item, dict) and item.get("success")
        ]
        if isinstance(items, list)
        else [result.get("proposal", {})]
    )
    targets: List[str] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        action = scrub_text(str(proposal.get("action", "")))
        if action in ("", "no_op", "multi"):
            continue
        kind = scrub_text(str(proposal.get("kind", "")))
        name = scrub_text(str(proposal.get("name", "")))
        targets.append(f"{action} {kind} '{name}'")
    return targets


def _recoveries_for(applied: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Describe every durable recovery id an applied or reserved edit left behind.

    Newest first, because that is the only safe rollback order: memory recovery
    is positional, so undoing an earlier append before a later one shifts the
    later entry and its rollback fails closed as a conflict.
    """
    recoveries: List[Dict[str, Any]] = []
    for item in reversed(applied):
        journal_id = item.get("journal_id")
        if not journal_id:
            continue
        durable = journal.get_entry(str(journal_id)) or {}
        recovery: Dict[str, Any] = {
            "journal_id": str(journal_id),
            "outcome": durable.get("outcome", item.get("outcome", "unknown")),
            "reversible": bool(item.get("reversible")),
        }
        if item.get("reversible"):
            recovery["rollback_command"] = f"/refine rollback {journal_id}"
        recoveries.append(recovery)
    return recoveries


def refine_run(
    llm: Optional[PluginLlm],
    *,
    reason: str = "",
    session_id: Optional[str] = None,
    auto: bool = False,
    dry_run: bool = False,
    explicit_session: bool = False,
    session_ending: bool = False,
) -> Dict[str, Any]:
    """Serialize a run, reconcile approvals, and preserve every recovery id.

    ``explicit_session`` marks the ``/refine session <id>`` form, where the user
    names a session to analyse rather than working in it; ``session_ending`` marks
    the automatic end-of-session pass. Only prompt-note scoping reads either —
    see ``_session_can_hold_a_note``.
    """
    started = time.time()
    # The mutation lock serializes *mutations* and nothing else. Evidence
    # collection and the LLM proposal call mutate no state, so they must run
    # WITHOUT the lock: otherwise a slow or hung provider (live measured 28 s)
    # blocks every other refine operation on the host. The lock is acquired in
    # _apply_edit / _apply_transaction, immediately before the first backup or
    # write. The budget is re-verified there, after acquiring, not before.
    # Reordering is itself a mutation, so it keeps its own brief lock.
    with journal.mutation_lock():
        try:
            _reconcile_pending()
        except IOError:
            return {
                "success": False,
                "outcome": "journal_unreadable",
                "message": "Journal could not be read; refine did not run to avoid bypassing budget limits.",
                "reversible": False,
            }

    if dry_run:
        # Dry-run: one proposal pass, no apply, no budget consumed.
        return _refine_once(
            llm, reason=scrub_text(reason), session_id=session_id,
            auto=auto, dry_run=True, explicit_session=explicit_session,
            session_ending=session_ending,
        )

    runs: List[Dict[str, Any]] = []
    # ``max_edits_per_run`` bounds proposal passes; ``max_edits_per_proposal``
    # bounds edits inside one transaction; the daily edit budget bounds edits
    # overall and is re-checked after acquiring the mutation lock, before every
    # single edit.
    max_runs = max(1, config.max_edits_per_run())
    run_reason = scrub_text(reason)
    for _ in range(max_runs):
        if journal.daily_limit_reached():
            break
        result = _refine_once(
            llm, reason=run_reason, session_id=session_id, auto=auto,
            explicit_session=explicit_session, session_ending=session_ending,
        )
        runs.append(result)
        if not result.get("success") or not int(result.get("edits_applied", 0) or 0):
            break
        targets = _completed_targets(result)
        if not targets:
            break
        note = (
            f"Already completed or reserved {'; '.join(targets)} in this run; "
            "propose a different edit or no_op."
        )
        run_reason = f"{reason}\n{note}".strip() if reason else note
        run_reason = scrub_text(run_reason)

    if not runs:
        return {
            "success": False,
            "message": f"Daily edit limit reached ({config.max_edits_per_day()}).",
            "reversible": False,
        }
    if len(runs) == 1:
        return runs[0]

    recoveries: List[Dict[str, Any]] = []
    # Each pass already returns its own recoveries newest-first.  Traverse
    # passes in reverse too, so the combined list is directly safe for
    # positional memory rollback.
    for item in reversed(runs):
        inner = item.get("recoveries")
        if isinstance(inner, list) and inner:
            recoveries.extend(inner)
            continue
        if item.get("journal_id") and int(item.get("edits_applied", 0) or 0):
            recoveries.extend(_recoveries_for([item]))

    failed_after_success = bool(
        recoveries and any(not item.get("success") for item in runs)
    )
    last = runs[-1]
    if failed_after_success:
        message = (
            f"PARTIAL SUCCESS: {len(recoveries)} earlier edit(s) were applied or reserved, "
            "but a later pass failed. Use the recovery IDs listed below."
        )
        outcome = "partial_success"
        success = False
    else:
        message = (
            f"{len(runs)} pass(es), {len(recoveries)} edit(s) applied or reserved "
            f"({time.time() - started:.1f}s)"
        )
        outcome = "completed"
        success = all(item.get("success") for item in runs)
    response: Dict[str, Any] = {
        "success": success,
        "outcome": outcome,
        "message": message,
        "proposal": last.get("proposal", runs[0].get("proposal", {})),
        "results": runs,
        "recoveries": recoveries,
        "journal_ids": [item["journal_id"] for item in recoveries],
        "evidence": runs[0].get("evidence", {}),
        "reversible": any(item.get("reversible") for item in recoveries),
        "edits_applied": len(recoveries),
    }
    return response


def refine_rollback(entry_id: str) -> Dict[str, Any]:
    with journal.mutation_lock():
        _reconcile_pending()
        entry = journal.get_entry(entry_id)
        if not entry:
            return {"success": False, "error": f"Entry {entry_id} not found"}
        if entry.get("outcome") == "rolled_back":
            return {"success": True, "message": f"Entry {entry_id} is already rolled back"}
        if entry.get("outcome") == "pending_rollback":
            return {
                "success": True,
                "staged": True,
                "pending_id": entry.get("pending_id", ""),
                "message": "Rollback is still pending approval; target is unchanged",
            }
        if not journal.is_reversible(entry):
            return {"success": False, "error": f"Entry {entry_id} is not reversible"}
        kind = entry.get("proposal", {}).get("kind", "skill")
        if kind == "skill":
            result = journal.rollback_skill(entry_id)
        elif kind == "memory":
            result = journal.rollback_memory(entry_id)
        elif kind == "prompt":
            result = journal.rollback_prompt_note(entry_id)
        else:
            return {"success": False, "error": f"Unknown kind for rollback: {kind}"}
        latest = journal.get_entry(entry_id)
        if latest:
            try:
                ledger.record_journal_state(latest)
            except Exception as exc:
                logger.warning("Cannot mirror rollback state in ledger: %s", scrub_text(str(exc)))
        return sanitize(result)
