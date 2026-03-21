"""
sources.sky - Sky UK channel data sources.

Provider-specific defaults and shared parsing utilities used by
the Sky source parsers (viewersvault, tvchannellists).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    import sys
    sys.exit("Missing dependency: pip install beautifulsoup4")

logger = logging.getLogger(__name__)

# Provider-specific defaults (read by each source's fetch())
DEFAULT_REGION = 1           # London - main Sky network region

# Regex for stripping badge suffixes (HD/SD/UHD/4K) from channel names
_BADGE_RE = re.compile(r'(HD|SD|UHD|4K)\s*$', re.IGNORECASE)


# ── Shared HTML table utilities ───────────────────────────────────────────────

def _resolve_rowspans(table: Tag) -> list[list[str]]:
    """Resolve rowspan/colspan attributes in a table into a flat grid of text values."""
    rows = table.find_all('tr')
    grid: list[list[str]] = []
    pending: dict[int, tuple[int, str]] = {}

    for row in rows:
        cells = row.find_all(['td', 'th'])
        row_data: list[str] = []
        col_idx = 0
        cell_iter = iter(cells)

        while True:
            if col_idx in pending:
                remaining, val = pending[col_idx]
                row_data.append(val)
                if remaining > 1:
                    pending[col_idx] = (remaining - 1, val)
                else:
                    del pending[col_idx]
                col_idx += 1
            else:
                cell = next(cell_iter, None)
                if cell is None:
                    break
                text = re.sub(r'\s+', ' ', cell.get_text(separator=' ', strip=True)).strip()
                try:
                    rs = int(cell.get('rowspan', 1))
                except (ValueError, TypeError):
                    rs = 1
                try:
                    cs = int(cell.get('colspan', 1))
                except (ValueError, TypeError):
                    cs = 1
                for _ in range(cs):
                    row_data.append(text)
                    if rs > 1:
                        pending[col_idx] = (rs - 1, text)
                    col_idx += 1

        while col_idx in pending:
            remaining, val = pending[col_idx]
            row_data.append(val)
            if remaining > 1:
                pending[col_idx] = (remaining - 1, val)
            else:
                del pending[col_idx]
            col_idx += 1

        if row_data:
            grid.append(row_data)

    return grid


def _find_col(headers: list[str], *patterns: str) -> int:
    """Find the column index matching any of the given patterns (case-insensitive)."""
    for i, h in enumerate(headers):
        h_lower = h.lower()
        for pat in patterns:
            if pat.lower() in h_lower:
                return i
    return -1



def _cell_name(td: Tag) -> tuple[str, str]:
    """Extract channel name and alias from a cell.

    If the cell has a <small> tag with a parenthesized name (permanent
    channel name under a temporary rebrand), the current name is primary
    and the permanent name is returned as an alias.

    Walks child nodes and stops at styled badge spans (HD/SD/+1/4:3 SD),
    including +1 badges (red background) as part of the name.

    Returns (current_name, alias) where alias is the permanent name if
    a rebrand is detected, or "" otherwise.
    """
    alias = ""
    small = td.find('small')
    if small:
        small_text = small.get_text(strip=True)
        m = re.match(r'^\((.+)\)$', small_text)
        if m:
            alias = m.group(1)

    # Walk children, collecting text and stopping at badge spans
    parts: list[str] = []
    for child in td.children:
        if isinstance(child, NavigableString):
            parts.append(str(child).strip())
        elif child.name == 'a' and not child.find('img'):
            parts.append(child.get_text(strip=True))
        elif child.name in ('sup', 'small', 'img'):
            continue  # skip reference links, small tags, images
        elif child.name == 'p':
            # Some cells wrap content in <p> - recurse into it
            return _cell_name(child)
        elif child.name == 'span':
            # Red badge = +1 channel (include in name)
            style = child.get('style', '')
            badge_text = child.get_text(strip=True)
            if 'red' in style and badge_text == '+1':
                parts.append('+1')
            # Green/other badges (HD, SD, UHD) - stop
            break
        else:
            break
    name = ' '.join(p for p in parts if p)
    return name, alias


def _parse_channel_changes(
    soup: BeautifulSoup,
    *,
    default_name_offset: int = 2,
) -> list[tuple[str, str]]:
    """Extract rename pairs from the upcoming/past channel changes tables.

    Returns [(current_name, new_name), ...] for Rename/Reversion rows.
    Badge suffixes (HD/SD/+1) are stripped from names.

    The changes table has a complex multi-row header with mismatched
    column counts, so we locate rename rows by searching for the type
    keyword and extract names at fixed offsets: current name is 1 cell
    before the type, new name is ``default_name_offset`` cells after.
    """
    RENAME_TYPES = {'rename', 'temporary rename', 'reversion'}
    pairs: list[tuple[str, str]] = []

    for table in soup.find_all('table', class_='wikitable'):
        grid = _resolve_rowspans(table)
        # Verify this is a changes table
        if not any('type of change' in ' '.join(r).lower() for r in grid[:3]):
            continue

        # Compute offset between "type of change" and "new channel name"
        name_offset = default_name_offset
        for row in grid[:3]:
            rl = [h.lower() for h in row]
            if 'type of change' in rl and 'new channel name' in rl:
                name_offset = rl.index('new channel name') - rl.index('type of change')
                break

        for row in grid:
            for i, cell in enumerate(row):
                if cell.strip().lower() in RENAME_TYPES and i >= 1 and i + name_offset < len(row):
                    current = _BADGE_RE.sub('', row[i - 1].strip()).strip()
                    new_name = _BADGE_RE.sub('', row[i + name_offset].strip()).strip()
                    if current and new_name and current.lower() != new_name.lower():
                        pairs.append((current, new_name))
                    break

    return pairs


# ── Region resolution ─────────────────────────────────────────────────────────

def _find_region(regions: list[dict], query: str | int) -> dict | None:
    """Find a region by number, Sky name, or broadcaster region name."""
    if isinstance(query, int) or (isinstance(query, str) and query.isdigit()):
        num = int(query)
        for r in regions:
            if r['num'] == num:
                return r
        return None

    q = query.strip().lower()
    for r in regions:
        if r['name'].lower() == q:
            return r
    for r in regions:
        if r['bbc_one'].lower() == q:
            return r
    for r in regions:
        if r['itv'].lower() == q:
            return r
    return None


def resolve_region(
    region_value: str | int,
    region_url: str,
    region_fmt: str,
    region_parse: callable,
    scraper_name: str,
    api_key: str,
    cache_days: int,
    cache_dir: Path,
    timeout: int = 60,
    max_retries: int = 3,
) -> dict:
    """Resolve a region value into a region dict for parse() filtering."""
    from scrapers import fetch_cached

    content, from_cache, _ = fetch_cached(
        scraper_name=scraper_name,
        api_key=api_key,
        url=region_url,
        fmt=region_fmt,
        cache_dir=cache_dir,
        cache_days=cache_days,
        timeout=timeout,
        max_retries=max_retries,
    )
    if not content:
        raise RuntimeError("No content returned from region source.")

    regions = region_parse(content)
    if not regions:
        raise RuntimeError("No regions parsed from region source.")

    region = _find_region(regions, region_value)
    if not region:
        available = ", ".join(f"{r['num']}={r['name']}" for r in regions[:15])
        raise ValueError(f"Region '{region_value}' not found. Available: {available}")

    logger.info(
        "Region %d (%s): BBC One=%s, ITV=%s, Ch4=%s (%s)",
        region['num'], region['name'],
        region.get('bbc_one', '?'), region.get('itv', '?'),
        region.get('ch4', '?'), "cache" if from_cache else "network",
    )
    return region


def fetch_sky_source(
    *,
    url: str,
    fmt: str,
    region_url: str,
    region_fmt: str,
    region_parse: callable,
    parse_fn: callable,
    source_name: str,
    scraper_name: str,
    api_key: str,
    cache_days: int,
    cache_dir: Path,
    region_value: int | str | None = None,
    force_refresh: bool = False,
    scraping_timeout: int = 60,
    max_retries: int = 3,
    country_rules=None,
    min_channels: int = 100,
    max_drop_percent: int = 20,
    **_kwargs,
) -> list[tuple[int, str, str, list[str]]]:
    """Shared fetch logic for all Sky web sources.

    Handles region resolution, scraper caching, and logging. Each source
    module provides its own URL, format, region parser, and channel parser.
    """
    from scrapers import fetch_cached

    if region_value is None:
        region_value = DEFAULT_REGION

    region = resolve_region(
        region_value, region_url, region_fmt, region_parse,
        scraper_name, api_key, cache_days, cache_dir,
        timeout=scraping_timeout, max_retries=max_retries,
    )

    # Snapshot previous cache count before fetch overwrites it.
    from scrapers import cache_file_path
    cp = cache_file_path(cache_dir, url, fmt)
    prev_count = 0
    if cp.exists():
        try:
            prev_content = cp.read_text(encoding="utf-8")
            prev_channels = parse_fn(prev_content, region=region, country_rules=country_rules)
            prev_count = len(prev_channels)
        except Exception:
            pass  # can't parse old cache - skip

    content, from_cache, cache_path = fetch_cached(
        scraper_name=scraper_name, api_key=api_key,
        url=url, fmt=fmt,
        cache_dir=cache_dir, cache_days=cache_days,
        force_refresh=force_refresh,
        timeout=scraping_timeout, max_retries=max_retries,
    )
    if not content:
        raise RuntimeError(f"No content returned from scraper for {source_name}")

    state = "cache" if from_cache else "network"
    logger.info("%s chars received (%s: %s).", f"{len(content):,}", state, cache_path)

    channels = parse_fn(content, region=region, country_rules=country_rules)
    logger.info("%d channel entries parsed.", len(channels))

    # Guard against badly rendered pages - if the scraper returned a
    # Cloudflare challenge, empty JS shell, or truncated HTML, the parser
    # will produce far fewer channels than expected.
    #
    # Two checks:
    #   1. Hard floor - reject anything absurdly low (sanity check)
    #   2. Drop guard - if we had a previous good count, reject drops
    #      of more than 20% (catches partial renders)
    source_label = "cache" if from_cache else "fresh fetch"

    if len(channels) < min_channels:
        raise RuntimeError(
            f"{source_name}: only {len(channels)} channels parsed "
            f"(expected ≥{min_channels}) - source may not have rendered "
            f"correctly ({source_label}, {len(content):,} chars). "
            f"No changes will be made."
        )

    if not from_cache and prev_count > 0:
        drop = (prev_count - len(channels)) / prev_count * 100
        if drop > max_drop_percent:
            raise RuntimeError(
                f"{source_name}: channel count dropped {drop:.0f}% "
                f"({prev_count} → {len(channels)}) - possible render failure. "
                f"No changes will be made."
            )

    return channels


def _post_process_channels(
    parsed: list[tuple[int, str, str, list[str]]],
    soup: BeautifulSoup,
    *,
    default_name_offset: int = 2,
    regional_renames: dict[str, str] | None = None,
    regional_aliases: dict[str, list[str]] | None = None,
) -> list[tuple[int, str, str, list[str]]]:
    """Shared post-processing for Sky channel parsers.

    Applies three steps in order:
      1. Dedup by channel number (keep first, add different names as aliases)
      2. Simulcast removal (remove entries whose name or alias already exists
         at a lower channel number)
      3. Channel changes alias merging (from _parse_channel_changes)

    Optional regional_renames and regional_aliases are applied between
    steps 2 and 3 (used by viewersvault for region-specific renaming).

    Args:
        parsed: Raw list of (number, name, category, aliases) tuples.
        soup: Parsed BeautifulSoup of the page (for channel changes table).
        default_name_offset: Passed to _parse_channel_changes.
        regional_renames: Optional dict mapping base_name_lower → "Base Region".
            Applied to non-Regional entries after simulcast removal.
        regional_aliases: Optional dict mapping base_name_lower → list of aliases.
            Applied after regional renames.
    """
    # 1. Deduplicate: keep first occurrence per channel number.
    # If a second entry has the same number but a different name (timeshare),
    # add the second name as an alias on the first entry.
    seen: dict[int, int] = {}  # number → index in deduped
    deduped: list[tuple[int, str, str, list[str]]] = []
    for num, name, cat, aliases in parsed:
        if num not in seen:
            seen[num] = len(deduped)
            deduped.append((num, name, cat, aliases))
        else:
            idx = seen[num]
            existing = deduped[idx]
            if name.lower() != existing[1].lower() and name not in existing[3]:
                existing[3].append(name)

    # 2. Remove simulcasts - entries whose name already exists at a lower
    # channel number, either as a primary name or as an alias.
    name_to_lowest: dict[str, int] = {}
    for num, name, cat, aliases in deduped:
        key = name.lower()
        if key not in name_to_lowest or num < name_to_lowest[key]:
            name_to_lowest[key] = num
        for alias in aliases:
            akey = alias.lower()
            if akey not in name_to_lowest or num < name_to_lowest[akey]:
                name_to_lowest[akey] = num
    deduped = [
        (num, name, cat, aliases)
        for num, name, cat, aliases in deduped
        if name_to_lowest[name.lower()] == num
    ]

    # Apply regional renames to main entries only (not Regional category).
    if regional_renames:
        deduped = [
            (num, regional_renames.get(name.lower(), name) if cat != 'Regional' else name, cat, aliases)
            for num, name, cat, aliases in deduped
        ]

    # Apply compound-region aliases.
    if regional_aliases:
        name_idx_ra: dict[str, int] = {}
        for i, (_, name, _, _) in enumerate(deduped):
            name_idx_ra.setdefault(name.lower(), i)
        for base_lower, alias_list in regional_aliases.items():
            idx = name_idx_ra.get(base_lower)
            if idx is not None:
                existing = deduped[idx][3]
                for alias in alias_list:
                    if alias not in existing:
                        existing.append(alias)

    # 3. Merge aliases from channel changes table.
    changes = _parse_channel_changes(soup, default_name_offset=default_name_offset)
    if changes:
        name_idx: dict[str, int] = {}
        for i, (_, name, _, _) in enumerate(deduped):
            name_idx.setdefault(name.lower(), i)

        for current, new_name in changes:
            idx = name_idx.get(current.lower())
            if idx is not None:
                existing = deduped[idx][3]
                if new_name not in existing:
                    existing.append(new_name)

    return deduped


def region_tokens(region: dict) -> list[str]:
    """Derive annotation filter tokens from a resolved region dict.

    Sky channel lists use annotations like "(Eng, Wales, ScotITVB)" on
    EPG numbers. Determines the country from the region dict fields -
    primarily from 'country', falling back to 'bbc_one' for regions
    like Republic of Ireland that receive another country's channels.

      England regions  → ["Eng"]
      Wales            → ["Wales"]
      Northern Ireland → ["NI"]
      Scotland STV     → ["Scot", "ScotSTV"]
      Scotland ITVB    → ["Scot", "ScotITVB"]
      Channel Islands  → ["CI"]
    """
    country = (region.get('country') or '').lower()
    bbc_one = (region.get('bbc_one') or '').lower()
    itv = (region.get('itv') or '').lower()
    name = (region.get('name') or '').lower()

    if 'scotland' in country:
        if 'stv' in itv:
            return ['Scot', 'ScotSTV']
        if 'border' in itv:
            return ['Scot', 'ScotITVB']
        return ['Scot']
    if 'northern ireland' in country or 'northern ireland' in bbc_one:
        return ['NI']
    if 'wales' in country:
        return ['Wales']
    if 'channel' in name and 'isle' in name:
        return ['CI']
    return ['Eng']
