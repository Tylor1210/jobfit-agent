"""Turn job-board HTML into readable plain text, and pull a pay range out of free text."""

from __future__ import annotations

import html
import re

_BLOCK = re.compile(r"</?(p|div|h[1-6]|ul|ol|section|article|table|tr)\b[^>]*>", re.I)
_BR = re.compile(r"<br\s*/?>", re.I)
_LI = re.compile(r"<li\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


_MOJIBAKE = ("\u00e2\u0080", "\u00c3\u00a9", "\u00c3\u00b1", "\u00e2\u20ac")


def fix_mojibake(t: str) -> str:
    """Undo UTF-8 text that was decoded as Latin-1 upstream ("â\x80\x93" -> "\u2013").
    Remote OK's API does this. Leaves text alone unless the round trip is clean."""
    if not any(m in t for m in _MOJIBAKE):
        return t
    for enc in ("latin-1", "cp1252"):
        try:
            return t.encode(enc).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return t


def html_to_text(raw: str | None) -> str:
    """Keep paragraph and list structure; drop everything else. Handles Greenhouse's
    double-escaped HTML (the API returns `&lt;p&gt;` rather than `<p>`)."""
    if not raw:
        return ""
    t = fix_mojibake(raw)
    if "&lt;" in t and "<" not in t:
        t = html.unescape(t)
    t = _SCRIPT.sub(" ", t)
    t = _LI.sub("\n- ", t)
    t = _BR.sub("\n", t)
    t = _BLOCK.sub("\n", t)
    t = _TAG.sub("", t)
    t = html.unescape(t).replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


_MONEY = r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?"
_PAY = re.compile(
    rf"({_MONEY})(?:\s*/\s*(?:hr|hour))?\s*(?:-|\u2013|\u2014|to)\s*({_MONEY})(?:\s*(?:/|per)\s*(hour|hr|year|yr|annually))?",
    re.I,
)


def extract_pay(text: str) -> str | None:
    """First "$X - $Y" range in the text, normalized to use "to" (no dashes in stored text)."""
    m = _PAY.search(text)
    if not m:
        return None
    lo, hi, unit = (g.replace(" ", "") if g else g for g in m.groups())
    suffix = {"hour": "/hr", "hr": "/hr", "year": "/yr", "yr": "/yr", "annually": "/yr"}.get(
        (unit or "").lower(), ""
    )
    return f"{lo} to {hi}{suffix}"


def format_range(
    lo: float | None, hi: float | None, unit: str = "", currency: str = "USD"
) -> str | None:
    if not lo and not hi:
        return None
    sym = "$" if currency.upper() == "USD" else f"{currency.upper()} "

    def f(v: float) -> str:
        return f"{sym}{v:,.0f}"

    if lo and hi and lo != hi:
        return f"{f(lo)} to {f(hi)}{unit}"
    return f"{f(lo or hi or 0)}{unit}"
