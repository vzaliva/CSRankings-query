#!/usr/bin/env python3
# /// script
# dependencies = [
#   "requests",
#   "pandas",
#   "rapidfuzz",
# ]
# ///

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from rapidfuzz import fuzz, process


CACHE_DIR = Path.home() / ".cache" / "csrankings"
CACHE_TTL_SECONDS = 24 * 60 * 60
FACULTY_CSV_URLS = [
    f"https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/csrankings-{letter}.csv"
    for letter in "abcdefghijklmnopqrstuvwxyz"
]
VENUE_MAPPING_URL = (
    "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/generated/venue_to_area.json"
)
CSRANKINGS_TS_URL = (
    "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/csrankings.ts"
)
AUTHOR_INFO_URL = (
    "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/generated/author-info.json"
)
AUTHOR_INFO_CSV_URL = (
    "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/generated-author-info.csv"
)
INSTITUTIONS_URL = (
    "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/institutions.csv"
)
COUNTRIES_URL = (
    "https://raw.githubusercontent.com/emeryberger/CSrankings/gh-pages/countries.csv"
)
ALL_KNOWN_URLS = FACULTY_CSV_URLS + [
    VENUE_MAPPING_URL,
    CSRANKINGS_TS_URL,
    AUTHOR_INFO_URL,
    AUTHOR_INFO_CSV_URL,
    INSTITUTIONS_URL,
    COUNTRIES_URL,
]
REQUEST_TIMEOUT_SECONDS = 30


class FetchError(RuntimeError):
    """Raised when fetching a required URL fails."""

    def __init__(self, url: str, message: str) -> None:
        super().__init__(message)
        self.url = url


def warn(message: str) -> None:
    print(message, file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query CSRankings publication counts for a university, filtered by conference venues."
        )
    )
    parser.add_argument("university_name", help="Free-text university name to fuzzy-match")
    parser.add_argument(
        "--conferences",
        required=True,
        help="Comma-separated conference venue names, e.g. NeurIPS,ICML,ICLR",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit JSON to stdout instead of plain text",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Delete cached responses and fetch everything again",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=50.0,
        help="Minimum fuzzy match score from 0 to 100 (default: 50)",
    )
    parser.add_argument(
        "--country",
        help="Optional country filter, as ISO alpha-2 code or country name, e.g. US or Switzerland",
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        help="Optional maximum number of affiliation matches to allow before failing as ambiguous",
    )
    args = parser.parse_args()
    if not 0 <= args.threshold <= 100:
        parser.error("--threshold must be between 0 and 100")
    if args.max_matches is not None and args.max_matches < 1:
        parser.error("--max-matches must be at least 1")
    args.conferences = [item.strip() for item in args.conferences.split(",") if item.strip()]
    if not args.conferences:
        parser.error("--conferences must contain at least one conference name")
    return args


def normalize_name(value: str) -> str:
    return value.casefold().strip()


