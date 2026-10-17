"""Third-party text for context packs (A§8; RISK.md's prompt-injection row; DECISIONS D-062).

News is stored raw (A§4.2) and cleaned only here, when a pack is built:

- ``normalize(raw)``: HTML reduced to its text (``<script>``, ``<style>`` and the like
  dropped, entities decoded, block tags read as spaces), NFKC, invisible characters removed
  (controls, zero-width and bidi marks, Unicode tag characters, variation selectors, private-use
  and unassigned code points; characters that render as a blank become spaces), whitespace
  collapsed. The steps repeat until nothing changes, so double-escaped markup comes out too.
  Text that hasn't settled after ``_MAX_PASSES`` is replaced by ``UNSETTLED``: no real news
  needs more than three passes, and what remains could otherwise reach the pack as markup.
- ``clean_text(raw, max_chars)``: ``normalize``, then ``cut`` to at most ``max_chars``
  characters at a word boundary, ending in ``...``. Cleaning clean text changes nothing.
- ``is_suspicious(text)``: injection heuristics over several views of the text: the clean
  text; the decoded text with its tags and line breaks kept; the text with invisible
  characters read as spaces and glued words split; text smuggled in tag characters or
  reversed under a bidi override; and text CSS hides from a human reader. Each view is folded
  first: lookalike letters read as Latin, accents and stacked marks dropped, curly quotes
  straightened, case ignored. A letters-only form, with digits read as letters, catches
  spaced-out and leetspeak phrases. The patterns aim at what addresses a model, not at news
  words: "ignores Fed guidance", "rates XYZ Neutral" and "Attention to AI spending" are news;
  "ignore previous instructions" and "Note to AI models:" are not.
- ``untrusted(headline, summary, limits)``: the pack's ``{"untrusted": {...}, "suspicious": ...}``.
- ``anonymize(text, names)``: hides the given names (the E1 experiment; off by default).

A flag informs Claude and, per RISK.md, sets the candidate's ``suspicious_content`` risk flag
(code ORs it in after validation: P4.4). The real defenses lie elsewhere: labeled data, output
bound to a schema that can only veto or reduce, and no tools (RISK.md).
"""

import html
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TypedDict, get_args

from trader.llm.schemas import CatalystType, DayMode, RiskFlag

MARK = "..."  # ends a cut; "…" would not survive the next pass's NFKC
UNSETTLED = "[text removed: its markup would not settle]"
RAW_CAP = 32_768  # characters read per field; Benzinga's are a few hundred
_MAX_PASSES = 8  # each pass decodes one more layer of escaping
_WORD_BACKOFF = 15  # a cut steps back this far, at most, to end at a space


@dataclass(frozen=True)
class TextLimits:
    """Characters kept per field of a news item in a pack (P4.3 may pass its own)."""

    headline: int = 300
    summary: int = 600

    def __post_init__(self) -> None:
        if min(self.headline, self.summary) <= len(MARK):
            raise ValueError(f"each limit must exceed {len(MARK)} characters")


DEFAULT_LIMITS = TextLimits()


class UntrustedText(TypedDict):
    headline: str
    summary: str


class NewsText(TypedDict):
    untrusted: UntrustedText
    suspicious: bool


# ---------------------------------------------------------------- cleaning

