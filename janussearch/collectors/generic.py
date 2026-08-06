#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch all indexed papers for a venue-year target (for example: AAAI-26, CVPR-26)
and export normalized metadata to JSON.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

OPENALEX_BASE_URL = "https://api.openalex.org"
OPENREVIEW_BASE_URLS = (
    "https://api2.openreview.net",
    "https://api.openreview.net",
)
DEFAULT_TIMEOUT = 45.0
DEFAULT_RETRIES = 4
PER_PAGE = 200
VALID_PRESENTATION_LEVELS = {"poster", "oral", "bestpaper"}
OPENREVIEW_LOW_COUNT_THRESHOLD = 100
WORKS_SELECT_FIELDS = (
    "id,title,display_name,publication_year,doi,"
    "authorships,abstract_inverted_index,keywords,concepts"
)

OPENREVIEW_VENUE_ID_PATTERNS: Dict[str, List[str]] = {
    # Verified pattern from OpenReview docs examples.
    "NEURIPS": ["NeurIPS.cc/{year}/Conference"],
    "ICLR": ["ICLR.cc/{year}/Conference"],
    "ICML": ["ICML.cc/{year}/Conference"],
}

TRACK_MAIN = "main"
TRACK_WORKSHOP = "workshop"
TRACK_DATASETS_BENCHMARKS = "datasets_benchmarks"
TRACK_COMPETITION = "competition"
TRACK_OTHER = "other"
TRACK_VALUES = (
    TRACK_MAIN,
    TRACK_WORKSHOP,
    TRACK_DATASETS_BENCHMARKS,
    TRACK_COMPETITION,
    TRACK_OTHER,
)
TRACK_GROUP_MAIN = "main"
TRACK_GROUP_OTHER = "other"
TRACK_GROUP_VALUES = (TRACK_GROUP_MAIN, TRACK_GROUP_OTHER)

NEURIPS_VIRTUAL_TRACKS_JSON_URL = (
    "https://neurips.cc/static/virtual/data/neurips-{year}-orals-posters.json"
)

NOISE_ANCHOR_TEXTS = {
    "login",
    "logout",
    "register",
    "sign in",
    "sign up",
    "home",
    "schedule",
    "poster",
    "oral",
    "spotlight",
    "paper",
    "papers",
    "pdf",
    "openreview",
    "arxiv",
    "video",
    "project",
    "website",
    "code",
    "github",
    "slides",
    "supplementary",
    "details",
    "author",
    "authors",
}
NOISE_VIRTUAL_CATEGORIES = {
    "session",
    "town-hall",
    "town_hall",
    "townhall",
}

# Use known aliases when possible, then fallback to searching with the short code.
VENUE_SEARCH_TERMS: Dict[str, List[str]] = {
    "AAAI": [
        "Proceedings of the AAAI Conference on Artificial Intelligence",
        "AAAI Conference on Artificial Intelligence",
    ],
    "ACL": [
        "Annual Meeting of the Association for Computational Linguistics",
        "ACL",
    ],
    "CVPR": [
        "IEEE/CVF Conference on Computer Vision and Pattern Recognition",
        "Conference on Computer Vision and Pattern Recognition",
    ],
    "ECCV": ["European Conference on Computer Vision"],
    "EMNLP": ["Conference on Empirical Methods in Natural Language Processing"],
    "ICCV": ["IEEE International Conference on Computer Vision"],
    "ICLR": ["International Conference on Learning Representations"],
    "ICML": ["International Conference on Machine Learning"],
    "IJCAI": ["International Joint Conference on Artificial Intelligence"],
    "KDD": ["ACM SIGKDD International Conference on Knowledge Discovery and Data Mining"],
    "NAACL": ["North American Chapter of the Association for Computational Linguistics"],
    "NEURIPS": [
        "Neural Information Processing Systems",
        "NeurIPS",
        "Advances in Neural Information Processing Systems",
    ],
    "SIGIR": ["International ACM SIGIR Conference on Research and Development in Information Retrieval"],
    "WWW": ["The Web Conference", "International World Wide Web Conference"],
}


def log(message: str) -> None:
    print(message, file=sys.stderr)


class ProgressBar:
    SPINNER = "|/-\\"

    def __init__(self, label: str, enabled: bool) -> None:
        self.label = label
        self.enabled = enabled
        self.current = 0
        self.total: Optional[int] = None
        self._spin_index = 0
        self._last_length = 0

    def _render(self, final: bool = False, extra: str = "") -> None:
        if not self.enabled:
            return

        if self.total is not None and self.total > 0:
            ratio = min(1.0, self.current / self.total)
            width = 24
            filled = int(width * ratio)
            bar = f"[{'#' * filled}{'-' * (width - filled)}]"
            message = (
                f"{self.label} {bar} {self.current}/{self.total} "
                f"{ratio * 100:5.1f}%"
            )
        else:
            spinner = self.SPINNER[self._spin_index % len(self.SPINNER)]
            message = f"{self.label} {spinner} {self.current}"
            self._spin_index += 1

        if extra:
            message = f"{message} {extra}"

        padding = " " * max(0, self._last_length - len(message))
        end = "\n" if final else "\r"
        print(f"{message}{padding}", end=end, file=sys.stderr, flush=True)
        self._last_length = len(message)

    def update(
        self,
        current: Optional[int] = None,
        total: Optional[int] = None,
        extra: str = "",
    ) -> None:
        if current is not None:
            self.current = current
        if total is not None:
            self.total = total
        self._render(final=False, extra=extra)

    def finish(self, extra: str = "") -> None:
        self._render(final=True, extra=extra)


class AnchorExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: List[Tuple[str, str]] = []
        self._current_href: Optional[str] = None
        self._current_text_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        attrs_map = {k.lower(): (v or "") for k, v in attrs}
        href = attrs_map.get("href", "").strip()
        if not href:
            return
        self._current_href = href
        self._current_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a":
            return
        if self._current_href is None:
            return
        text = " ".join("".join(self._current_text_parts).split())
        href = self._current_href
        self._current_href = None
        self._current_text_parts = []
        if text:
            self.anchors.append((href, text))


class MaincardBodyExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: List[str] = []
        self._capture_depth = 0
        self._current_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self._capture_depth > 0:
            self._capture_depth += 1
            return

        attrs_map = {k.lower(): (v or "") for k, v in attrs}
        class_name = attrs_map.get("class", "").lower()
        if "maincardbody" in class_name:
            self._capture_depth = 1
            self._current_parts = []

    def handle_data(self, data: str) -> None:
        if self._capture_depth > 0:
            self._current_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture_depth <= 0:
            return
        self._capture_depth -= 1
        if self._capture_depth == 0:
            text = " ".join("".join(self._current_parts).split())
            self._current_parts = []
            if text:
                self.texts.append(text)


def redact_sensitive_query_values(url: str, sensitive_keys: Sequence[str]) -> str:
    safe_url = url
    for key in sensitive_keys:
        safe_url = re.sub(rf"({re.escape(key)}=)[^&]+", r"\1***", safe_url)
    return safe_url


