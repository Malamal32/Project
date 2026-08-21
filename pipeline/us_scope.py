"""Best-effort US-scope classification from Greenhouse's free-text location string.

The base Greenhouse job-board API gives no structured country/state field — just a
free-text `location.name` (e.g. "US-Remote", "San Francisco, CA", "Dublin, Ireland").
This module is a provisional heuristic, not a geocoder: it favors precision over
recall (an undetermined location is excluded, not guessed into scope) and always
records *why* a decision was made in `reason`, so exclusions are visible rather than
silently dropped. Revisit with a real geocoding source if recall matters more later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

US_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

# Cities whose country is unambiguous even without a state qualifier. Deliberately
# excludes names that collide with a non-US place of the same name (Dublin, Cambridge,
# Birmingham, Richmond, Portland-vs-Portland is fine since both are US) — those are
# resolved by the explicit "City, ST" pattern or the non-US marker check instead.
UNAMBIGUOUS_US_CITIES = {
    "san francisco": "CA", "sf": "CA", "new york": "NY", "new york city": "NY", "nyc": "NY",
    "seattle": "WA", "sea": "WA", "chicago": "IL", "chi": "IL", "austin": "TX",
    "denver": "CO", "atlanta": "GA", "los angeles": "CA", "la": "CA", "boston": "MA",
    "miami": "FL", "houston": "TX", "dallas": "TX", "philadelphia": "PA", "phoenix": "AZ",
    "san diego": "CA", "detroit": "MI", "minneapolis": "MN", "charlotte": "NC",
    "nashville": "TN", "pittsburgh": "PA", "san jose": "CA", "oakland": "CA",
    "sacramento": "CA", "salt lake city": "UT", "raleigh": "NC", "orlando": "FL",
    "tampa": "FL", "las vegas": "NV", "washington dc": "DC", "dc": "DC",
}

NON_US_MARKERS = {
    "canada", "toronto", "vancouver", "montreal", "united kingdom", "uk", "london",
    "dublin", "ireland", "france", "paris", "germany", "berlin", "munich", "india",
    "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune", "singapore",
    "australia", "sydney", "melbourne", "netherlands", "amsterdam", "mexico",
    "mexico city", "brazil", "sao paulo", "japan", "tokyo", "china", "shanghai",
    "beijing", "spain", "madrid", "barcelona", "poland", "warsaw", "israel",
    "tel aviv", "philippines", "manila", "colombia", "bogota", "argentina",
    "costa rica", "romania", "bucharest", "portugal", "lisbon", "sweden",
    "stockholm", "switzerland", "zurich", "belgium", "brussels", "italy", "milan",
    "rome", "new zealand", "auckland", "korea", "seoul", "taiwan", "taipei",
    "hong kong", "vietnam", "indonesia", "jakarta", "egypt", "cairo", "nigeria",
    "kenya", "nairobi", "south africa", "cape town", "johannesburg",
}

# The trailing (?!-) guards against Greenhouse's other convention, a comma-joined
# list of "XX-City" country-code segments (e.g. "CA-Toronto, CA-Montreal"): without
# it, "CA-Toronto, CA-Montreal" would greedily capture group1="CA-Toronto" and
# group2="CA" from the *next* segment's leading country code, misreading Canada's
# "CA-" prefix as California's trailing state code.
_CITY_STATE_RE = re.compile(r"([A-Za-z .'-]+),\s*([A-Z]{2})(?!-)\b")
# Requires the code to be all-caps, matching Greenhouse's actual convention
# ("US-", "CA-"). A case-insensitive version also matches plain English phrases
# like "In-Office" or "On-Site" as if "In"/"On" were two-letter place codes.
_LEADING_SEGMENT_PREFIX_RE = re.compile(r"^([A-Z]{2})-")


@dataclass(frozen=True)
class LocationClassification:
    is_us: bool
    reason: str
    is_remote: bool = False
    location_city: Optional[str] = None
    location_state: Optional[str] = None
    location_country: Optional[str] = None


def classify_location(location_raw: Optional[str]) -> LocationClassification:
    if not location_raw or not location_raw.strip():
        return LocationClassification(is_us=False, reason="no_location_data")

    lowered = location_raw.lower()
    is_remote = "remote" in lowered

    # Greenhouse's "XX-City[, XX-City, ...]" prefix convention is structurally
    # ambiguous on its own: "CA-" could mean California (a US state) or Canada (a
    # country) depending on the company. Resolve using the city that follows the
    # hyphen, not the code alone — "CA-Toronto" excludes (Toronto is a known non-US
    # marker), "CA-San Francisco" includes (California, nothing contradicts it).
    for segment in re.split(r"[,;]", location_raw):
        seg = segment.strip()
        prefix_match = _LEADING_SEGMENT_PREFIX_RE.match(seg)
        if not prefix_match:
            continue
        code = prefix_match.group(1).upper()
        if code == "US":
            return LocationClassification(is_us=True, reason="us_prefix_segment", is_remote=is_remote, location_country="US")
        city_part = seg[prefix_match.end():].strip().lower()
        if any(marker in city_part for marker in NON_US_MARKERS):
            return LocationClassification(is_us=False, reason=f"non_us_prefix_segment:{code.lower()}")
        if code in US_STATE_ABBR:
            return LocationClassification(
                is_us=True, reason=f"state_prefix_segment:{code}", is_remote=is_remote,
                location_state=code, location_country="US",
            )
        # Unrecognized prefix with no city cue either way — don't guess from the
        # code alone; fall through to the rest of the checks (e.g. the full-string
        # non-US marker scan below) instead of returning here.
        break

    match = _CITY_STATE_RE.search(location_raw)
    if match and match.group(2) in US_STATE_ABBR:
        return LocationClassification(
            is_us=True,
            reason=f"city_state_pattern:{match.group(2)}",
            is_remote=is_remote,
            location_city=match.group(1).strip(),
            location_state=match.group(2),
            location_country="US",
        )

    if "united states" in lowered:
        return LocationClassification(is_us=True, reason="country_name_match", is_remote=is_remote, location_country="US")
    if re.search(r"\busa\b", lowered):
        return LocationClassification(is_us=True, reason="country_abbreviation_match:usa", is_remote=is_remote, location_country="US")

    for marker in NON_US_MARKERS:
        if marker in lowered:
            return LocationClassification(is_us=False, reason=f"non_us_location_match:{marker}")

    # Standalone "US" token (case-sensitive to avoid matching the pronoun "us"),
    # e.g. "Remote - US: Select locations", "US (Remote)". Checked after the non-US
    # marker scan so "US Virgin Islands"-style strings still hit that path first
    # only when they contain an actual non-US marker; a bare "US" otherwise wins.
    if re.search(r"\bUS\b", location_raw):
        return LocationClassification(is_us=True, reason="us_token_match", is_remote=is_remote, location_country="US")

    for city, state in UNAMBIGUOUS_US_CITIES.items():
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return LocationClassification(
                is_us=True,
                reason=f"unambiguous_city_match:{city}",
                is_remote=is_remote,
                location_city=city.title(),
                location_state=state,
                location_country="US",
            )

    tokens = re.split(r"[^A-Za-z]+", location_raw)
    for tok in tokens:
        if tok in US_STATE_ABBR:
            return LocationClassification(
                is_us=True, reason=f"state_abbreviation_token:{tok}", is_remote=is_remote,
                location_state=tok, location_country="US",
            )

    if is_remote:
        return LocationClassification(is_us=False, reason="ambiguous_remote_no_country_cue")

    return LocationClassification(is_us=False, reason="undetermined_scope")