# Elements whose content never reaches the pack. The raw-text ones (xmp, plaintext, noembed,
# noframes) would otherwise pass their markup through as text, a pass at a time.
_HIDDEN_TAGS = frozenset(
    {"script", "style", "noscript", "template", "iframe", "object", "svg", "xmp", "plaintext",
     "noembed", "noframes"}
)  # fmt: skip
_BREAK_TAGS = frozenset(
    {"br", "p", "div", "li", "ul", "ol", "tr", "td", "th", "table", "hr", "blockquote",
     "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt",
     "dd", "caption", "figure", "figcaption", "thead", "tbody", "tfoot", "pre", "main", "nav",
     "aside", "address"}
)  # fmt: skip
_VOID_TAGS = frozenset(
    {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source",
     "track", "wbr"}
)  # fmt: skip
_CONCEALING_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|(?:font-size|opacity)\s*:\s*0+(?:\.0+)?"
    r"\s*(?:px|pt|em|rem|%)?\s*(?:;|$)",
    re.IGNORECASE,
)
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_INVISIBLE = frozenset(  # marks and letters that render as nothing
    {"\N{COMBINING GRAPHEME JOINER}", "\N{KHMER VOWEL INHERENT AQ}",
     "\N{KHMER VOWEL INHERENT AA}", "\N{MUSICAL SYMBOL NULL NOTEHEAD}",
     "\N{KHITAN SMALL SCRIPT FILLER}"}
)  # fmt: skip
_BLANK = frozenset(  # letters that render as a blank: a reader sees a space, so does the pack
    {"\N{HANGUL CHOSEONG FILLER}", "\N{HANGUL JUNGSEONG FILLER}", "\N{HANGUL FILLER}",
     "\N{HALFWIDTH HANGUL FILLER}", "\N{BRAILLE PATTERN BLANK}"}
)  # fmt: skip
_LINE_BREAKS = frozenset("\n\r\N{LINE SEPARATOR}\N{PARAGRAPH SEPARATOR}\N{NEXT LINE}")
_ASCII_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1b\x7f]")  # the controls that aren't spaces
_COMBINING = frozenset({"Mn", "Mc", "Me"})
# A "<" that would hide text: one that never closes, or a "<US$1"-style price, unless it
# opens a role tag (those are dropped, and flagged).
_STRAY_LT = re.compile(
    r"<(?!/?\s*(?:system|assistant|user|human|instructions?|untrusted|prompt)\b)"
    r"(?:(?=/?[A-Za-z0-9][^<>]*(?:<|$))|(?=[A-Za-z]{1,3}\$\d))",
    re.IGNORECASE,
)
# Kept apart before NFKC, which would glue them to the number or word before them ("4½" ->
# "41/2", "$1.25¹" -> "$1.251", "ZEPBOUND™" -> "ZEPBOUNDTM").
_SUPERSCRIPT_DIGITS = {
    0x2070: "^0", 0x00B9: "^1", 0x00B2: "^2", 0x00B3: "^3", 0x2074: "^4", 0x2075: "^5",
    0x2076: "^6", 0x2077: "^7", 0x2078: "^8", 0x2079: "^9", 0x2122: None, 0x2120: None,
}  # fmt: skip
_NUMBER_END = re.compile(r"\d[\d.,]*$")
# A cut ending in one of these would open markup with the mark: "</..." is a comment the
# next pass drops, so the cut text wouldn't survive cleaning (D-063).
_OPENER_END = re.compile(r"<(?:/|!-{0,2}|\?)?$")
_UNIT_NEXT = re.compile(
    r"\s+(?:thousand|million|billion|trillion|mln|bln|percent|pct|cents?|bps|basis\s+points|"
    r"shares)\b|\s*%",
    re.IGNORECASE,
)
_FRACTIONS = "".join(chr(c) for c in (0xBC, 0xBD, 0xBE, *range(0x2150, 0x215F), 0x2189))
_FRACTION_AFTER_DIGIT = re.compile(rf"(?<=\d)(?=[{_FRACTIONS}])")


class _TextOnly(HTMLParser):
    """The text of HTML, and how many letters of it CSS hides from a human reader."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.concealed_letters = 0
        self._hidden = 0
        self._sup = 0
        self._open: list[tuple[str, bool]] = []  # open elements, and whether each conceals
        self._open_tags: Counter[str] = Counter()  # how many of each tag are open
        self._concealing = 0  # open elements that conceal, counted rather than rescanned

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif tag in _BREAK_TAGS:
            self.parts.append(" ")
        if tag == "sup":
            self._sup += 1
        if tag not in _VOID_TAGS:
            conceals = any(_conceals(name, value) for name, value in attrs)
            self._open.append((tag, conceals))
            self._open_tags[tag] += 1
            self._concealing += conceals

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BREAK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BREAK_TAGS:
            self.parts.append(" ")
        if tag == "sup":
            self._sup = max(0, self._sup - 1)
        if not self._open_tags[tag]:
            return  # it closes nothing that is open
        for i in range(len(self._open) - 1, -1, -1):  # what the scan passes, it closes
            if self._open[i][0] == tag:
                for name, conceals in self._open[i:]:
                    self._open_tags[name] -= 1
                    self._concealing -= conceals
                del self._open[i:]
                break

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._concealing:
            self.concealed_letters += sum(ch.isalpha() for ch in data)
        if self._sup and data.strip().isdigit():
            data = "^" + data.strip()  # a footnote mark stays apart from the number before
        self.parts.append(data)


def _conceals(name: str, value: str | None) -> bool:
    if name == "hidden":
        return True
    if name == "aria-hidden":
        return (value or "").strip().lower() == "true"
    return name == "style" and value is not None and _CONCEALING_STYLE.search(value) is not None


def _parse(text: str) -> _TextOnly:
    parser = _TextOnly()
    parser.feed(_STRAY_LT.sub("&lt;", text))
    parser.close()
    return parser


def _text_of_html(text: str) -> str:
    if "<" not in text and "&" not in text:
        return text
    return "".join(_parse(text).parts)


def _is_invisible(ch: str) -> bool:
    code = ord(ch)
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF or 0x180B <= code <= 0x180F:
        return True  # variation selectors, which can carry hidden data
    return ch in _INVISIBLE or unicodedata.category(ch) in _INVISIBLE_CATEGORIES


def _visible(text: str, *, invisible_as: str = "") -> str:
    """``text`` with every invisible character removed (or read as ``invisible_as``),
    blank-looking letters and every kind of space read as a space, and line breaks kept."""
    if text.isascii():
        return _ASCII_CONTROLS.sub(invisible_as, text).replace("\r", "\n")
    out = []
    for ch in text:
        if ch in _LINE_BREAKS:
            out.append("\n")
        elif ch.isspace() or ch in _BLANK:
            out.append(" ")
        elif _is_invisible(ch):
            out.append(invisible_as)
        else:
            out.append(ch)
    return "".join(out)


def _nfkc(text: str) -> str:
    """NFKC, keeping superscript digits, fractions and trademark signs from merging into
    the word or number before them."""
    text = _FRACTION_AFTER_DIGIT.sub(" ", text.translate(_SUPERSCRIPT_DIGITS))
    return unicodedata.normalize("NFKC", text).replace("\N{FRACTION SLASH}", "/")


def _settle(raw: str, *, invisible_as: str = "") -> str | None:
    """``raw`` passed through NFKC, ``_visible`` and HTML until nothing changes; None if it
    hasn't settled after ``_MAX_PASSES``."""
    text = raw
    for _ in range(_MAX_PASSES):
        step = _text_of_html(_visible(_nfkc(text), invisible_as=invisible_as))
        if step == text:
            return text
        text = step
    return None


