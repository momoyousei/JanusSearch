#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Declarative registry for supported venue collectors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from janussearch.domain.errors import ConfigurationError


@dataclass(frozen=True)
class CollectorSpec:
    """One collector implementation and its operational characteristics."""

    venues: tuple[str, ...]
    module: str
    provider: str
    mode: str = "batch"
    venue_option: str | None = None
    supports_abstracts: bool = True
    extra_args: tuple[str, ...] = ()

    def command(self, *, venue: str, years: str, output_root: Path) -> List[str]:
        """Build module arguments without embedding a Python executable."""
        normalized = venue.upper()
        if normalized not in self.venues:
            raise ConfigurationError(
                f"Collector {self.module} does not support venue {normalized}"
            )
        if self.mode == "target":
            parsed_years = parse_years(years)
            if len(parsed_years) != 1:
                raise ConfigurationError(
                    f"{normalized} generic collection accepts one year per run; got {years}"
                )
            year = parsed_years[0]
            output = output_root / f"{normalized}-{str(year)[-2:]}.json"
            return [
                "-m",
                self.module,
                f"{normalized}-{year}",
                "--output",
                str(output),
                *self.extra_args,
            ]

        command = ["-m", self.module]
        if self.venue_option:
            command.extend([self.venue_option, normalized])
        command.extend(
            [
                "--years",
                years,
                "--output-root",
                str(output_root),
                "--index-root",
                str(output_root.parent / "reports"),
                *self.extra_args,
            ]
        )
        return command


def parse_years(raw: str) -> List[int]:
    """Parse comma-separated years and inclusive year ranges."""
    years: List[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ConfigurationError(f"Invalid year range: {token}") from exc
            if end < start:
                raise ConfigurationError(f"Invalid descending year range: {token}")
            years.extend(range(start, end + 1))
        else:
            try:
                years.append(int(token))
            except ValueError as exc:
                raise ConfigurationError(f"Invalid year: {token}") from exc
    if not years:
        raise ConfigurationError("At least one year is required")
    if any(year < 1900 or year > 2100 for year in years):
        raise ConfigurationError(f"Year outside supported range: {raw}")
    return sorted(set(years))


_SPECS: tuple[CollectorSpec, ...] = (
    CollectorSpec(
        ("AAAI",),
        "janussearch.collectors.aaai",
        "aaai_ojs",
        extra_args=("--no-openreview-fallback",),
    ),
    CollectorSpec(("ACL",), "janussearch.collectors.acl", "acl_anthology"),
    CollectorSpec(("AISTATS",), "janussearch.collectors.aistats", "pmlr"),
    CollectorSpec(
        ("CVPR", "ICCV"),
        "janussearch.collectors.cvpr",
        "cvf",
        venue_option="--venue",
        extra_args=("--source", "openaccess"),
    ),
    CollectorSpec(("IJCAI",), "janussearch.collectors.ijcai", "ijcai"),
    CollectorSpec(
        ("KDD",),
        "janussearch.collectors.kdd",
        "kdd_official_acm_dblp_openalex",
    ),
    CollectorSpec(("TPAMI",), "janussearch.collectors.tpami", "dblp_openalex"),
    CollectorSpec(
        ("ICDE", "SIGIR", "ACMMM", "WWW"),
        "janussearch.collectors.dblp_expand",
        "dblp_openalex",
        venue_option="--venues",
    ),
    CollectorSpec(
        ("ICLR", "ICML", "NEURIPS", "ECCV"),
        "janussearch.collectors.virtual",
        "official_virtual",
        mode="target",
    ),
    CollectorSpec(("VLDB",), "janussearch.collectors.pvldb", "pvldb_official"),
)

_BY_VENUE: Dict[str, CollectorSpec] = {
    venue: spec for spec in _SPECS for venue in spec.venues
}


def get_collector(venue: str) -> CollectorSpec:
    """Resolve a venue or raise a useful configuration error."""
    normalized = venue.strip().upper()
    try:
        return _BY_VENUE[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_BY_VENUE))
        raise ConfigurationError(
            f"Unsupported venue {normalized!r}; supported venues: {supported}"
        ) from exc


def list_collectors() -> Sequence[CollectorSpec]:
    """Return unique registered collector specifications."""
    return _SPECS


def supported_venues() -> Iterable[str]:
    """Return supported venue codes in deterministic order."""
    return tuple(sorted(_BY_VENUE))