def affiliation_acronym(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", value).strip()
    if not cleaned:
        return ""

    tokens = [token for token in cleaned.split() if token]
    if len(tokens) <= 1:
        return cleaned.casefold()

    initials = "".join(token[0].casefold() for token in tokens if token[0].isalnum())
    return initials


def affiliation_tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[^0-9A-Za-z]+", value.casefold()) if token]


def affiliation_match_score(query: str, affiliation: str, **_: Any) -> float:
    normalized_query = normalize_name(query)
    normalized_affiliation = normalize_name(affiliation)
    query_tokens = affiliation_tokens(query)
    affiliation_token_list = affiliation_tokens(affiliation)
    affiliation_token_set = set(affiliation_token_list)

    if normalized_query and normalized_query == affiliation_acronym(affiliation):
        return 100.0

    if normalized_query == normalized_affiliation:
        return 100.0

    if query_tokens:
        if len(query_tokens) == 1 and query_tokens[0] in affiliation_token_set:
            return 95.0
        if set(query_tokens).issubset(affiliation_token_set):
            return 95.0
        if len(query_tokens) == 1:
            token = query_tokens[0]
            if len(token) >= 4 and any(candidate.startswith(token) for candidate in affiliation_token_list):
                return 88.0
            comparable_tokens = [
                candidate
                for candidate in affiliation_token_list
                if len(candidate) >= max(4, len(token) - 1) and abs(len(candidate) - len(token)) <= 2
            ]
            if comparable_tokens:
                best_token_score = max(
                    float(fuzz.ratio(token, candidate)) for candidate in comparable_tokens
                )
                if best_token_score >= 70.0:
                    return best_token_score
            return float(fuzz.ratio(token, " ".join(affiliation_token_list))) * 0.45

    if len(normalized_query) <= 4 and " " not in normalized_query:
        return float(fuzz.ratio(normalized_query, normalized_affiliation)) * 0.5

    token_overlap = len(set(query_tokens) & affiliation_token_set)
    if query_tokens and token_overlap > 0:
        coverage = token_overlap / len(set(query_tokens))
        base_score = float(fuzz.ratio(normalized_query, normalized_affiliation))
        if len(query_tokens) > 1 and token_overlap == 1:
            return base_score * 0.8
        return max(
            base_score,
            35.0 + 20.0 * coverage,
        )

    return float(fuzz.ratio(normalized_query, normalized_affiliation))


def cache_path_for_url(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.cache"


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def clear_caches(urls: list[str]) -> None:
    ensure_cache_dir()
    for url in urls:
        cache_path = cache_path_for_url(url)
        if cache_path.exists():
            cache_path.unlink()


def is_cache_fresh(path: Path) -> bool:
    age = time.time() - path.stat().st_mtime
    return age < CACHE_TTL_SECONDS


def fetch_text(
    session: requests.Session,
    url: str,
    required: bool,
    warn_on_failure: bool = True,
) -> str | None:
    ensure_cache_dir()
    cache_path = cache_path_for_url(url)

    if cache_path.exists() and is_cache_fresh(cache_path):
        return cache_path.read_text(encoding="utf-8")

    stale_text: str | None = None
    if cache_path.exists():
        stale_text = cache_path.read_text(encoding="utf-8")

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        if stale_text is not None:
            if warn_on_failure:
                warn(f"Warning: failed to refresh {url}; using stale cache")
            return stale_text
        if required:
            raise FetchError(url, f"Failed to fetch required URL: {url}") from exc
        if warn_on_failure:
            warn(f"Warning: failed to fetch {url}; skipping")
        return None

    cache_path.write_text(response.text, encoding="utf-8")
    return response.text


def load_faculty_affiliations(session: requests.Session) -> list[str]:
    affiliations: set[str] = set()

    for url in FACULTY_CSV_URLS:
        csv_text = fetch_text(session, url, required=False)
        if not csv_text:
            continue
        try:
            frame = pd.read_csv(io.StringIO(csv_text))
        except Exception as exc:  # pragma: no cover - defensive parse handling
            warn(f"Warning: could not parse faculty CSV from {url}: {exc}")
            continue

        if "affiliation" not in frame.columns:
            warn(f"Warning: CSV {url} is missing an affiliation column; skipping")
            continue

        for value in frame["affiliation"].dropna().astype(str):
            cleaned = value.strip()
            if cleaned:
                affiliations.add(cleaned)

    return sorted(affiliations)


def load_author_info(session: requests.Session) -> dict[str, Any]:
    json_text = fetch_text(session, AUTHOR_INFO_URL, required=False, warn_on_failure=False)
    if json_text is not None:
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise FetchError(AUTHOR_INFO_URL, "Failed to parse required author-info.json") from exc
        if not isinstance(payload, dict):
            raise FetchError(AUTHOR_INFO_URL, "author-info.json did not contain the expected object")
        return payload

    try:
        csv_text = fetch_text(session, AUTHOR_INFO_CSV_URL, required=True, warn_on_failure=False)
    except FetchError as exc:
        raise FetchError(
            AUTHOR_INFO_CSV_URL,
            "Failed to fetch author publication data; neither author-info.json nor generated-author-info.csv was available",
        ) from exc
    assert csv_text is not None
    return load_author_info_from_csv(csv_text)


def load_author_info_from_csv(csv_text: str) -> dict[str, Any]:
    try:
        frame = pd.read_csv(io.StringIO(csv_text))
    except Exception as exc:
        raise FetchError(AUTHOR_INFO_CSV_URL, "Failed to parse generated-author-info.csv") from exc

    required_columns = {"name", "dept", "area", "count"}
    if not required_columns.issubset(frame.columns):
        raise FetchError(
            AUTHOR_INFO_CSV_URL,
            "generated-author-info.csv is missing one or more required columns",
        )

    author_info: dict[str, Any] = {}
    for row in frame.itertuples(index=False):
        name = str(getattr(row, "name", "")).strip()
        affiliation = str(getattr(row, "dept", "")).strip()
        venue_name = str(getattr(row, "area", "")).strip()
        count_value = coerce_count(getattr(row, "count", 0))

        if not name or not affiliation or not venue_name:
            continue

        record = author_info.setdefault(
            name,
            {
                "affiliation": affiliation,
                "count": {},
            },
        )
        record["affiliation"] = affiliation
        record["count"][venue_name] = record["count"].get(venue_name, 0) + count_value

    return author_info


def extract_areadict_object(ts_text: str) -> str | None:
    match = re.search(r"\bareadict\b(?:\s*:\s*[^=]+)?\s*=\s*{", ts_text)
    if not match:
        return None

    start = ts_text.find("{", match.start())
    if start == -1:
        return None

    depth = 0
    in_string = False
    string_delim = ""
    escaped = False

    for index in range(start, len(ts_text)):
        char = ts_text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string_delim:
                in_string = False
            continue

        if char in {"'", '"'}:
            in_string = True
            string_delim = char
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return ts_text[start : index + 1]

    return None


def parse_areadict_venues(ts_text: str) -> set[str]:
    object_text = extract_areadict_object(ts_text)
    if not object_text:
        return set()

    venues: set[str] = set()
    array_pattern = re.compile(
        r'["\'][^"\']+["\']\s*:\s*\[(.*?)\]',
        re.DOTALL,
    )
    value_pattern = re.compile(r'["\']([^"\']+)["\']')

    for array_match in array_pattern.finditer(object_text):
        for venue_match in value_pattern.finditer(array_match.group(1)):
            venue = venue_match.group(1).strip()
            if venue:
                venues.add(venue)

    return venues


def load_venue_mapping(session: requests.Session) -> set[str]:
    venues: set[str] = set()

    json_text = fetch_text(session, VENUE_MAPPING_URL, required=False, warn_on_failure=False)
    if json_text:
        try:
            payload = json.loads(json_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            venues.update(str(key).strip() for key in payload.keys() if str(key).strip())
        elif isinstance(payload, list):
            venues.update(str(item).strip() for item in payload if str(item).strip())

    if venues:
        return venues

    ts_text = fetch_text(session, CSRANKINGS_TS_URL, required=False, warn_on_failure=False)
    if not ts_text:
        return set()

    parsed = parse_areadict_venues(ts_text)
    if not parsed:
        warn("Warning: could not parse areadict from csrankings.ts")
    return parsed


def load_institution_country_data(
    session: requests.Session,
) -> tuple[dict[str, str], dict[str, str]]:
    institution_text = fetch_text(session, INSTITUTIONS_URL, required=False, warn_on_failure=False)
    country_text = fetch_text(session, COUNTRIES_URL, required=False, warn_on_failure=False)

    institution_to_country: dict[str, str] = {}
    country_names: dict[str, str] = {}

    if institution_text:
        frame = pd.read_csv(io.StringIO(institution_text))
        if {"institution", "countryabbrv"}.issubset(frame.columns):
            for row in frame.to_dict("records"):
                institution = str(row.get("institution", "")).strip()
                country = str(row.get("countryabbrv", "")).strip().lower()
                if institution and country:
                    institution_to_country[institution] = country

    if country_text:
        frame = pd.read_csv(io.StringIO(country_text))
        columns = {column.lstrip("\ufeff"): column for column in frame.columns}
        name_column = columns.get("name")
        alpha2_column = columns.get("alpha_2")
        if name_column and alpha2_column:
            for row in frame.to_dict("records"):
                name = str(row.get(name_column, "")).strip()
                alpha2 = str(row.get(alpha2_column, "")).strip().lower()
                if name and alpha2:
                    country_names[alpha2] = name

    return institution_to_country, country_names


def resolve_country_filter(country_value: str, country_names: dict[str, str]) -> tuple[str | None, str]:
    normalized = normalize_name(country_value)
    if len(normalized) == 2 and normalized.isalpha():
        return normalized, normalized.upper()

    by_name = {normalize_name(name): code for code, name in country_names.items()}
    code = by_name.get(normalized)
    if code:
        return code, country_names.get(code, code.upper())
    return None, country_value


def coerce_count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def build_count_key_lookup(author_info: dict[str, Any]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for record in author_info.values():
        if not isinstance(record, dict):
            continue
        counts = record.get("count", {})
        if not isinstance(counts, dict):
            continue
        for venue in counts.keys():
            venue_name = str(venue).strip()
            if not venue_name:
                continue
            lookup.setdefault(normalize_name(venue_name), venue_name)
    return lookup


def resolve_conferences(
    requested: list[str],
    count_lookup: dict[str, str],
    venue_mapping: set[str],
) -> tuple[list[str], set[str]]:
    venue_mapping_lookup = {
        normalize_name(name): name for name in venue_mapping if isinstance(name, str) and name.strip()
    }
    canonical_names: list[str] = []
    selected_norms: set[str] = set()

    for conference in requested:
        normalized = normalize_name(conference)
        if normalized in count_lookup:
            canonical = venue_mapping_lookup.get(normalized, conference)
            selected_norms.add(normalized)
        else:
            canonical = venue_mapping_lookup.get(normalized, conference)
            warn(f'Unknown conference: "{conference}" - check spelling')
        canonical_names.append(canonical)

    return canonical_names, selected_norms


def compute_scores(
    author_info: dict[str, Any],
    selected_conferences: set[str],
) -> dict[str, int]:
    scores: dict[str, int] = defaultdict(int)

    for record in author_info.values():
        if not isinstance(record, dict):
            continue
        affiliation = str(record.get("affiliation", "")).strip()
        if not affiliation:
            continue
        counts = record.get("count", {})
        if not isinstance(counts, dict):
            continue

        total = 0
        for venue, value in counts.items():
            if normalize_name(str(venue)) in selected_conferences:
                total += coerce_count(value)

        if total > 0:
            scores[affiliation] += total

    return dict(scores)


def rank_by_score(scores: dict[str, int]) -> tuple[dict[int, int], int]:
    sorted_scores = sorted(scores.values(), reverse=True)
    rank_lookup: dict[int, int] = {}
    for position, score in enumerate(sorted_scores, start=1):
        rank_lookup.setdefault(score, position)
    return rank_lookup, len(sorted_scores)


def match_affiliations(
    query: str,
    affiliations: list[str],
    threshold: float,
) -> list[tuple[str, float]]:
    matches = process.extract(
        query,
        affiliations,
        limit=None,
        scorer=affiliation_match_score,
    )
    filtered = [(choice, float(score)) for choice, score, _ in matches if float(score) >= threshold]
    filtered.sort(key=lambda item: (-item[1], item[0]))
    return filtered


def render_plain_text(results: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for result in results:
        header_lines = [f'Query:        "{result["query"]}"']
        if result.get("country_filter"):
            header_lines.append(f'Country:      {result["country_filter"]}')

        matched_line = (
            f'Matched:      {result["matched_institution"]}  '
            f'(match score: {result["match_score"]:.1f})'
        )
        if result.get("low_confidence"):
            matched_line += "  low confidence"

        if result["global_rank"] is None:
            rank_line = "Global rank:  -"
        else:
            rank_line = (
                f'Global rank:  {result["global_rank"]} '
                f'of {result["total_institutions"]} institutions'
            )
        blocks.append(
            "\n".join(
                header_lines
                + [
                    matched_line,
                    f'Conferences:  {", ".join(result["conferences"])}',
                    f'Publications: {result["publications"]}',
                    rank_line,
                ]
            )
        )
    return "\n\n".join(blocks)


def main() -> int:
    args = parse_args()

    if args.refresh:
        clear_caches(ALL_KNOWN_URLS)

    session = requests.Session()
    session.headers.update({"User-Agent": "csrankings-query/1.0"})

    try:
        author_info = load_author_info(session)
    except FetchError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    affiliations = load_faculty_affiliations(session)
    if not affiliations:
        warn("Warning: no faculty CSV affiliations were available; falling back to author-info affiliations")
        affiliations = sorted(
            {
                str(record.get("affiliation", "")).strip()
                for record in author_info.values()
                if isinstance(record, dict) and str(record.get("affiliation", "")).strip()
            }
        )

    venue_mapping = load_venue_mapping(session)
    institution_to_country, country_names = load_institution_country_data(session)
    count_lookup = build_count_key_lookup(author_info)
    canonical_conferences, selected_conferences = resolve_conferences(
        requested=args.conferences,
        count_lookup=count_lookup,
        venue_mapping=venue_mapping,
    )

    country_code: str | None = None
    country_label: str | None = None
    if args.country:
        country_code, country_label = resolve_country_filter(args.country, country_names)
        if not country_code:
            print(f'Unknown country: "{args.country}"', file=sys.stderr)
            return 1
        filtered_affiliations = [
            affiliation
            for affiliation in affiliations
            if institution_to_country.get(affiliation) == country_code
        ]
        if not filtered_affiliations:
            print(
                f'No institutions available for country filter "{country_label}"',
                file=sys.stderr,
            )
            return 1
        affiliations = filtered_affiliations

    matches = match_affiliations(
        query=args.university_name,
        affiliations=affiliations,
        threshold=args.threshold,
    )
    if not matches:
        print(
            f'No affiliation matched "{args.university_name}" with threshold {args.threshold:.1f}',
            file=sys.stderr,
        )
        return 1

    if args.max_matches is not None and len(matches) > args.max_matches:
        matched_names = ", ".join(name for name, _ in matches)
        print(
            (
                f'Too many matches ({len(matches)}) found for "{args.university_name}"; '
                f"refine the name, raise --threshold, or add --country. "
                f"Matches: {matched_names}"
            ),
            file=sys.stderr,
        )
        return 1

    best_score = matches[0][1]
    if args.threshold <= best_score < 80:
        matched_names = ", ".join(name for name, _ in matches)
        warn(
            f'Warning: low-confidence affiliation matches for "{args.university_name}": {matched_names}'
        )

    institution_scores = compute_scores(author_info, selected_conferences)
    rank_lookup, total_institutions = rank_by_score(institution_scores)

    results: list[dict[str, Any]] = []
    for matched_institution, score in matches:
        publications = institution_scores.get(matched_institution, 0)
        result: dict[str, Any] = {
            "query": args.university_name,
            "matched_institution": matched_institution,
            "match_score": round(score, 1),
            "conferences": canonical_conferences,
            "publications": publications,
            "global_rank": rank_lookup.get(publications) if publications > 0 else None,
            "total_institutions": total_institutions,
        }
        if country_label:
            result["country_filter"] = country_label
        if score < 80:
            result["low_confidence"] = True
        results.append(result)

    if args.emit_json:
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(render_plain_text(results))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
