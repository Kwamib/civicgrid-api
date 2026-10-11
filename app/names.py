"""Leader-name hygiene shared by every write path.

Rules come from the Oct 8, 2026 cleanup: titles leaked into full_name
("Mayor Jane Smith", "Sean Reed, Mayor", "Jessie Bellflowers - Mayor"),
suffixes became last names ("Jr.", "III"), and re-saving the same person
created fake history.
"""

import re
import unicodedata

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# "Hon." only with the period: "Hon" alone is a real given name and surname.
_TITLE = r"(?:mayor|hon\.|honorable|village\s+president|first\s+selectman|town\s+supervisor)"
_LEADING_TITLE = re.compile(rf"^\s*{_TITLE}\s+(?=\S)", re.I)
_TRAILING_TITLE = re.compile(rf"\s*(?:[,\-\u2013\u2014]\s*|\(\s*){_TITLE}\s*\)?\s*$", re.I)
_ONLY_TITLE = re.compile(rf"^\s*{_TITLE}\s*$", re.I)
_ROLE_TITLE = re.compile(
    r"\b(?:vice|deputy|acting|interim|former|assistant)\s+mayor\b|\bmayor\s+pro\s*-?\s*tem\b",
    re.I,
)


class NameNeedsReview(ValueError):
    """The name can't be cleaned safely; a person has to look at it."""


def strip_titles(name: str | None) -> str:
    """Remove leading/trailing chief-executive titles. Never raises."""
    s = " ".join((name or "").split())
    previous = None
    while previous != s:
        previous = s
        s = _LEADING_TITLE.sub("", s)
        s = _TRAILING_TITLE.sub("", s)
    return s.strip()


def clean_full_name(name: str | None) -> str:
    """Strip titles, or raise NameNeedsReview when the text implies another office or is malformed."""
    raw = " ".join((name or "").split())
    if _ROLE_TITLE.search(raw):
        raise NameNeedsReview(
            "Name includes a role title (Vice/Deputy/Acting/Interim/Former/Pro Tem). "
            "Confirm this person is the chief executive, then enter the name without the title."
        )
    cleaned = strip_titles(raw)
    if not cleaned or _ONLY_TITLE.match(cleaned):
        raise NameNeedsReview("Name is empty or only a title.")
    if not cleaned[0].isalnum():
        raise NameNeedsReview(f"Name starts with {cleaned[0]!r}; part of it may be missing.")
    return cleaned


def derive_last_name(full_name: str) -> str:
    """Last name for leaders.last_name (NOT NULL). Skips titles and Jr/Sr/II-IV suffixes."""
    parts = strip_titles(full_name).replace(",", " ").split()
    while len(parts) > 1 and parts[-1].lower().rstrip(".") in NAME_SUFFIXES:
        parts.pop()
    return parts[-1] if parts else (full_name or "").strip()


def norm_name(value: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def same_person(a: str | None, b: str | None) -> bool:
    """Strict match: same name after removing titles, case, accents and punctuation.

    Deliberately does NOT merge nicknames or initials (Jim vs James): a father
    and son can share a surname and a city. Those stay a reviewer's call.
    """
    left, right = norm_name(strip_titles(a)), norm_name(strip_titles(b))
    return bool(left) and left == right


def _spelling_parts(name: str | None) -> tuple[set[str], str]:
    """(single-letter initials, every other letter run together)."""
    tokens = norm_name(strip_titles(name)).split()
    initials = {t for t in tokens if len(t) == 1}
    core = "".join(t for t in tokens if len(t) > 1)
    return initials, core


def same_person_spelling(a: str | None, b: str | None) -> bool:
    """Same person written two ways: a strict match, or a spelling variant.

    A variant has exactly the same letters once spacing inside the name and
    middle initials are ignored, and one name's initials are a subset of the
    other's:
        "Don DeGraff"  == "Don A. De Graff"   (spacing + added initial)
        "Beto Lopez"   == "J. Beto Lopez"     (added leading initial)
    Still different people (a reviewer decides):
        "Jim Smith"    != "James Smith"       (nicknames are never merged)
        "John A. Smith" != "John B. Smith"    (conflicting initials)
        "Frank Scott"  != "Frank Scott Jr."   (a suffix can mean father and son)

    Added after approvals on Oct 9-10, 2026 recorded the same mayor as a new
    mayor (Lee's Summit MO, South Holland IL), creating fake history.
    """
    if same_person(a, b):
        return True
    initials_a, core_a = _spelling_parts(a)
    initials_b, core_b = _spelling_parts(b)
    if not core_a or core_a != core_b:
        return False
    return initials_a <= initials_b or initials_b <= initials_a