def api_get_json(
    path: str,
    params: Dict[str, str],
    timeout: float,
    retries: int,
    api_key: Optional[str] = None,
    base_url: str = OPENALEX_BASE_URL,
    sensitive_query_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    request_params = dict(params)
    if api_key:
        request_params["api_key"] = api_key

    query = urlencode(request_params)
    url = f"{base_url}{path}?{query}"
    headers = {
        "User-Agent": "paper-search-skill/1.0 (metadata export tool)",
        "Accept": "application/json",
    }
    sensitive_keys = list(sensitive_query_keys or [])
    if api_key and "api_key" not in sensitive_keys:
        sensitive_keys.append("api_key")

    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            last_error = f"HTTP {status}: {body[:300]}"
            if status in {429, 500, 502, 503, 504} and attempt < retries:
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            safe_url = redact_sensitive_query_values(url, sensitive_keys)
            raise RuntimeError(f"Request failed for {safe_url}. {last_error}") from exc
        except URLError as exc:
            last_error = str(exc.reason)
            if attempt < retries:
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            safe_url = redact_sensitive_query_values(url, sensitive_keys)
            raise RuntimeError(f"Request failed for {safe_url}. {last_error}") from exc
        except TimeoutError as exc:
            last_error = "request timed out"
            if attempt < retries:
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            safe_url = redact_sensitive_query_values(url, sensitive_keys)
            raise RuntimeError(f"Request timed out for {safe_url}") from exc

    safe_url = redact_sensitive_query_values(url, sensitive_keys)
    raise RuntimeError(f"Request failed for {safe_url}. {last_error or 'unknown error'}")


def http_get_text(
    url: str,
    timeout: float,
    retries: int,
) -> str:
    headers = {
        "User-Agent": "paper-search-skill/1.0 (metadata export tool)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    last_error: Optional[str] = None

    for attempt in range(retries + 1):
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            last_error = f"HTTP {status}: {body[:300]}"
            if status in {429, 500, 502, 503, 504} and attempt < retries:
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            raise RuntimeError(f"Request failed for {url}. {last_error}") from exc
        except URLError as exc:
            last_error = str(exc.reason)
            if attempt < retries:
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            raise RuntimeError(f"Request failed for {url}. {last_error}") from exc
        except TimeoutError as exc:
            last_error = "request timed out"
            if attempt < retries:
                time.sleep(min(8.0, 0.7 * (2**attempt)))
                continue
            raise RuntimeError(f"Request timed out for {url}") from exc

    raise RuntimeError(f"Request failed for {url}. {last_error or 'unknown error'}")


def parse_target(target: str) -> Tuple[str, int, str, str]:
    match = re.fullmatch(r"\s*([A-Za-z0-9]+)-(\d{2}|\d{4})\s*", target)
    if not match:
        raise ValueError(
            f"Invalid target '{target}'. Use pattern like AAAI-26, CVPR-2026."
        )

    venue_code = match.group(1).upper()
    year_raw = match.group(2)
    year = int(year_raw)
    if len(year_raw) == 2:
        year += 2000
    if year < 1900 or year > 2100:
        raise ValueError(f"Parsed year {year} is out of supported range [1900, 2100].")

    short_key = f"{venue_code}-{year_raw}"
    canonical_key = f"{venue_code}-{year}"
    return venue_code, year, short_key, canonical_key


def normalize_title(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return re.sub(r"\s+", " ", normalized).strip()


def slugify_identifier(value: str, default: str = TRACK_OTHER) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or default


def track_display_name_from_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("_") if part)


def normalize_track_value(value: Any, default: str = TRACK_OTHER) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return slugify_identifier(raw, default=default)


def infer_track_group(track: str) -> str:
    normalized = normalize_track_value(track, default=TRACK_OTHER)
    if normalized in {"main", "conference", "main_track", "conference_main_track"}:
        return TRACK_GROUP_MAIN
    return TRACK_GROUP_OTHER


def normalize_track_group(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw == TRACK_GROUP_MAIN:
        return TRACK_GROUP_MAIN
    return TRACK_GROUP_OTHER


def set_record_track_fields(
    record: Dict[str, Any],
    track: Any,
    track_display_name: Optional[str] = None,
    track_group: Optional[str] = None,
) -> None:
    normalized_track = normalize_track_value(track, default=TRACK_OTHER)
    record["track"] = normalized_track
    record["track_display_name"] = (
        track_display_name.strip()
        if isinstance(track_display_name, str) and track_display_name.strip()
        else track_display_name_from_slug(normalized_track)
    )
    record["track_group"] = (
        normalize_track_group(track_group)
        if isinstance(track_group, str)
        else infer_track_group(normalized_track)
    )


def normalize_source_id(source_id: str) -> str:
    value = source_id.strip()
    match = re.search(r"(S\d+)$", value)
    if match:
        return match.group(1)
    return value


def build_source_year_filters(source_id: str, year: int) -> List[Tuple[str, str]]:
    sid = normalize_source_id(source_id)
    year_start = f"{year}-01-01"
    year_end = f"{year}-12-31"
    return [
        (
            "locations.source.id + publication_year",
            f"locations.source.id:{sid},publication_year:{year}",
        ),
        (
            "primary_location.source.id + publication_year",
            f"primary_location.source.id:{sid},publication_year:{year}",
        ),
        (
            "locations.source.id + publication_date range",
            (
                f"locations.source.id:{sid},"
                f"from_publication_date:{year_start},to_publication_date:{year_end}"
            ),
        ),
        (
            "primary_location.source.id + publication_date range",
            (
                f"primary_location.source.id:{sid},"
                f"from_publication_date:{year_start},to_publication_date:{year_end}"
            ),
        ),
    ]


def parse_meta_count(payload: Dict[str, Any]) -> int:
    meta = payload.get("meta", {})
    count = meta.get("count", 0)
    if isinstance(count, int):
        return count
    if isinstance(count, str) and count.isdigit():
        return int(count)
    return 0


def infer_track_from_context_texts(
    texts: Iterable[str],
    default_track: str = TRACK_OTHER,
) -> str:
    blob = " ".join(str(text).lower() for text in texts if str(text).strip())
    if not blob:
        return default_track

    if "workshop" in blob or "/workshop" in blob:
        return TRACK_WORKSHOP

    if (
        "competition" in blob
        or "challenge" in blob
        or "contest" in blob
        or "capture-the-flag" in blob
    ):
        return TRACK_COMPETITION

    if (
        "datasets and benchmarks" in blob
        or "dataset and benchmark" in blob
        or "datasets_and_benchmarks" in blob
        or "d&b" in blob
        or "/datasets" in blob
        or "/benchmark" in blob
    ):
        return TRACK_DATASETS_BENCHMARKS

    if "/conference" in blob or "main track" in blob or "conference" in blob:
        return TRACK_MAIN

    return default_track


def infer_track_from_title_keywords(title: str) -> str:
    t = normalize_title(title)
    if not t:
        return TRACK_OTHER

    # Keep competition/workshop signals stronger than dataset/benchmark words.
    competition_keywords = (
        "competition",
        "challenge",
        "contest",
        "capture the flag",
        "shared task",
    )
    if any(keyword in t for keyword in competition_keywords):
        return TRACK_COMPETITION

    if "workshop" in t:
        return TRACK_WORKSHOP

    dataset_bench_keywords = (
        "dataset",
        "datasets",
        "benchmark",
        "benchmarks",
        "leaderboard",
        "reproducibility study",
        "reproducibility",
        "corpus",
    )
    if any(keyword in t for keyword in dataset_bench_keywords):
        return TRACK_DATASETS_BENCHMARKS

    return TRACK_OTHER


def infer_track_from_virtual_category(category: str, title: str) -> str:
    cat = category.lower().strip()
    title_track = infer_track_from_title_keywords(title)
    if title_track != TRACK_OTHER:
        return title_track
    if "workshop" in cat:
        return TRACK_WORKSHOP
    if any(token in cat for token in ("competition", "challenge", "contest")):
        return TRACK_COMPETITION
    if any(token in cat for token in ("dataset", "benchmark", "datasets")):
        return TRACK_DATASETS_BENCHMARKS
    if cat in {"poster", "oral", "spotlight", "paper"}:
        return TRACK_MAIN
    return infer_track_from_context_texts([cat, title], default_track=TRACK_OTHER)


def infer_presentation_level_from_virtual_category(
    category: str,
    default_level: str = "poster",
) -> str:
    cat = category.lower().strip()
    if cat in {"oral", "spotlight"}:
        return "oral"
    if cat == "poster":
        return "poster"
    return default_level


def is_noise_anchor_title(text: str) -> bool:
    normalized = normalize_title(text)
    if not normalized:
        return True
    if normalized in NOISE_ANCHOR_TEXTS:
        return True
    if re.fullmatch(r"\d+", normalized.replace(" ", "")):
        return True
    return False


def score_anchor_title_candidate(text: str) -> int:
    if is_noise_anchor_title(text):
        return -10_000

    normalized = normalize_title(text)
    words = normalized.split()
    score = 0
    score += min(120, len(normalized))
    score += min(60, len(words) * 6)
    if len(words) >= 3:
        score += 30
    if len(words) >= 7:
        score += 15
    if any(ch.isdigit() for ch in text):
        score += 3
    if any(ch in text for ch in (":", "-", ";", ",")):
        score += 4
    return score


def extract_neurips_virtual_titles(
    html: str,
    page_url: str,
) -> List[Dict[str, str]]:
    extractor = AnchorExtractor()
    extractor.feed(html)

    entries: List[Dict[str, str]] = []
    grouped: Dict[str, Dict[str, Any]] = {}
    pattern = re.compile(r"^/virtual/\d{4}/([A-Za-z0-9_-]+)/(\d+)$")
    for href, text in extractor.anchors:
        href_abs = urljoin(page_url, href)
        parsed = urlparse(href_abs)
        clean_path = re.sub(r"/+$", "", parsed.path)
        match = pattern.match(clean_path)
        if not match:
            continue
        category = match.group(1).lower()
        if category in NOISE_VIRTUAL_CATEGORIES:
            continue
        canonical_url = f"{parsed.scheme}://{parsed.netloc}{clean_path}"
        if canonical_url not in grouped:
            grouped[canonical_url] = {"category": category, "texts": []}
        if text and text.strip():
            grouped[canonical_url]["texts"].append(text.strip())

    seen = set()
    for url, meta in grouped.items():
        texts = meta.get("texts", [])
        if not isinstance(texts, list) or not texts:
            continue
        best_text = max(texts, key=score_anchor_title_candidate)
        if score_anchor_title_candidate(best_text) < 20:
            continue
        if is_noise_anchor_title(best_text):
            continue

        normalized = normalize_title(best_text)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        category = str(meta.get("category") or "").lower()
        entries.append(
            {
                "title": best_text,
                "url": url,
                "virtual_category": category,
                "track": infer_track_from_virtual_category(category, best_text),
                "presentation_level": infer_presentation_level_from_virtual_category(
                    category,
                    default_level="poster",
                ),
            }
        )

    return entries


def extract_icml_accepted_titles(
    html: str,
    page_url: str,
) -> List[Dict[str, str]]:
    extractor = MaincardBodyExtractor()
    extractor.feed(html)

    entries: List[Dict[str, str]] = []
    seen = set()
    for text in extractor.texts:
        title = " ".join(text.split())
        normalized = normalize_title(title)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        entries.append(
            {
                "title": title,
                "url": page_url,
                "virtual_category": "conference",
                "track": TRACK_MAIN,
                "presentation_level": "poster",
            }
        )

    return entries


def extract_external_titles(
    html: str,
    page_url: str,
) -> List[Dict[str, str]]:
    parsed = urlparse(page_url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if "neurips.cc" in host and "/virtual/" in path:
        return extract_neurips_virtual_titles(html=html, page_url=page_url)
    if "icml.cc" in host and "acceptedpapers" in path:
        return extract_icml_accepted_titles(html=html, page_url=page_url)

    neurips_entries = extract_neurips_virtual_titles(html=html, page_url=page_url)
    if neurips_entries:
        return neurips_entries
    return extract_icml_accepted_titles(html=html, page_url=page_url)


def classify_neurips_track_from_sourceurl(sourceurl: str) -> Dict[str, str]:
    raw = sourceurl.strip()
    if not raw:
        return {
            "track": TRACK_OTHER,
            "track_display_name": "Other",
            "track_group": TRACK_GROUP_OTHER,
        }

    working = raw
    url_start = min(
        [idx for idx in (working.find("https://"), working.find("http://")) if idx >= 0],
        default=-1,
    )
    if url_start > 0:
        working = working[url_start:]
    working = re.sub(r"-(cdmx|mx|mexico_city|mexicocity|sandiego|san_diego)$", "", working, flags=re.IGNORECASE)
    lower = working.lower()

    if "openreview.net/group" in lower:
        parsed = urlparse(working)
        group_id = ""
        if parsed.query:
            group_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if not group_id and parsed.path:
            group_id = parsed.path.strip("/").split("/")[-1]

        group_lower = group_id.lower()
        if group_lower.endswith("/conference"):
            return {
                "track": "conference",
                "track_display_name": "Conference",
                "track_group": TRACK_GROUP_MAIN,
            }
        if "datasets_and_benchmarks" in group_lower:
            return {
                "track": "datasets_and_benchmarks_track",
                "track_display_name": "Datasets and Benchmarks Track",
                "track_group": TRACK_GROUP_OTHER,
            }
        if "position_paper_track" in group_lower:
            return {
                "track": "position_paper_track",
                "track_display_name": "Position Paper Track",
                "track_group": TRACK_GROUP_OTHER,
            }
        if group_lower.startswith("ml_reproducibility_challenge"):
            return {
                "track": "ml_reproducibility_challenge",
                "track_display_name": "ML Reproducibility Challenge",
                "track_group": TRACK_GROUP_OTHER,
            }

        segment = group_id.split("/")[-1] if group_id else "openreview_track"
        track = slugify_identifier(segment, default="openreview_track")
        return {
            "track": track,
            "track_display_name": track_display_name_from_slug(track),
            "track_group": infer_track_group(track),
        }

    if "tmlr" in lower:
        return {
            "track": "journal_track_tmlr",
            "track_display_name": "Journal Track (TMLR)",
            "track_group": TRACK_GROUP_OTHER,
        }
    if "jmlr" in lower:
        return {
            "track": "journal_track_jmlr",
            "track_display_name": "Journal Track (JMLR)",
            "track_group": TRACK_GROUP_OTHER,
        }
    if "rescience" in lower:
        return {
            "track": "journal_track_rescience",
            "track_display_name": "Journal Track (ReScience)",
            "track_group": TRACK_GROUP_OTHER,
        }
    if "annals-of-statistics" in lower:
        return {
            "track": "journal_track_annals_of_statistics",
            "track_display_name": "Journal Track (Annals of Statistics)",
            "track_group": TRACK_GROUP_OTHER,
        }
    if "journal" in lower:
        return {
            "track": "journal_track",
            "track_display_name": "Journal Track",
            "track_group": TRACK_GROUP_OTHER,
        }

    track = slugify_identifier(working, default=TRACK_OTHER)
    return {
        "track": track,
        "track_display_name": track_display_name_from_slug(track),
        "track_group": infer_track_group(track),
    }


def parse_int_like(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return default


def load_neurips_official_track_index(
    year: int,
    timeout: float,
    retries: int,
) -> Dict[str, Any]:
    url = NEURIPS_VIRTUAL_TRACKS_JSON_URL.format(year=year)
    text = http_get_text(url=url, timeout=timeout, retries=retries)
    payload = json.loads(text)
    results = payload.get("results")
    if not isinstance(results, list):
        raise RuntimeError("Official NeurIPS JSON payload has no 'results' list.")

    title_index: Dict[str, Dict[str, str]] = {}
    track_counts = Counter()
    track_display_names: Dict[str, str] = {}
    track_groups: Dict[str, str] = {}
    conflicting_title_count = 0

    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("name") or "").strip()
        normalized_title = normalize_title(title)
        if not normalized_title:
            continue

        sourceurl = str(item.get("sourceurl") or "").strip()
        track_info = classify_neurips_track_from_sourceurl(sourceurl)
        track = normalize_track_value(track_info.get("track"), default=TRACK_OTHER)
        track_display_name = str(
            track_info.get("track_display_name") or track_display_name_from_slug(track)
        )
        track_group = normalize_track_group(track_info.get("track_group"))

        track_counts[track] += 1
        if track not in track_display_names:
            track_display_names[track] = track_display_name
        if track not in track_groups:
            track_groups[track] = track_group

        existing = title_index.get(normalized_title)
        candidate = {
            "track": track,
            "track_display_name": track_display_name,
            "track_group": track_group,
            "official_sourceurl": sourceurl,
        }
        if existing is None:
            title_index[normalized_title] = candidate
            continue

        if existing.get("track") != track:
            conflicting_title_count += 1
            existing_is_other = normalize_track_value(existing.get("track")) == TRACK_OTHER
            candidate_is_other = track == TRACK_OTHER
            if existing_is_other and not candidate_is_other:
                title_index[normalized_title] = candidate

    track_catalog = [
        {
            "track": track,
            "track_display_name": track_display_names.get(
                track, track_display_name_from_slug(track)
            ),
            "track_group": track_groups.get(track, infer_track_group(track)),
            "paper_count": count,
        }
        for track, count in track_counts.items()
    ]
    track_catalog.sort(key=lambda item: (-int(item["paper_count"]), str(item["track"])))

    return {
        "url": url,
        "paper_count_official": parse_int_like(payload.get("count"), default=len(results)),
        "results_count": len(results),
        "title_index": title_index,
        "track_catalog": track_catalog,
        "conflicting_title_count": conflicting_title_count,
    }


def apply_official_track_index(
    records: List[Dict[str, Any]],
    title_index: Dict[str, Dict[str, str]],
) -> Dict[str, int]:
    matched = 0
    updated = 0
    with_source_url = 0

    for record in records:
        previous_track = normalize_track_value(record.get("track"), default=TRACK_OTHER)
        previous_group = normalize_track_group(record.get("track_group"))

        title = str(record.get("paper_title") or "").strip()
        normalized_title = normalize_title(title)
        mapped = title_index.get(normalized_title) if normalized_title else None

        if mapped is None:
            set_record_track_fields(
                record=record,
                track=previous_track,
                track_display_name=str(record.get("track_display_name") or ""),
                track_group=record.get("track_group") if record.get("track_group") else None,
            )
            continue

        matched += 1
        set_record_track_fields(
            record=record,
            track=mapped.get("track", TRACK_OTHER),
            track_display_name=str(mapped.get("track_display_name") or ""),
            track_group=str(mapped.get("track_group") or TRACK_GROUP_OTHER),
        )
        current_track = normalize_track_value(record.get("track"), default=TRACK_OTHER)
        current_group = normalize_track_group(record.get("track_group"))
        if previous_track != current_track or previous_group != current_group:
            updated += 1

        source_url = str(mapped.get("official_sourceurl") or "").strip()
        if source_url:
            record["official_track_source_url"] = source_url
            with_source_url += 1

    return {
        "matched_record_count": matched,
        "updated_record_count": updated,
        "unmatched_record_count": max(0, len(records) - matched),
        "records_with_official_source_url": with_source_url,
    }


def remap_main_track_to_conference(records: List[Dict[str, Any]]) -> int:
    updated = 0
    for record in records:
        if normalize_track_value(record.get("track"), default=TRACK_OTHER) != TRACK_MAIN:
            continue
        set_record_track_fields(
            record=record,
            track="conference",
            track_display_name="Conference",
            track_group=TRACK_GROUP_MAIN,
        )
        updated += 1
    return updated


def build_track_counts(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter()
    for record in records:
        track = normalize_track_value(record.get("track"), default=TRACK_OTHER)
        counts[track] += 1
    return {
        track: counts[track]
        for track in sorted(counts, key=lambda key: (-counts[key], key))
    }


def build_track_group_counts(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter()
    for record in records:
        group = normalize_track_group(record.get("track_group"))
        counts[group] += 1
    return {group: counts.get(group, 0) for group in TRACK_GROUP_VALUES}


def reconcile_records_with_external_titles(
    records: List[Dict[str, Any]],
    external_entries: List[Dict[str, Any]],
    include_missing: bool,
    drop_extra: bool,
    default_level: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    records_out = list(records)
    provider_track_counts_before = build_track_counts(records_out)
    record_by_title: Dict[str, Dict[str, Any]] = {}
    for record in records_out:
        title = str(record.get("paper_title") or "").strip()
        normalized = normalize_title(title)
        if normalized and normalized not in record_by_title:
            record_by_title[normalized] = record

    external_by_title: Dict[str, Dict[str, Any]] = {}
    for entry in external_entries:
        title = str(entry.get("title") or "").strip()
        normalized = normalize_title(title)
        if normalized and normalized not in external_by_title:
            external_by_title[normalized] = entry

    matched_norms = [
        normalized for normalized in external_by_title.keys()
        if normalized in record_by_title
    ]
    missing_norms = [
        normalized for normalized in external_by_title.keys()
        if normalized not in record_by_title
    ]
    extras_norms = [
        normalized for normalized in record_by_title.keys()
        if normalized not in external_by_title
    ]

    matched_track_conflicts = 0
    matched_track_updated = 0
    for normalized in matched_norms:
        entry = external_by_title[normalized]
        record = record_by_title[normalized]
        external_track = str(entry.get("track") or TRACK_OTHER).strip().lower()
        if external_track not in TRACK_VALUES:
            external_track = TRACK_OTHER

        provider_track = str(record.get("track") or TRACK_OTHER).strip().lower()
        if provider_track not in TRACK_VALUES:
            provider_track = TRACK_OTHER

        if (
            external_track != TRACK_OTHER
            and provider_track in {TRACK_OTHER, TRACK_MAIN}
            and provider_track != external_track
        ):
            set_record_track_fields(record=record, track=external_track)
            matched_track_updated += 1
        elif (
            provider_track != TRACK_OTHER
            and external_track != TRACK_OTHER
            and provider_track != external_track
        ):
            matched_track_conflicts += 1

    if include_missing:
        for normalized in missing_norms:
            entry = external_by_title[normalized]
            track = str(entry.get("track") or TRACK_OTHER).strip().lower()
            if track not in TRACK_VALUES:
                track = TRACK_OTHER
            presentation_level = str(entry.get("presentation_level") or default_level)
            if presentation_level not in VALID_PRESENTATION_LEVELS:
                presentation_level = default_level
            record = {
                "paper_title": entry["title"],
                "authors": [],
                "institutions": [],
                "abstract": "",
                "keywords": [],
                "presentation_level": presentation_level,
                "openalex_id": None,
                "doi": None,
                "openreview_id": None,
                "external_url": entry.get("url"),
                "external_only": True,
            }
            set_record_track_fields(record=record, track=track)
            records_out.append(record)

    if drop_extra:
        extras_set = set(extras_norms)
        records_out = [
            record
            for record in records_out
            if normalize_title(str(record.get("paper_title") or "")) not in extras_set
        ]

    summary: Dict[str, Any] = {
        "enabled": True,
        "external_title_count": len(external_by_title),
        "provider_title_count_before": len(record_by_title),
        "matched_title_count": len(external_by_title) - len(missing_norms),
        "matched_track_updated_count": matched_track_updated,
        "matched_track_conflict_count": matched_track_conflicts,
        "missing_in_provider_count": len(missing_norms),
        "extra_in_provider_count": len(extras_norms),
        "missing_in_provider_titles": [
            external_by_title[norm]["title"] for norm in missing_norms
        ],
        "extra_in_provider_titles": [
            record_by_title[norm]["paper_title"] for norm in extras_norms
        ],
        "include_missing_applied": include_missing,
        "drop_extra_applied": drop_extra,
        "external_track_counts": build_track_counts(
            [
                {
                    "track": external_by_title[norm].get("track", TRACK_OTHER)
                }
                for norm in external_by_title
            ]
        ),
        "provider_track_counts_before": provider_track_counts_before,
        "provider_track_counts_after": build_track_counts(records_out),
        "provider_title_count_after": len(
            {
                normalize_title(str(record.get("paper_title") or ""))
                for record in records_out
                if normalize_title(str(record.get("paper_title") or ""))
            }
        ),
    }

    return records_out, summary


def unwrap_record_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def to_string_list(value: Any) -> List[str]:
    unwrapped = unwrap_record_value(value)
    if isinstance(unwrapped, list):
        return [str(item).strip() for item in unwrapped if str(item).strip()]
    if isinstance(unwrapped, str) and unwrapped.strip():
        return [unwrapped.strip()]
    return []


def parse_notes_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    notes = payload.get("notes")
    if isinstance(notes, list):
        return [item for item in notes if isinstance(item, dict)]
    results = payload.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return []


def build_openreview_venue_ids(venue_code: str, year: int) -> List[str]:
    patterns = OPENREVIEW_VENUE_ID_PATTERNS.get(venue_code, [])
    return unique_preserve_order([pattern.format(year=year) for pattern in patterns])


def openreview_note_score(note: Dict[str, Any]) -> int:
    for key in ("tcdate", "tmdate", "cdate", "pdate"):
        value = note.get(key)
        if isinstance(value, int):
            return value
    return 0


def dedupe_openreview_notes(notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best_by_forum: Dict[str, Dict[str, Any]] = {}
    for note in notes:
        forum = str(note.get("forum") or note.get("id") or "").strip()
        if not forum:
            continue
        existing = best_by_forum.get(forum)
        if existing is None or openreview_note_score(note) > openreview_note_score(existing):
            best_by_forum[forum] = note
    return list(best_by_forum.values())


def infer_presentation_level_from_texts(texts: Iterable[str]) -> str:
    blob = " ".join(text.lower() for text in texts if text).strip()
    if not blob:
        return "poster"
    if "best paper" in blob or "outstanding paper" in blob:
        return "bestpaper"
    if "oral" in blob or "spotlight" in blob:
        return "oral"
    if "poster" in blob:
        return "poster"
    return "poster"


def infer_presentation_level_from_note(note: Dict[str, Any]) -> str:
    content = note.get("content", {})
    texts: List[str] = []
    if isinstance(content, dict):
        for key in ("presentation_type", "presentation", "venue", "venueid"):
            value = unwrap_record_value(content.get(key))
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())

    details = note.get("details")
    if isinstance(details, dict):
        replies = details.get("replies", [])
        if isinstance(replies, list):
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_content = reply.get("content", {})
                if not isinstance(reply_content, dict):
                    continue
                for key in ("decision", "recommendation", "comment"):
                    value = unwrap_record_value(reply_content.get(key))
                    if isinstance(value, str) and value.strip():
                        texts.append(value.strip())

    return infer_presentation_level_from_texts(texts)


def infer_track_from_openreview_note(note: Dict[str, Any]) -> str:
    content = note.get("content", {})
    context_texts: List[str] = []
    if isinstance(content, dict):
        for key in (
            "venueid",
            "venue_id",
            "venue",
            "track",
            "papertrack",
            "submission_track",
        ):
            value = unwrap_record_value(content.get(key))
            if isinstance(value, str) and value.strip():
                context_texts.append(value.strip())

    invitation = note.get("invitation")
    if isinstance(invitation, str) and invitation.strip():
        context_texts.append(invitation.strip())

    return infer_track_from_context_texts(context_texts, default_track=TRACK_OTHER)


def note_is_likely_accepted(note: Dict[str, Any], venue_id: str) -> bool:
    content = note.get("content", {})
    if not isinstance(content, dict):
        content = {}

    venue_id_norm = venue_id.lower().strip()

    venueid_values: List[str] = []
    for key in ("venueid", "venue_id"):
        value = unwrap_record_value(content.get(key))
        if isinstance(value, str) and value.strip():
            venueid_values.append(value.strip())

    venue_raw = unwrap_record_value(content.get("venue"))
    venue_text = venue_raw.strip() if isinstance(venue_raw, str) else ""
    invitation = note.get("invitation")
    invitation_text = invitation.strip() if isinstance(invitation, str) else ""

    context_blob = " ".join([*venueid_values, venue_text, invitation_text]).lower()
    if (
        "withdrawn_submission" in context_blob
        or "withdrawn submission" in context_blob
        or "/withdrawn" in context_blob
        or " withdrawn" in context_blob
    ):
        return False
    if "submitted to" in context_blob or " submitted" in context_blob:
        return False

    venue_text_lower = venue_text.lower()
    if venue_text_lower:
        if any(token in venue_text_lower for token in ("reject", "withdrawn", "submitted")):
            return False
        if any(
            token in venue_text_lower
            for token in (
                "poster",
                "oral",
                "spotlight",
                "notable top 25",
                "notable top 5",
                "accept",
                "accepted",
            )
        ):
            return True
        if venue_id_norm in venue_text_lower:
            return True

    texts: List[str] = []
    details = note.get("details")
    if isinstance(details, dict):
        replies = details.get("replies", [])
        if isinstance(replies, list):
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_content = reply.get("content", {})
                if not isinstance(reply_content, dict):
                    continue
                for key in ("decision", "recommendation"):
                    value = unwrap_record_value(reply_content.get(key))
                    if isinstance(value, str) and value.strip():
                        texts.append(value.strip().lower())

    blob = " ".join(texts)
    if blob:
        if "withdraw" in blob:
            return False
        if "reject" in blob and "accept" not in blob:
            return False
        if "accept" in blob and "reject" not in blob:
            return True
        if "accepted" in blob and "rejected" not in blob:
            return True

    for venueid_raw in venueid_values:
        venueid_lower = venueid_raw.lower().strip()
        if venueid_lower == venue_id_norm:
            return True

    return False


def fetch_openreview_notes_for_venue(
    venue_id: str,
    timeout: float,
    retries: int,
    show_progress: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    best_notes: List[Dict[str, Any]] = []
    best_meta = {"base_url": "", "filter_key": "", "venue_id": venue_id, "mode": ""}

    for base_url in OPENREVIEW_BASE_URLS:
        best_content_notes: List[Dict[str, Any]] = []
        best_content_key = ""

        for filter_key in (
            "content.venueid",
            "content.venue_id",
            "content.venue",
            "content.venueid.value",
            "content.venue.value",
        ):
            offset = 0
            per_page = 1000
            collected: List[Dict[str, Any]] = []
            progress = ProgressBar(
                f"OpenReview {venue_id} [{filter_key}]",
                enabled=show_progress,
            )

            while True:
                params = {
                    filter_key: venue_id,
                    "limit": str(per_page),
                    "offset": str(offset),
                    "details": "replies",
                }
                try:
                    payload = api_get_json(
                        path="/notes",
                        params=params,
                        timeout=timeout,
                        retries=retries,
                        base_url=base_url,
                    )
                except RuntimeError:
                    collected = []
                    break

                batch = parse_notes_from_payload(payload)
                if not batch:
                    break

                collected.extend(batch)
                progress.update(
                    current=len(collected),
                    extra=f"pages={offset // per_page + 1}",
                )
                if len(batch) < per_page:
                    break

                offset += len(batch)
                time.sleep(0.08)

            deduped = dedupe_openreview_notes(collected)
            accepted = [
                note for note in deduped if note_is_likely_accepted(note, venue_id)
            ]
            progress.finish(extra=f"accepted={len(accepted)}/{len(deduped)}")
            if len(accepted) > len(best_content_notes):
                best_content_notes = accepted
                best_content_key = filter_key

        best_invitation_accepted: List[Dict[str, Any]] = []
        best_invitation_key = ""
        for invitation in (
            f"{venue_id}/-/Submission",
            f"{venue_id}/-/Blind_Submission",
            f"{venue_id}/-/Paper_Submission",
        ):
            offset = 0
            per_page = 1000
            collected: List[Dict[str, Any]] = []
            progress = ProgressBar(
                f"OpenReview {venue_id} [{invitation}]",
                enabled=show_progress,
            )

            while True:
                params = {
                    "invitation": invitation,
                    "limit": str(per_page),
                    "offset": str(offset),
                    "details": "replies",
                }
                try:
                    payload = api_get_json(
                        path="/notes",
                        params=params,
                        timeout=timeout,
                        retries=retries,
                        base_url=base_url,
                    )
                except RuntimeError:
                    collected = []
                    break

                batch = parse_notes_from_payload(payload)
                if not batch:
                    break
                collected.extend(batch)
                progress.update(
                    current=len(collected),
                    extra=f"pages={offset // per_page + 1}",
                )
                if len(batch) < per_page:
                    break

                offset += len(batch)
                time.sleep(0.08)

            deduped = dedupe_openreview_notes(collected)
            accepted = [
                note for note in deduped if note_is_likely_accepted(note, venue_id)
            ]
            progress.finish(extra=f"accepted={len(accepted)}")
            if len(accepted) > len(best_invitation_accepted):
                best_invitation_accepted = accepted
                best_invitation_key = invitation

        union_notes = dedupe_openreview_notes(
            [*best_content_notes, *best_invitation_accepted]
        )
        candidate_notes = union_notes
        mode = "content+invitation_union"
        key = f"{best_content_key}|{best_invitation_key}".strip("|")

        if not candidate_notes:
            if len(best_content_notes) >= len(best_invitation_accepted):
                candidate_notes = best_content_notes
                mode = "content_filter"
                key = best_content_key
            else:
                candidate_notes = best_invitation_accepted
                mode = "invitation_only"
                key = best_invitation_key

        if len(candidate_notes) > len(best_notes):
            best_notes = candidate_notes
            best_meta = {
                "base_url": base_url,
                "filter_key": key,
                "venue_id": venue_id,
                "mode": mode,
                "content_count": str(len(best_content_notes)),
                "invitation_count": str(len(best_invitation_accepted)),
                "union_count": str(len(union_notes)),
            }

    return best_notes, best_meta


def openreview_note_to_record(
    note: Dict[str, Any],
    default_level: str,
    title_level_overrides: Dict[str, str],
) -> Dict[str, Any]:
    content = note.get("content", {})
    if not isinstance(content, dict):
        content = {}

    title_raw = unwrap_record_value(content.get("title"))
    paper_title = title_raw.strip() if isinstance(title_raw, str) else ""
    normalized_title = normalize_title(paper_title)

    authors = to_string_list(content.get("authors"))
    authorids = to_string_list(content.get("authorids"))
    if not authors and authorids:
        for author_id in authorids:
            if author_id.startswith("~"):
                clean = re.sub(r"\d+$", "", author_id.lstrip("~"))
                clean = clean.replace("_", " ").strip()
                if clean:
                    authors.append(clean)

    abstract_raw = unwrap_record_value(content.get("abstract"))
    abstract = abstract_raw.strip() if isinstance(abstract_raw, str) else ""
    keywords = to_string_list(content.get("keywords"))

    institution_fields = (
        "institutions",
        "institution",
        "affiliations",
        "author_institutions",
    )
    institutions: List[str] = []
    for field in institution_fields:
        institutions.extend(to_string_list(content.get(field)))

    inferred_level = infer_presentation_level_from_note(note)
    presentation_level = title_level_overrides.get(normalized_title, inferred_level)
    if presentation_level not in VALID_PRESENTATION_LEVELS:
        presentation_level = default_level
    track = infer_track_from_openreview_note(note)
    if track not in TRACK_VALUES:
        track = TRACK_OTHER

    doi_raw = unwrap_record_value(content.get("doi"))
    doi = doi_raw.strip() if isinstance(doi_raw, str) else None

    record = {
        "paper_title": paper_title,
        "authors": unique_preserve_order(authors),
        "institutions": unique_preserve_order(institutions),
        "abstract": abstract,
        "keywords": unique_preserve_order(keywords),
        "presentation_level": presentation_level,
        "openalex_id": None,
        "doi": doi,
        "openreview_id": note.get("id"),
    }
    set_record_track_fields(record=record, track=track)
    return record

def unique_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for item in items:
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def rebuild_abstract(inverted_index: Any) -> str:
    if not isinstance(inverted_index, dict) or not inverted_index:
        return ""

    max_pos = -1
    for positions in inverted_index.values():
        if isinstance(positions, list):
            for pos in positions:
                if isinstance(pos, int) and pos > max_pos:
                    max_pos = pos
    if max_pos < 0:
        return ""

    tokens = [""] * (max_pos + 1)
    for token, positions in inverted_index.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and 0 <= pos <= max_pos:
                tokens[pos] = token

    return " ".join(word for word in tokens if word).strip()


def score_source(source: Dict[str, Any], query: str) -> float:
    display_name = str(source.get("display_name") or "").lower()
    if not display_name:
        return -1.0

    query_lower = query.lower().strip()
    query_tokens = [token for token in re.split(r"[^a-z0-9]+", query_lower) if token]

    score = 0.0
    if display_name == query_lower:
        score += 8.0

    for token in query_tokens:
        if token in display_name:
            score += 5.0

    source_type = str(source.get("type") or "").lower()
    if source_type in {"conference", "journal", "book series"}:
        score += 2.0

    works_count = source.get("works_count")
    if isinstance(works_count, int) and works_count > 0:
        score += min(3.0, works_count / 50000.0)

    return score


def search_sources(
    query: str,
    mailto: Optional[str],
    timeout: float,
    retries: int,
    api_key: Optional[str],
) -> List[Dict[str, Any]]:
    params: Dict[str, str] = {
        "search": query,
        "per-page": "200",
        "select": "id,display_name,type,works_count",
    }
    if mailto:
        params["mailto"] = mailto

    payload = api_get_json(
        "/sources",
        params,
        timeout=timeout,
        retries=retries,
        api_key=api_key,
    )
    results = payload.get("results")
    if not isinstance(results, list):
        return []

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for source in results:
        if not isinstance(source, dict):
            continue
        if not source.get("id"):
            continue
        scored.append((score_source(source, query), source))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [source for _, source in scored]


def get_source_year_count(
    source_id: str,
    year: int,
    mailto: Optional[str],
    timeout: float,
    retries: int,
    api_key: Optional[str],
) -> int:
    best_count = 0
    for _, filter_expr in build_source_year_filters(source_id, year):
        params: Dict[str, str] = {
            "filter": filter_expr,
            "per-page": "1",
            "cursor": "*",
            "select": "id",
        }
        if mailto:
            params["mailto"] = mailto

        try:
            payload = api_get_json(
                "/works",
                params,
                timeout=timeout,
                retries=retries,
                api_key=api_key,
            )
        except RuntimeError:
            continue

        best_count = max(best_count, parse_meta_count(payload))

    return best_count


def pick_source(
    venue_code: str,
    year: int,
    mailto: Optional[str],
    timeout: float,
    retries: int,
    api_key: Optional[str],
) -> Dict[str, Any]:
    preferred_terms = VENUE_SEARCH_TERMS.get(venue_code, [])
    search_terms = unique_preserve_order([*preferred_terms, venue_code])

    by_id: Dict[str, Dict[str, Any]] = {}
    for term in search_terms:
        sources = search_sources(
            term,
            mailto=mailto,
            timeout=timeout,
            retries=retries,
            api_key=api_key,
        )
        for source in sources:
            source_id = source.get("id")
            if not isinstance(source_id, str):
                continue
            match_score = score_source(source, term)
            existing = by_id.get(source_id)
            if existing is None or match_score > existing["match_score"]:
                by_id[source_id] = {
                    "id": source_id,
                    "display_name": source.get("display_name") or "",
                    "type": source.get("type") or "",
                    "match_score": match_score,
                    "works_count": int(source.get("works_count") or 0),
                }

    if not by_id:
        raise RuntimeError(
            f"No OpenAlex source candidate found for venue code '{venue_code}'."
        )

    def name_term_hits(display_name: str) -> int:
        text = display_name.lower()
        hits = 0
        for term in preferred_terms:
            normalized = term.lower().strip()
            if normalized and normalized in text:
                hits += 1
        if venue_code.lower() in text:
            hits += 1
        return hits

    candidates = sorted(
        by_id.values(),
        key=lambda item: item["match_score"],
        reverse=True,
    )[:50]

    selected: Optional[Dict[str, Any]] = None
    for candidate in candidates:
        source_id = candidate["id"]
        try:
            year_count = get_source_year_count(
                source_id,
                year,
                mailto=mailto,
                timeout=timeout,
                retries=retries,
                api_key=api_key,
            )
        except RuntimeError:
            year_count = -1
        candidate["year_count"] = year_count
        candidate["name_term_hits"] = name_term_hits(
            str(candidate.get("display_name") or "")
        )

        if selected is None:
            selected = candidate
            continue

        current_rank = (
            selected.get("year_count", -1),
            selected.get("name_term_hits", -1),
            selected.get("works_count", -1),
            selected.get("match_score", -1.0),
        )
        candidate_rank = (
            candidate.get("year_count", -1),
            candidate.get("name_term_hits", -1),
            candidate.get("works_count", -1),
            candidate.get("match_score", -1.0),
        )
        if candidate_rank > current_rank:
            selected = candidate

    if selected is None:
        raise RuntimeError(f"Failed to pick source for venue code '{venue_code}'.")

    return selected


def fetch_works(
    source_id: str,
    year: int,
    mailto: Optional[str],
    timeout: float,
    retries: int,
    max_papers: Optional[int],
    api_key: Optional[str],
    show_progress: bool,
) -> Tuple[List[Dict[str, Any]], str]:
    def fetch_works_with_filter(filter_expr: str, strategy_name: str) -> List[Dict[str, Any]]:
        cursor = "*"
        all_works: List[Dict[str, Any]] = []
        page = 0
        progress = ProgressBar(
            f"OpenAlex {year} [{strategy_name}]",
            enabled=show_progress,
        )

        while True:
            params: Dict[str, str] = {
                "filter": filter_expr,
                "per-page": str(PER_PAGE),
                "cursor": cursor,
                "select": WORKS_SELECT_FIELDS,
            }
            if mailto:
                params["mailto"] = mailto

            payload = api_get_json(
                "/works",
                params,
                timeout=timeout,
                retries=retries,
                api_key=api_key,
            )
            results = payload.get("results")
            if not isinstance(results, list):
                break
            page += 1

            meta_total = parse_meta_count(payload)
            total_hint: Optional[int] = meta_total if meta_total > 0 else None
            if max_papers is not None and total_hint is not None:
                total_hint = min(total_hint, max_papers)

            for work in results:
                if isinstance(work, dict):
                    all_works.append(work)
                    progress.update(
                        current=len(all_works),
                        total=total_hint,
                        extra=f"pages={page}",
                    )
                    if max_papers is not None and len(all_works) >= max_papers:
                        progress.finish(extra="done")
                        return all_works

            meta = payload.get("meta", {})
            next_cursor = meta.get("next_cursor")
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
            ):
                break

            cursor = next_cursor
            time.sleep(0.08)

        progress.finish(extra="done")
        return all_works

    for strategy_name, filter_expr in build_source_year_filters(source_id, year):
        try:
            works = fetch_works_with_filter(filter_expr, strategy_name)
        except RuntimeError:
            continue
        if works:
            return works, strategy_name

    return [], "none"


def extract_keywords(work: Dict[str, Any], max_keywords: int = 12) -> List[str]:
    keywords_field = work.get("keywords")
    if isinstance(keywords_field, list):
        names: List[str] = []
        for item in keywords_field:
            if isinstance(item, dict):
                display_name = item.get("display_name")
                if isinstance(display_name, str) and display_name.strip():
                    names.append(display_name.strip())
        if names:
            return unique_preserve_order(names)[:max_keywords]

    concepts_field = work.get("concepts")
    if isinstance(concepts_field, list):
        ranked: List[Tuple[float, str]] = []
        for concept in concepts_field:
            if not isinstance(concept, dict):
                continue
            name = concept.get("display_name")
            if not isinstance(name, str) or not name.strip():
                continue
            score = concept.get("score")
            numeric_score = float(score) if isinstance(score, (int, float)) else 0.0
            ranked.append((numeric_score, name.strip()))
        ranked.sort(key=lambda item: item[0], reverse=True)
        fallback_names = [name for _, name in ranked]
        if fallback_names:
            return unique_preserve_order(fallback_names)[:max_keywords]

    return []


def load_presentation_overrides(
    overrides_path: Optional[Path],
    keys: Sequence[str],
) -> Dict[str, str]:
    if overrides_path is None:
        return {}
    if not overrides_path.exists():
        raise FileNotFoundError(f"Overrides file not found: {overrides_path}")

    with overrides_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError("Overrides file must be a JSON object.")

    block: Dict[str, Any] = {}
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            block = value
            break

    if not block:
        return {}

    overrides: Dict[str, str] = {}

    def ingest_titles(field_name: str, level: str) -> None:
        titles = block.get(field_name, [])
        if not isinstance(titles, list):
            return
        for title in titles:
            if not isinstance(title, str):
                continue
            normalized = normalize_title(title)
            if normalized:
                overrides[normalized] = level

    ingest_titles("poster_titles", "poster")
    ingest_titles("oral_titles", "oral")
    ingest_titles("bestpaper_titles", "bestpaper")

    paper_levels = block.get("paper_levels")
    if isinstance(paper_levels, dict):
        for title, level in paper_levels.items():
            if not isinstance(title, str) or not isinstance(level, str):
                continue
            normalized_title = normalize_title(title)
            normalized_level = level.lower().strip()
            if normalized_title and normalized_level in VALID_PRESENTATION_LEVELS:
                overrides[normalized_title] = normalized_level

    return overrides


def work_to_record(
    work: Dict[str, Any],
    default_level: str,
    title_level_overrides: Dict[str, str],
    track: str = TRACK_MAIN,
) -> Dict[str, Any]:
    title = str(work.get("title") or work.get("display_name") or "").strip()
    normalized_title = normalize_title(title)

    authors: List[str] = []
    institutions: List[str] = []
    authorships = work.get("authorships")
    if isinstance(authorships, list):
        for authorship in authorships:
            if not isinstance(authorship, dict):
                continue
            author = authorship.get("author")
            if isinstance(author, dict):
                name = author.get("display_name")
                if isinstance(name, str) and name.strip():
                    authors.append(name.strip())

            insts = authorship.get("institutions")
            if isinstance(insts, list):
                for inst in insts:
                    if not isinstance(inst, dict):
                        continue
                    inst_name = inst.get("display_name")
                    if isinstance(inst_name, str) and inst_name.strip():
                        institutions.append(inst_name.strip())

    abstract = rebuild_abstract(work.get("abstract_inverted_index"))
    keywords = extract_keywords(work)
    presentation_level = title_level_overrides.get(normalized_title, default_level)

    record = {
        "paper_title": title,
        "authors": unique_preserve_order(authors),
        "institutions": unique_preserve_order(institutions),
        "abstract": abstract,
        "keywords": keywords,
        "presentation_level": presentation_level,
        "openalex_id": work.get("id"),
        "doi": work.get("doi"),
    }
    set_record_track_fields(record=record, track=track)
    return record


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch all papers for a venue-year target and save as JSON. "
            "Example target: AAAI-26 or CVPR-2026."
        )
    )
    parser.add_argument("target", help="Venue-year token, e.g., AAAI-26, CVPR-2026")
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSON path",
    )
    parser.add_argument(
        "--overrides",
        help=(
            "Optional JSON file for presentation-level overrides. "
            "See references/presentation_overrides_template.json."
        ),
    )
    parser.add_argument(
        "--source-id",
        help=(
            "Optional OpenAlex source ID to skip venue auto-matching, "
            "for example https://openalex.org/S4306400393."
        ),
    )
    parser.add_argument(
        "--source-name",
        help="Optional source display name when --source-id is provided.",
    )
    parser.add_argument(
        "--mailto",
        help="Optional contact email appended to OpenAlex requests (recommended).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENALEX_API_KEY", ""),
        help=(
            "OpenAlex API key. If omitted, reads OPENALEX_API_KEY env var. "
            "OpenAlex requires API key access for non-trivial usage."
        ),
    )
    parser.add_argument(
        "--provider",
        default="auto",
        choices=("auto", "openalex", "openreview"),
        help=(
            "Data provider strategy. auto: OpenAlex first, then OpenReview fallback "
            "when OpenAlex count is too low for supported venues."
        ),
    )
    parser.add_argument(
        "--openreview-threshold",
        type=int,
        default=OPENREVIEW_LOW_COUNT_THRESHOLD,
        help=(
            "When provider=auto, fallback to OpenReview if OpenAlex paper count is below this value."
        ),
    )
    parser.add_argument(
        "--reconcile-url",
        help=(
            "Optional external checklist URL (for example NeurIPS virtual papers page). "
            "Titles from this page are used for count reconciliation."
        ),
    )
    parser.add_argument(
        "--reconcile-include-missing",
        action="store_true",
        help=(
            "When reconciliation finds titles missing from provider results, append "
            "placeholder entries so output covers the external checklist."
        ),
    )
    parser.add_argument(
        "--reconcile-drop-extra",
        action="store_true",
        help=(
            "When reconciliation is enabled, drop provider-only titles that are not "
            "present in the external checklist."
        ),
    )
    parser.add_argument(
        "--progress",
        dest="progress",
        action="store_true",
        default=True,
        help="Show progress bars while fetching (default: enabled).",
    )
    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Disable progress bars.",
    )
    parser.add_argument(
        "--max-papers",
        type=int,
        default=0,
        help="Optional cap for fetched papers, useful for debugging.",
    )
    parser.add_argument(
        "--default-level",
        default="poster",
        choices=sorted(VALID_PRESENTATION_LEVELS),
        help="Default presentation level before applying overrides.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retry attempts per request (default: {DEFAULT_RETRIES}).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation spaces (default: 2).",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.retries < 0:
        parser.error("--retries must be >= 0")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    if args.openreview_threshold < 0:
        parser.error("--openreview-threshold must be >= 0")
    if args.provider != "openreview" and not str(args.api_key or "").strip():
        parser.error(
            "--api-key is required (or set OPENALEX_API_KEY in environment)."
        )

    try:
        venue_code, year, short_key, canonical_key = parse_target(args.target)
    except ValueError as exc:
        log(f"[ERROR] {exc}")
        return 2

    output_path = Path(args.output).expanduser().resolve()
    overrides_path = Path(args.overrides).expanduser().resolve() if args.overrides else None
    max_papers = args.max_papers if args.max_papers > 0 else None

    try:
        title_overrides = load_presentation_overrides(
            overrides_path=overrides_path,
            keys=(canonical_key, short_key, short_key.upper(), canonical_key.upper()),
        )
        if title_overrides:
            log(f"[INFO] Loaded {len(title_overrides)} title-level presentation overrides.")

        official_track_metadata: Optional[Dict[str, Any]] = None
        official_track_index: Dict[str, Dict[str, str]] = {}
        official_track_assignment: Optional[Dict[str, int]] = None
        if venue_code == "NEURIPS":
            try:
                official_track_metadata = load_neurips_official_track_index(
                    year=year,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                official_track_index = official_track_metadata.get("title_index", {})
                track_catalog = official_track_metadata.get("track_catalog", [])
                track_preview = {
                    str(item.get("track")): int(item.get("paper_count", 0))
                    for item in track_catalog
                    if isinstance(item, dict)
                }
                log(
                    "[INFO] Official NeurIPS tracks: "
                    f"url={official_track_metadata.get('url')}, "
                    f"tracks={len(track_preview)}, "
                    f"papers={official_track_metadata.get('results_count')}, "
                    f"track_counts={track_preview}"
                )
            except Exception as exc:
                log(f"[WARN] Failed to load official NeurIPS tracks: {exc}")

        openalex_records: List[Dict[str, Any]] = []
        openalex_filter_strategy = "none"
        selected_source: Optional[Dict[str, Any]] = None
        source_id = ""
        source_name = ""
        source_type = ""

        if args.provider in ("auto", "openalex"):
            if args.source_id:
                selected_source = {
                    "id": args.source_id,
                    "display_name": args.source_name or venue_code,
                    "type": "",
                    "match_score": None,
                    "year_count": None,
                }
            else:
                log(f"[INFO] Resolving OpenAlex source for {venue_code}-{year} ...")
                selected_source = pick_source(
                    venue_code,
                    year,
                    mailto=args.mailto,
                    timeout=args.timeout,
                    retries=args.retries,
                    api_key=args.api_key,
                )

            source_id = str(selected_source["id"])
            source_name = str(selected_source.get("display_name") or venue_code)
            source_type = str(selected_source.get("type") or "")
            log(f"[INFO] Using source: {source_name} ({source_id})")
            if isinstance(selected_source.get("year_count"), int):
                log(
                    "[INFO] Estimated papers in selected source for target year: "
                    f"{selected_source['year_count']}"
                )

            works, openalex_filter_strategy = fetch_works(
                source_id=source_id,
                year=year,
                mailto=args.mailto,
                timeout=args.timeout,
                retries=args.retries,
                max_papers=max_papers,
                api_key=args.api_key,
                show_progress=args.progress,
            )
            log(f"[INFO] Work filter strategy: {openalex_filter_strategy}")

            openalex_records = [
                work_to_record(
                    work=work,
                    default_level=args.default_level,
                    title_level_overrides=title_overrides,
                    track=TRACK_MAIN,
                )
                for work in works
            ]
            openalex_records = [
                record for record in openalex_records if record.get("paper_title")
            ]

        openreview_records: List[Dict[str, Any]] = []
        openreview_meta: Dict[str, str] = {}
        openreview_venue_id = ""

        should_try_openreview = args.provider == "openreview" or (
            args.provider == "auto"
            and venue_code in OPENREVIEW_VENUE_ID_PATTERNS
            and len(openalex_records) < args.openreview_threshold
        )
        if should_try_openreview:
            venue_ids = build_openreview_venue_ids(venue_code, year)
            if not venue_ids and args.provider == "openreview":
                raise RuntimeError(
                    f"No OpenReview venue-id template configured for venue '{venue_code}'."
                )

            best_notes: List[Dict[str, Any]] = []
            for venue_id in venue_ids:
                notes, meta = fetch_openreview_notes_for_venue(
                    venue_id=venue_id,
                    timeout=args.timeout,
                    retries=args.retries,
                    show_progress=args.progress,
                )
                log(
                    "[INFO] OpenReview candidate "
                    f"{venue_id}: {len(notes)} papers "
                    f"(base={meta.get('base_url') or 'n/a'}, "
                    f"key={meta.get('filter_key') or 'n/a'}, "
                    f"mode={meta.get('mode') or 'n/a'}, "
                    f"content={meta.get('content_count') or '0'}, "
                    f"invitation={meta.get('invitation_count') or '0'})"
                )
                if len(notes) > len(best_notes):
                    best_notes = notes
                    openreview_meta = meta
                    openreview_venue_id = venue_id

            if best_notes:
                openreview_records = [
                    openreview_note_to_record(
                        note=note,
                        default_level=args.default_level,
                        title_level_overrides=title_overrides,
                    )
                    for note in best_notes
                ]
                openreview_records = [
                    record
                    for record in openreview_records
                    if record.get("paper_title")
                ]

        final_provider = "openalex"
        final_records = openalex_records
        final_filter_strategy = openalex_filter_strategy
        final_source_display_name = source_name
        final_source_type = source_type
        final_openalex_source_id: Optional[str] = source_id or None
        final_openreview_venue_id: Optional[str] = None
        source_year_count_estimate = (
            selected_source.get("year_count") if isinstance(selected_source, dict) else None
        )

        if args.provider == "openreview":
            if not openreview_records:
                raise RuntimeError(
                    "OpenReview provider selected, but no papers were retrieved."
                )
            final_provider = "openreview"
            final_records = openreview_records
            final_filter_strategy = (
                f"openreview:{openreview_meta.get('base_url','')}:{openreview_meta.get('filter_key','')}"
            )
            final_source_display_name = f"OpenReview {openreview_venue_id}"
            final_source_type = "conference"
            final_openalex_source_id = None
            final_openreview_venue_id = openreview_venue_id
            source_year_count_estimate = None
            log(
                f"[INFO] Using OpenReview results: {len(openreview_records)} papers from {openreview_venue_id}"
            )
        elif args.provider == "auto":
            if len(openreview_records) > len(openalex_records):
                final_provider = "openreview"
                final_records = openreview_records
                final_filter_strategy = (
                    f"openreview:{openreview_meta.get('base_url','')}:{openreview_meta.get('filter_key','')}"
                )
                final_source_display_name = f"OpenReview {openreview_venue_id}"
                final_source_type = "conference"
                final_openalex_source_id = None
                final_openreview_venue_id = openreview_venue_id
                source_year_count_estimate = None
                log(
                    "[INFO] Auto provider switched to OpenReview due to larger paper count: "
                    f"{len(openreview_records)} vs {len(openalex_records)}"
                )

        reconciliation: Optional[Dict[str, Any]] = None
        if args.reconcile_url:
            log(f"[INFO] Loading external checklist: {args.reconcile_url}")
            html = http_get_text(
                url=args.reconcile_url,
                timeout=args.timeout,
                retries=args.retries,
            )
            external_entries = extract_external_titles(
                html=html,
                page_url=args.reconcile_url,
            )
            if not external_entries:
                raise RuntimeError(
                    "External checklist loaded but no paper titles were extracted. "
                    "Check the URL or parser rules."
                )
            external_track_counts = build_track_counts(
                [{"track": entry.get("track", TRACK_OTHER)} for entry in external_entries]
            )
            log(
                "[INFO] External checklist parsed (noise-cleaned): "
                f"titles={len(external_entries)}, tracks={external_track_counts}"
            )
            final_records, reconciliation = reconcile_records_with_external_titles(
                records=final_records,
                external_entries=external_entries,
                include_missing=args.reconcile_include_missing,
                drop_extra=args.reconcile_drop_extra,
                default_level=args.default_level,
            )
            log(
                "[INFO] Reconcile result: "
                f"external={reconciliation['external_title_count']}, "
                f"matched={reconciliation['matched_title_count']}, "
                f"missing={reconciliation['missing_in_provider_count']}, "
                f"extra={reconciliation['extra_in_provider_count']}"
            )

        if official_track_index:
            official_track_assignment = apply_official_track_index(
                records=final_records,
                title_index=official_track_index,
            )
            has_conference_track = any(
                isinstance(item, dict) and str(item.get("track")) == "conference"
                for item in (official_track_metadata or {}).get("track_catalog", [])
            )
            remapped_main_count = 0
            if has_conference_track:
                remapped_main_count = remap_main_track_to_conference(final_records)
                official_track_assignment["remapped_main_to_conference_count"] = remapped_main_count
                official_track_assignment["updated_record_count"] += remapped_main_count
            log(
                "[INFO] Applied official track mapping: "
                f"matched={official_track_assignment['matched_record_count']}, "
                f"updated={official_track_assignment['updated_record_count']}, "
                f"unmatched={official_track_assignment['unmatched_record_count']}, "
                f"remapped_main={official_track_assignment.get('remapped_main_to_conference_count', 0)}"
            )
        else:
            for record in final_records:
                existing_group = record.get("track_group")
                set_record_track_fields(
                    record=record,
                    track=record.get("track", TRACK_OTHER),
                    track_display_name=str(record.get("track_display_name") or ""),
                    track_group=existing_group if isinstance(existing_group, str) and existing_group.strip() else None,
                )

        final_records.sort(key=lambda item: normalize_title(item.get("paper_title", "")))
        track_counts = build_track_counts(final_records)
        track_group_counts = build_track_group_counts(final_records)

        payload: Dict[str, Any] = {
            "query": {
                "target": args.target,
                "venue_code": venue_code,
                "year": year,
                "provider": final_provider,
                "api_key_used": bool(str(args.api_key or "").strip()),
                "work_filter_strategy": final_filter_strategy,
                "source_year_count_estimate": source_year_count_estimate,
            },
            "source": {
                "provider": final_provider,
                "openalex_source_id": final_openalex_source_id,
                "openreview_venue_id": final_openreview_venue_id,
                "display_name": final_source_display_name,
                "source_type": final_source_type,
            },
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "paper_count": len(final_records),
            "track_counts": track_counts,
            "track_group_counts": track_group_counts,
            "papers": final_records,
        }
        if reconciliation is not None:
            payload["reconciliation"] = {
                **reconciliation,
                "external_url": args.reconcile_url,
            }
        if official_track_metadata is not None:
            payload["official_tracks"] = {
                "source_url": official_track_metadata.get("url"),
                "paper_count_official": official_track_metadata.get("paper_count_official"),
                "results_count": official_track_metadata.get("results_count"),
                "track_catalog": official_track_metadata.get("track_catalog"),
                "conflicting_title_count": official_track_metadata.get("conflicting_title_count"),
                "assignment": official_track_assignment,
            }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=args.indent)
            handle.write("\n")

        log(f"[OK] Exported {len(final_records)} papers to {output_path}")
        return 0
    except Exception as exc:  # pragma: no cover - CLI guard
        log(f"[ERROR] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
