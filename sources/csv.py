"""
sources.csv - Shared CSV channel list source.

Loads channel data from a local CSV file. No scraper needed.

Expected format (header row required):
    number,name,category,aliases
    101,BBC One,Entertainment,
    401,Sky Sports Main Event,Sports,Sky Sports ME|Sky Sports Main
    303,Sky Cinema Rom-Coms,Movies,Sky Cinema Rom Coms

Required columns: number, name, category
Optional columns: aliases (pipe-delimited, e.g. "Old Name|Alt Name")

See template.csv in the project root for a blank starting point.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load(path: Path) -> list[tuple[int, str, str, list[str]]]:
    """Load a CSV channel list file into (number, name, category, aliases) tuples."""
    if not path.exists():
        raise FileNotFoundError(f"CSV source file not found: {path}")
    results: list[tuple[int, str, str, list[str]]] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        # Validate required columns
        if reader.fieldnames is None:
            raise ValueError(f"CSV file is empty: {path}")
        missing = {"number", "name", "category"} - {h.strip().lower() for h in reader.fieldnames}
        if missing:
            raise ValueError(
                f"CSV file {path.name} missing required columns: {', '.join(sorted(missing))}. "
                f"Expected: number,name,category"
            )

        # Normalize header names (strip whitespace, lowercase)
        header_map = {h.strip().lower(): h for h in reader.fieldnames}

        for row_num, row in enumerate(reader, start=2):
            num_str = row[header_map["number"]].strip()
            name = row[header_map["name"]].strip()
            category = row[header_map["category"]].strip()

            if not num_str or not name or not category:
                continue

            try:
                num = int(num_str)
            except ValueError:
                logger.warning("Skipping CSV row %d - invalid number: %r", row_num, num_str)
                continue

            aliases_raw = row.get(header_map.get("aliases", ""), "") or ""
            aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()]

            results.append((num, name, category, aliases))

    return results