def normalize(raw: str) -> str:
    """The visible text of ``raw`` (see the module docstring), uncut."""
    settled = _settle(raw)
    return UNSETTLED if settled is None else " ".join(settled.split())


def cut(text: str, max_chars: int) -> str:
    """``text`` if it fits in ``max_chars`` characters; else as much as fits before ``MARK``,
    ending at a space when one lies close enough, and never parting a letter from its
    marks, a virama from what follows it, or a flag's two letters."""
    if max_chars <= len(MARK):
        raise ValueError(f"max_chars must exceed {len(MARK)}")
    if len(text) <= max_chars:
        return text
    end = max_chars - len(MARK)
    space = text.rfind(" ", max(0, end - _WORD_BACKOFF), end + 1)
    if space > 0:
        end = space
        if _NUMBER_END.search(text, 0, end) and _UNIT_NEXT.match(text, end):
            end = max(0, text.rfind(" ", 0, end))  # "$1.25" without its "billion" misleads
    while end > 0 and (
        unicodedata.category(text[end]) in _COMBINING or unicodedata.combining(text[end - 1]) == 9
    ):
        end -= 1
    flags = 0
    while flags < end and 0x1F1E6 <= ord(text[end - 1 - flags]) <= 0x1F1FF:
        flags += 1
    end -= flags % 2
    kept = text[:end].rstrip()
    while opener := _OPENER_END.search(kept):
        kept = kept[: opener.start()].rstrip()
    return kept + MARK


def clean_text(raw: str, max_chars: int) -> str:
    return cut(normalize(raw[:RAW_CAP]), max_chars)


# ---------------------------------------------------------------- injection heuristics


