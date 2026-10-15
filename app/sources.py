"""Is a Fix-a-city source page plausibly the city's own official site?

A Leader save records a MANUAL verification with the source URL as its
evidence, so the source must be the city's own page. Oct 15, 2026: 12 of 378
manual saves cited another city or a non-government site, e.g. Thomasville GA
"verified" from rose.net (an internet provider, the wrong website then on
file) and Red Bank TN from cityofgaffney-sc.gov (Gaffney, South Carolina).

The rule, checked before a chief-executive save:
  - .<this state>.us                         accepted
  - .<another state>.us                      needs confirmation
  - .gov / .gov.pr whose host contains the city's name, or the host of the
    city's stored website                    accepted
  - any other .gov                           needs confirmation
  - .org, .com, .net ... on the city's stored website whose address
    contains the city's name (thomasville.org)  accepted
  - anything else                            needs confirmation

"Needs confirmation" means the reviewer ticks that they checked the page is
the city's own; it is a pause, not a ban (kcmo.gov, lacity.gov, dsm.city).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Words that are part of a place's legal name but rarely of its web address.
_PLACE_WORDS = re.compile(r"\b(city|town|township|village|borough|municipality|of)\b")
_STATE_US = re.compile(r"\.([a-z]{2})\.us$")


def _host(url: str | None) -> str:
    if not url:
        return ""
    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    host = (urlparse(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _name_keys(city: str) -> list[str]:
    """'Fort Wayne' -> ['fortwayne']; 'Barnstable Township' -> ['barnstable'];
    'St. Johns' -> ['stjohns', 'saintjohns']."""
    base = _PLACE_WORDS.sub(" ", city.lower().replace("&", " "))
    keys = {_letters(base)}
    if re.match(r"\s*st\.?\s", base):
        keys.add(_letters(re.sub(r"^\s*st\.?\s", "saint ", base)))
    return [k for k in keys if len(k) >= 3]


def source_problem(
    source: str | None, city: str, state_code: str, city_url: str | None
) -> str | None:
    """None if the source looks like the city's own site, else a short reason."""
    host = _host(source)
    if not host or "." not in host:
        return "the source is not a valid web address"
    m = _STATE_US.search(host)
    if m:
        if m.group(1) == state_code.lower():
            return None
        return f"{host} is a .{m.group(1)}.us address, not {state_code}"
    named = any(k in _letters(host) for k in _name_keys(city))
    site = _host(city_url)
    on_site = bool(site) and (host == site or host.endswith("." + site))
    if host.endswith(".gov") or host.endswith(".gov.pr"):
        if named or on_site:
            return None
        return f"{host} doesn't look like {city}'s own site"
    # Not a government domain: fine only when it is the stored website AND the
    # address names the city, so a wrong stored website (rose.net) still asks.
    if named and on_site:
        return None
    return f"{host} is not a .gov or .{state_code.lower()}.us address"
