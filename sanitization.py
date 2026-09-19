"""Line-structure hygiene shared by the validation and prompt paths.

This module used to carry a credential grammar: ``scrub_text`` replaced anything
shaped like a key, token, password, PEM block, ``Authorization`` header or URL
userinfo with ``[REDACTED]``, and ``sanitize`` walked a whole structure applying
it. Both are gone, by the owner's decision, and nothing here redacts anything.

Why they went, in short. The filter hid nothing from the model: Hermes passes the
conversation to the same model itself and has already stored it in ``state.db``.
Everything refine writes -- MEMORY.md, skills, prompt notes, its journal -- is
local, beside that same database. Against that it cost real correctness: it was
not JSON-transparent, so a structured tool result carrying a credential-shaped
field stopped parsing and a failure could be classified as a success; it ran
ahead of ``patterns.normalize_error``, so it could merge two different errors into
one fingerprint; and six validators asked "would the scrubber change this?" as a
proxy for "is this well-formed", which silently dropped session ids and refused
legitimate model names.

What is left is the one thing those callers actually shared: the exact set of
codepoints that end a line. Two callers need the same answer for opposite
purposes -- ``core`` refuses these inside a skill or memory body, ``llm``
collapses them out of a value whose contract is to render as one prompt line --
and two lists would drift invisibly, each site working while disagreeing about
which characters exist.
"""

import re

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