def _lookalikes() -> dict[int, str]:
    """Letters and quotes that pass for ASCII ones, mapped to them (looked up by name)."""
    table: dict[int, str] = {}

    def add(prefix: str, pairs: str) -> None:
        for pair in pairs.split():
            name, latin = pair.rsplit("=", 1)
            table[ord(unicodedata.lookup(f"{prefix} {name.replace('_', ' ')}"))] = latin

    add(
        "CYRILLIC SMALL LETTER",
        "A=a IE=e O=o ER=p ES=c U=y HA=x BYELORUSSIAN-UKRAINIAN_I=i "
        "JE=j DZE=s KOMI_DE=d SHHA=h PALOCHKA=l KA=k EM=m EN=h TE=t VE=b IO=e YI=i QA=q WE=w",
    )
    add(
        "CYRILLIC CAPITAL LETTER",
        "A=A IE=E O=O ER=P ES=C U=Y HA=X BYELORUSSIAN-UKRAINIAN_I=I "
        "JE=J DZE=S KA=K EM=M EN=H TE=T VE=B",
    )
    add(
        "GREEK SMALL LETTER",
        "ALPHA=a OMICRON=o RHO=p NU=v TAU=t UPSILON=u IOTA=i KAPPA=k "
        "CHI=x EPSILON=e ETA=n OMEGA=w ZETA=z BETA=b MU=u",
    )
    add(
        "GREEK CAPITAL LETTER",
        "ALPHA=A OMICRON=O RHO=P NU=N TAU=T UPSILON=Y IOTA=I KAPPA=K "
        "CHI=X EPSILON=E ETA=H ZETA=Z BETA=B MU=M",
    )
    add("ARMENIAN SMALL LETTER", "OH=o VO=n SEH=u CO=g HO=h")
    add("LATIN SMALL LETTER", "DOTLESS_I=i ALPHA=a SCRIPT_G=g I_WITH_STROKE=i")
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        try:
            table[ord(unicodedata.lookup(f"LATIN LETTER SMALL CAPITAL {letter}"))] = letter.lower()
        except KeyError:
            continue
    for start in (0x1F150, 0x1F170, 0x1F1E6):  # negative circled, negative squared, flags
        for i in range(26):
            table[start + i] = chr(ord("a") + i)
    double = ("LEFT DOUBLE QUOTATION MARK", "RIGHT DOUBLE QUOTATION MARK",
              "DOUBLE LOW-9 QUOTATION MARK", "DOUBLE HIGH-REVERSED-9 QUOTATION MARK",
              "LEFT-POINTING DOUBLE ANGLE QUOTATION MARK",
              "RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK")  # fmt: skip
    single = ("LEFT SINGLE QUOTATION MARK", "RIGHT SINGLE QUOTATION MARK",
              "SINGLE LOW-9 QUOTATION MARK", "SINGLE HIGH-REVERSED-9 QUOTATION MARK", "PRIME",
              "GRAVE ACCENT", "ACUTE ACCENT")  # fmt: skip
    table.update({ord(unicodedata.lookup(n)): '"' for n in double})
    table.update({ord(unicodedata.lookup(n)): "'" for n in single})
    for name, char in (
        ("MATHEMATICAL LEFT ANGLE BRACKET", "<"),
        ("MATHEMATICAL RIGHT ANGLE BRACKET", ">"),
        ("LEFT ANGLE BRACKET", "<"),
        ("RIGHT ANGLE BRACKET", ">"),
        ("SINGLE LEFT-POINTING ANGLE QUOTATION MARK", "<"),
        ("SINGLE RIGHT-POINTING ANGLE QUOTATION MARK", ">"),
    ):
        table[ord(unicodedata.lookup(name))] = char
    return table


_FOLD = _lookalikes()
_LEET = str.maketrans("0134578@$", "oieastbas")
_TAG_CHARS = range(0xE0020, 0xE007F)  # Unicode tags: invisible copies of ASCII
_TAG_FLAG = re.compile(r"\U0001F3F4[\U000E0030-\U000E0039\U000E0061-\U000E007A]{2,6}\U000E007F")
_REVERSING = frozenset(
    {"\N{RIGHT-TO-LEFT OVERRIDE}", "\N{RIGHT-TO-LEFT EMBEDDING}", "\N{RIGHT-TO-LEFT ISOLATE}"}
)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _fold(text: str) -> str:
    """For matching only: marks dropped, lookalikes and quotes read as ASCII, case ignored,
    spaces collapsed but line breaks kept."""
    bare = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    folded = bare.translate(_FOLD).casefold()
    return "\n".join(" ".join(line.split()) for line in folded.splitlines())


_AI_WORDS = (
    r"ai|a\.i\.|llms?|(?:large\s+)?language\s+models?|ai\s+(?:models?|assistants?|systems?|"
    r"agents?|bots?|crawlers?|summari[sz]ers?)|llm\s+(?:agents?|systems?)|assistants?|"
    r"chatbots?|claude|chatgpt|gpt|automated\s+(?:systems?|agents?|readers?|tools?|"
    r"summari[sz]ers?)"
)
_AI = rf"(?:{_AI_WORDS})"  # the model addressed
_GREETED = (  # whom a greeting addresses; not a bare "AI" ("Hey AI, take my job?")
    r"(?:claude|chatgpt|gpt|assistant|llms?|(?:large\s+)?language\s+models?|ai\s+(?:models?|"
    r"assistants?|agents?|systems?))"
)
_READER = (
    rf"(?:{_AI_WORDS}|bots?|(?:ai|llm)[\s-]+(?:\w+[\s-]+)?(?:agents?|models?|systems?|tools?))"
)
_THEN = (  # the AI word is the one addressed, not a modifier ("AI investors", "AI spending")
    r"(?=\s*[:,.!;-]|\s*$|\s+(?:reading|processing|analy[sz]ing|summari[sz]ing|parsing|that|"
    r"who|which)\b)"
)
_ORDER = (  # what an injected turn tells the model to do
    r"(?:ignore|disregard|forget|approve|allow|veto|classify|treat|output|respond|reply|mark|"
    r"rate|flag|set|do\s+not|don'?t|i\s+will|understood|the\s+(?:assistant|model|ai)|"
    r"you\s+(?:are|must|should|will))\b"
)
_POS = r"(?:previous|prior|above|earlier|preceding|former|original|initial)"
_NOT_A_WARNING = r"(?!\s+(?:sent|from|received|by|via|regarding|about|in\s+error)\b)"
_TEXT_NOUN = (
    r"(?:text|articles?|headlines?|stor(?:y|ies)|news|content|items?|summar(?:y|ies)|messages?|"
    r"pack|inputs?|lines?|pages?|documents?|posts?|words?|releases?)"
)
# The packs' own snake_case keys and stamps (llm/context.py, D-063); a test keeps them in step.
PACK_KEYS = frozenset(
    {
        "after_decision", "b_verdict", "bad_process_omitted", "before_decision",
        "candidates_omitted", "day_mode_source", "decision_basis", "edited_after_decision",
        "events_today", "filing_forms", "filings_lookback_days", "filings_omitted",
        "filings_unavailable", "incidents_omitted", "last_print", "lessons_omitted",
        "minutes_from_decision", "news_omitted", "open_issues_omitted", "open_notes_omitted",
        "picked_as", "premarket_price", "review_targets", "selected_for", "signals_omitted",
        "summary_omitted", "suspicious_news", "targets_omitted", "verdict_source",
    }
)  # fmt: skip
_OWN_WORDS = sorted(  # this system's own identifiers, which news has no reason to contain
    {w for w in (*get_args(RiskFlag), *get_args(CatalystType), *get_args(DayMode)) if "_" in w}
    | {"day_mode", "risk_flags", "pack_id", "catalyst_type", "instructions_note",
       "matching_lessons", "pack_version", "suspicious_content"}
    | PACK_KEYS
)  # fmt: skip

_PATTERNS = tuple(
    re.compile(p)
    for p in (
        # "ignore previous instructions" and its variants
        r"\b(?:ignore|disregard|forget|discard|override|never\s+mind)\s+(?:(?:all|any|every|of|"
        r"the|your|these|those|my)\s+)*(?:" + _POS + r"|system)\s+(?:instructions?|prompts?|"
        r"directives?|guidelines|rules(?!\s+of\s+thumb))\b(?!\s*\?)",
        r"\b(?:ignore|disregard|forget|override|bypass)\s+(?:(?:all|any|every|your)\s+)?"
        + _POS
        + r"\s+(?:directions?|commands?|messages?|context)\b"
        + _NOT_A_WARNING,
        r"\bbypass\s+(?:the|your|all|any)\s+(?:(?:" + _POS + r"|system)\s+)+(?:instructions?|"
        r"prompts?)\b",
        r"\b(?:ignore|disregard|forget|discard|set\s+aside)\s+(?:(?:all|any|every|of|the|your|"
        r"these|those|my)\s+)*(?:(?:instructions?|prompts?|directions?|directives?|commands?)\s+"
        r"(?:above|before|so\s+far|given\s+(?:above|earlier|before)|you\s+(?:were|have\s+been)\s+"
        r"given)|(?:" + _POS + r"\s+)?guidance\s+you\s+(?:were|have\s+been)\s+given)\b",
        r"\b(?:ignore|disregard|forget)\s+(?:all\s+(?:of\s+)?|everything\s+)?the\s+above\b(?![\w-])",
        r"\b(?:ignore|disregard|forget|discard)\b[^.!?]{0,60}?\b(?:all|any|every|your|these)\s+"
        r"(?:"
        + _POS
        + r"|system)\s+(?:instructions?|prompts?|directions?|directives?|commands?)\b",
        r"\b(?:ignore|disregard|forget)\s+(?:all|any|every|your|these|my)\s+(?:instructions?|"
        r"prompts?|directions?|directives?|commands?|guidelines|rules(?!\s+of\s+thumb))\b"
        + _NOT_A_WARNING,
        r"\b(?:ignore|disregard|forget)\s+(?:what|everything|anything)\s+you\s+(?:were|have\s+been)"
        r"\s+told\b",
        r"\bignore\s+(?:everything|anything)\s+(?:above|before|else\b(?!\s*:|\s+you\s+(?:hear|"
        r"read|see)\b))",
        r"\bstop\s+following\s+(?:your|these|all|any|my)\s+(?:instructions?|prompts?|rules|"
        r"guidelines)\b",
        r"\b(?:your|all|any|the)\s+(?:previous|prior|earlier|original|initial)\s+(?:instructions?|"
        r"prompts?)\s+(?:no\s+longer\s+apply|(?:are|is)\s+(?:now\s+)?(?:void|cancell?ed|revoked|"
        r"obsolete|invalid|null)|do\s+not\s+apply)\b",
        # a turn or a role
        r"\b(?:system|assistant|human|user|developer|admin|operator)(?:\s+note)?\s*(?:\*\*|__)?\s*:"
        r"\s*" + _ORDER,
        r"(?:^|[.!?;:|>\])]\s*)(?:\*\*|__|#{1,6}\s*)(?:system|assistant|human|user|developer)\s*"
        r"(?:\*\*|__)?\s*:",
        r"\b(?:human|user)\s*:.{0,500}?\bassistant\s*:",
        r"(?:^|[.!?;:]\s*)system\s+(?:prompt|override)\s*[:-]",
        r"\bsystem\s+message\s*:\s*" + _ORDER,
        r"\b(?:claude|llms?|ai\s+models?|language\s+models?|ai\s+agents?)\s*[,:]\s*" + _ORDER,
        r"\bfor\s+(?:ai|llm|bot|machine)\s+eyes\s+only\b",
        r"\b(?:mark|rate|classify|treat)\s+(?:\$?[a-z]{1,6}|this|it|the\s+\w+)\s+as\s+[\"']?"
        r"(?:allow|veto)\b",
        r"\bday\s+mode\s*:\s*(?:normal|reduced|no[\s_]trade)\b",
        r"\"\s*\}\s*,\s*\{\s*\"",  # a JSON object closed and another opened: breaking out
        # the canonical phrase in other languages (accents are folded away before matching)
        r"\bignora(?:r|d)?\s+(?:todas\s+)?las\s+instrucciones\s+(?:anteriores|previas)\b",
        r"\bignore[zr]?\s+(?:toutes\s+)?les\s+instructions\s+(?:precedentes|anterieures)\b",
        r"\bignorier(?:e|en)?\s+(?:alle\s+)?(?:vorherigen|bisherigen|obigen)\s+anweisungen\b",
        r"\bignor[ae]\s+(?:todas\s+)?as\s+instrucoes\s+anteriores\b",
        "\N{CJK UNIFIED IDEOGRAPH-5FFD}\N{CJK UNIFIED IDEOGRAPH-7565}.{0,8}(?:"
        "\N{CJK UNIFIED IDEOGRAPH-6307}\N{CJK UNIFIED IDEOGRAPH-4EE4}|"
        "\N{CJK UNIFIED IDEOGRAPH-6307}\N{CJK UNIFIED IDEOGRAPH-793A}|"
        "\N{CJK UNIFIED IDEOGRAPH-63D0}\N{CJK UNIFIED IDEOGRAPH-793A})",
        r"[({]\s{0,3}(?:system|assistant)\s{0,3}[)}]\s*" + _ORDER,
        r"</?(?:system|assistant|user|human|instructions?|untrusted|prompt)(?:_prompt|_message)?\b"
        r"(?![.@])",
        r"<\s{1,3}/?\s{0,3}(?:system|assistant|user|human|instructions?|untrusted|prompt)\b"
        r"[^<>]{0,60}>",
        r"\[\s{0,3}/?\s{0,3}(?:system|inst|instructions?|assistant|user|human)\s{0,3}\]",
        r"<\s{0,3}\|[^|]{0,40}\|\s{0,3}>",
        r"<<\s{0,3}/?\s{0,3}sys\s{0,3}>>",
        r"(?:'''|```)\s{0,3}(?:system|assistant|human|user|developer)\b",
        r"#{2,}\s{0,3}(?:instruction|response|system)\s{0,3}:",
        r"(?:^|[.!?;:]\s*)instructions\s*:\s*" + _ORDER,
        # new orders
        r"\byou(?:\s+are|'re)\s+now\s+(?:(?:an?|the|my)\s+(?:\w+\s+){0,2}(?:ai|assistant|model|"
        r"analyst|bot|chatbot|trader|agent|dan|llm)\b|(?:operating\s+)?in\s+(?:\w+\s+)?mode\b|"
        r"acting\b|no\s+longer\b|free\s+(?:from|of)\b|unrestricted\b|jailbroken\b|uncensored\b|"
        r"dan\b)",
        r"\b(?:from\s+now\s+on|henceforth|from\s+this\s+point\s+(?:on|forward)|from\s+here\s+on)"
        r",?\s+you\s+(?:are|will|must|shall|should|only|always|never|approve|allow|veto|answer|"
        r"respond|output|reply)\b",
        r"\byou\s+will\s+now\s+(?:approve|allow|veto|ignore|treat|output|mark|rate|answer|respond|"
        r"reply)\b",
        r"\b(?:you\s+)?(?:do\s+not|don't|must\s+not|should\s+not|never)\s+veto\b",
        r"\b(?:do\s+not|don't|never)\s+flag\s+(?:this|it|these)\b",
        r"\bpretend\s+(?:you\s+are|you're|that\s+you)\b",
        r"(?:^|[.!?;:]\s*)pretend\s+to\s+be\b",
        r"\bpretend\s+to\s+be\s+(?:an?\s+)?(?:" + _AI + r"|unrestricted|unfiltered|jailbroken|dan|"
        r"evil)\b",
        r"\brole-?play\s+as\b",
        r"\bact\s+as\s+(?:an?\s+|the\s+|my\s+)?(?:(?:unfiltered|unrestricted|uncensored|jailbroken|"
        r"evil|rogue|different|helpful|honest)\s+)?" + _AI + r"(?:\s+(?:models?|assistants?|"
        r"agents?|systems?|chatbots?))?(?=\s*[:,.!;]|\s*$|\s+(?:that|who|which|with|without|named|"
        r"called|and)\b)",
        r"(?<!'s )\b(?:new|updated|revised|real|actual|hidden|secret)\s+(?:system\s+)?"
        r"instructions\s*:",
        r"(?<!'s )\b(?:new|updated|revised|hidden|secret)\s+(?:system\s+)?prompt\s*"
        r"(?::|-+(?![a-z0-9]))",
        r"(?<!'s )\b(?:your\s+new\s+task\b|new\s+task\s*:)",
        r"\bnew\s+rules\s*:\s*(?:never|always|approve|allow|veto|ignore|do\s+not|don'?t)\b",
        r"\b(?:here\s+are|follow|obey)\s+(?:your|these|my)\s+(?:new|updated|real|secret|true)\s+"
        r"(?:instructions?|prompts?|orders)\b",
        # words to the model reading this
        r"\b(?:note|message|instructions?|reminder|memo)\s+(?:to|for)\s+(?:the\s+|any\s+|all\s+)?"
        + _AI
        + _THEN,
        r"\b(?:note|message|instructions?|reminder)\s+(?:to|for)\s+(?:the|this|any|all)\s+models?"
        + _THEN,
        r"\b(?:attention|attn)\s*[,:!]?\s+(?:all\s+|any\s+)?" + _READER + r"\s*[:,!]",
        r"\bif\s+you(?:\s+are|'re)\s+an?\s+" + _AI + _THEN,
        r"\b" + _READER + r"\s+(?:reading|processing|analy[sz]ing|summari[sz]ing|parsing)\s+"
        r"(?:this|these)(?=\s+" + _TEXT_NOUN + r"\b|\s+(?:should|must|will|need|please)\b|"
        r"\s*[,.:;!]|\s*$)",
        r"\b(?:hey|hi|hello|dear|ok|okay)\s*,?\s+" + _GREETED + _THEN,
        r"\bjailbreak\s+(?:the|this|your)\s+(?:" + _AI + r"|model)\b",
        r"\bdan\s+mode\b",
        r"\bgod\s+mode\s*(?::|\b(?:on|enabled|activated)\b)",
        r"\b(?:enable|activate)\s+god\s+mode\b",
        # this system's own output
        r"[\\\"']+(?:verdict|day_mode|risk_flags|catalyst_type|pack_id|assessments|confidence|"
        r"suspicious|instructions_note|pack_version|matching_lessons|untrusted|candidates|playbook|"
        r"book_b|headline|summary|source)[\\\"']+\s*:\s*(?:[\\\"'\[{0-9-]|true\b|false\b|null\b)",
        r"[\"']+role[\"']+\s*:\s*[\"']+(?:system|assistant|user|developer|tool|premarket|vet|"
        r"review)[\"']",
        r"\bverdict\s*(?:[:=]|->|\N{EM DASH}|\N{EN DASH})\s*[\"']?(?:allow|veto|"
        r"neutral(?!\s+(?:on|for)\b))\b",
        r"\bverdict\s+(?:is|should\s+be|must\s+be)\s+[\"']?(?:allow|veto)\b",
        # talk of the pack's own framing: "end of untrusted text", "this is not untrusted"
        r"\bend\s+of\s+(?:the\s+)?untrusted\s+(?:text|data|content|section|block|input|part|news|"
        r"items?)\b",
        r"\b(?:is|are|was)\s+not\s+(?:under\s+|in\s+|inside\s+)?(?:an?\s+|the\s+)?untrusted\b",
        r"\bset\s+(?:the\s+|its\s+|your\s+)?verdict\s+(?:to|as)\s+[\"']?(?:allow|veto|neutral)\b",
        r"\b(?:" + "|".join(_OWN_WORDS) + r")\b",
    )
)
# After a line break: a headline may open with "Human:" (a game's name), a turn can't.
_LINE_START_TURN = re.compile(
    r"\n[ \t]*(?:[#>*+(\[\"'-]+[ \t]*)?(?:human|assistant)[ \t]*(?:\*\*)?[ \t]*:"
)
# Spaced-out, punctuated or leetspeak phrases, on letters only.
_COMPACT = re.compile(
    r"(?:ignore|disregard|forget|discard)(?:all|any|every|the|your|of)*(?:previous|prior|above|"
    r"earlier)(?:instructions?|prompts?|directives?)|youarenow(?:an?)?(?:unrestricted|"
    r"jailbroken|uncensored|dan)"
)
_NOT_LETTERS = re.compile(r"[^a-z]")


def _matches(text: str) -> bool:
    folded = _fold(text)
    if _LINE_START_TURN.search(folded):
        return True
    flat = " ".join(folded.split())
    if any(p.search(flat) for p in _PATTERNS):
        return True
    return any(
        _COMPACT.search(_NOT_LETTERS.sub("", form)) for form in (flat, flat.translate(_LEET))
    )


def _decoded(raw: str) -> str | None:
    """``raw`` with entities decoded and invisible characters removed, tags and line breaks
    kept; None if it hasn't settled."""
    text = raw
    for _ in range(_MAX_PASSES):
        step = _visible(html.unescape(_nfkc(text)))
        if step == text:
            return text
        text = step
    return None


def _views(raw: str, clean: str, decoded: str) -> Iterable[str]:
    """The texts ``is_suspicious`` reads."""
    yield clean
    yield decoded
    spaced = _settle(raw, invisible_as=" ")
    if spaced is not None:
        yield _CAMEL.sub(" ", spaced)


def is_suspicious(text: str, *, cap: int = RAW_CAP) -> bool:
    """Whether ``text``, raw or clean, looks like it addresses the model reading it. Text
    longer than ``cap`` is flagged on its length alone."""
    if len(text) > cap:
        return True  # oversized text is no news item; nothing more is read of it
    raw = text
    clean, decoded = _settle(raw), _decoded(raw)
    if clean is None or decoded is None:
        return True  # nor is text whose markup won't settle
    if any(_matches(view) for view in _views(raw, clean, decoded)):
        return True
    smuggled = "".join(chr(ord(c) - 0xE0000) for c in raw if ord(c) in _TAG_CHARS)
    loose = sum(
        ord(c) in _TAG_CHARS and chr(ord(c) - 0xE0000).isalpha() for c in _TAG_FLAG.sub("", raw)
    )
    if loose >= 10 or (smuggled and _matches(smuggled)):
        return True  # a flag emoji's tags spell a few letters; a message needs more
    if "<" in decoded and _parse(decoded).concealed_letters >= 10:
        return True  # text a human reader wouldn't see
    return any(c in _REVERSING for c in raw) and _matches(" ".join(clean.split())[::-1])


# ---------------------------------------------------------------- the pack's form


def untrusted(
    headline: str,
    summary: str,
    limits: TextLimits = DEFAULT_LIMITS,
    *,
    names: Mapping[str, str] | None = None,
    raw_cap: int = RAW_CAP,
) -> NewsText:
    """One news item as a pack carries it (A§8). It is flagged on its whole text (each field
    and the two together) before anything is hidden or cut, so an attempt the cut drops still
    shows. A field longer than ``raw_cap`` is flagged for that alone, and only its first
    ``raw_cap`` characters are read (packs pass a lower cap: D-063). With ``names``, those
    names are hidden before the cut."""
    head, body = normalize(headline[:raw_cap]), normalize(summary[:raw_cap])
    both = f"{headline[:raw_cap]}\n{summary[:raw_cap]}"
    suspicious = (
        is_suspicious(headline, cap=raw_cap)
        or is_suspicious(summary, cap=raw_cap)
        or is_suspicious(both, cap=len(both))
    )
    if names:
        head, body = anonymize(head, names), anonymize(body, names)
    return {
        "untrusted": {"headline": cut(head, limits.headline), "summary": cut(body, limits.summary)},
        "suspicious": suspicious,
    }


def anonymize(text: str, names: Mapping[str, str]) -> str:
    """``text`` with each of ``names`` (exact spelling, after NFKC) replaced by its alias:
    whole words only, the longest name first, aliases never rescanned. Case counts, since
    tickers are words too ("ALL", "A"): pass each spelling to hide (E1)."""
    table = {unicodedata.normalize("NFKC", k): v for k, v in names.items() if k.strip()}
    if not table:
        return text
    pattern = re.compile(
        "|".join(
            rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])"
            for name in sorted(table, key=len, reverse=True)
        )
    )
    return pattern.sub(lambda m: table[m.group(0)], text)
