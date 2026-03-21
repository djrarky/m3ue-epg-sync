"""
sources.sky.tvchannellists — tvchannellists.com Sky UK channel list parser.

fetch() handles scraper fetching, region resolution, and parsing.
Returns standardized (number, name, category) tuples.
Categories are read from the wiki section headers (h2/h3/h4).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

from bs4 import BeautifulSoup, Tag

url = "https://www.tvchannellists.com/w/List_of_channels_on_Sky_Q_(United_Kingdom_%26_Ireland)"
fmt = "html"

from sources.sky import (
    fetch_sky_source, _resolve_rowspans, _find_col,
    _cell_name, _post_process_channels,
)


def _find_columns(table: Tag) -> tuple[int, int, bool]:
    """Find the UK EPG column, Channel name column, and region column presence.

    Returns (uk_col, name_col, has_region). Searches header rows for:
    - 'Channel name' text → name_col
    - 'UK' text (before name_col) → uk_col
    - EPG header with colspan >= 3 → has_region (3-col = UK|Region|Ire)

    Falls back to uk_col=0, name_col=-1, has_region=False if not found.
    """
    name_col = -1
    uk_col = 0  # fallback
    has_region = False

    for row in table.find_all('tr')[:5]:
        col_idx = 0
        for cell in row.find_all(['th', 'td']):
            text = cell.get_text(strip=True).lower()
            if 'channel name' in text and name_col < 0:
                name_col = col_idx
            if (name_col < 0 or col_idx < name_col) and text == 'uk':
                uk_col = col_idx
            if 'epg' in text and int(cell.get('colspan', 1) or 1) >= 3:
                has_region = True
            col_idx += int(cell.get('colspan', 1) or 1)

    return uk_col, name_col, has_region


def fetch(**kwargs) -> list[tuple[int, str, str, list[str]]]:
    """Fetch, resolve regions, parse, and return (number, name, category, aliases) tuples."""
    return fetch_sky_source(
        url=url, fmt=fmt,
        region_url=region_url, region_fmt=region_fmt,
        region_parse=region_parse, parse_fn=parse,
        source_name="tvchannellists",
        **kwargs,
    )


# ── Region mapping (TVChannelLists additional info page) ──────────────────────

region_url = "https://www.tvchannellists.com/w/Additional_information_for_List_of_channels_on_Sky_(UK_and_Ireland)"
region_fmt = "html"


def _region_parse_table(table: Tag, country: str) -> list[dict]:
    """Parse a single region mapping table into region dicts.

    Args:
        table: A <table> Tag containing region rows.
        country: Country name to attach to each region (e.g. "England", "Scotland").
    """
    grid = _resolve_rowspans(table)
    if len(grid) < 2:
        return []

    headers = grid[0]
    col_num  = _find_col(headers, 'region #')
    col_name = _find_col(headers, 'sky+ app', 'region name')
    col_bbc1 = _find_col(headers, 'bbc one')
    col_bbc2 = _find_col(headers, 'bbc two')
    col_itv  = _find_col(headers, 'stv (or itv1)', 'itv1 hd', 'itv1')
    col_itv1 = _find_col(headers, 'itv1 +1')
    col_ch4  = _find_col(headers, 'channel 4 hd', 'channel 4')
    col_ch41 = _find_col(headers, 'channel 4 +1')

    if col_num < 0 or col_name < 0:
        return []

    def _get(row: list[str], idx: int) -> str:
        return row[idx].strip() if 0 <= idx < len(row) else ''

    regions: list[dict] = []
    for row in grid[1:]:
        num_str = _get(row, col_num)
        m = re.match(r'(\d{1,2})', num_str)
        if not m:
            continue
        regions.append({
            'num':      int(m.group(1)),
            'name':     _get(row, col_name),
            'country':  country,
            'bbc_one':  _get(row, col_bbc1),
            'bbc_two':  _get(row, col_bbc2),
            'itv':      _get(row, col_itv),
            'itv_plus1': _get(row, col_itv1),
            'ch4':      _get(row, col_ch4),
            'ch4_plus1': _get(row, col_ch41),
        })
    return regions


def region_parse(content: str) -> list[dict]:
    """Parse TVChannelLists region mapping HTML into a list of region dicts.

    Each dict contains:
        num: int          -- Sky region number
        name: str         -- Sky region name (e.g. "Granada")
        country: str      -- Country (England / Wales / Northern Ireland / Scotland / Ireland)
        bbc_one: str      -- BBC One regional variant
        bbc_two: str      -- BBC Two regional variant
        itv: str          -- ITV1 / STV regional variant
        itv_plus1: str    -- ITV1 +1 regional variant
        ch4: str          -- Channel 4 regional variant
        ch4_plus1: str    -- Channel 4 +1 regional variant
    """
    if not content:
        return []

    soup = BeautifulSoup(content, 'html.parser')
    regions: list[dict] = []

    region_span = soup.find(id='Region_mapping')
    start = region_span.parent if region_span else soup

    current_country = 'England'
    for elem in start.next_siblings:
        tag = getattr(elem, 'name', None)

        if tag in ('h2', 'h3', 'h4'):
            heading = re.sub(r'\[edit\]', '', elem.get_text(strip=True)).strip()
            heading_base = re.split(r'[(\\[]', heading)[0].strip().lower()
            if 'scotland' in heading_base:
                current_country = 'Scotland'
            elif 'northern ireland' in heading_base:
                current_country = 'Northern Ireland'
            elif 'ireland' in heading_base:
                current_country = 'Ireland'
            elif 'wales' in heading_base:
                current_country = 'Wales'
            elif heading_base in ('england', 'uk', 'regions', 'key'):
                current_country = 'England'
            elif tag == 'h2':
                break

        elif tag == 'table' and 'wikitable' in elem.get('class', []):
            first_row = elem.find('tr')
            if not first_row:
                continue
            headers = [th.get_text(strip=True) for th in first_row.find_all(['th', 'td'])]
            if 'Region #' not in headers:
                continue
            regions.extend(_region_parse_table(elem, current_country))

    return regions


# ── Channel list (TVChannelLists Sky Q channel list page) ─────────────────────

# Map wiki section headers to normalized category names.
# "Secondary" sections (e.g. "Secondary Sports") inherit the parent category.
SECTION_CATEGORIES: dict[str, str] = {
    "entertainment & documentaries": "Entertainment",
    "secondary entertainment & documentaries": "Entertainment",
    "movies": "Movies",
    "secondary movies": "Movies",
    "music": "Music",
    "sports": "Sports",
    "secondary sports": "Sports",
    "interactive sports": "Sports",
    "news": "News",
    "secondary news": "News",
    "religion": "Religion",
    "kids": "Children",
    "secondary kids": "Children",
    "shopping": "Shopping",
    "international": "International",
    "secondary international": "International",
    "adult": "Adult",
    "regional & interactive": "Entertainment",
    "other": "Entertainment",
    "ultra hd & hdr": "Entertainment",
}

# Sections to skip (no useful linear TV channels)
SKIP_SECTIONS = {
    "channel format, packs and services",
    "abbreviations",
    "notes",
    "additional information",
    "contents",
    "channels on electronic programme guide",
    "premium channels and pay-per-view",
    "premium channels",
    "catch-up & on-demand",
    "streaming apps",
    "channel changes",
    "radio",
    "see also",
    "references",
}



def _extract_number(td: Tag) -> int | None:
    """Extract an EPG number from a <td> cell.

    Numbers are rendered as white text on colored backgrounds via
    {{font color|white|NNN}} → <span style="color:white">NNN</span>.
    Also handles plain text numbers.
    """
    text = td.get_text(strip=True)
    m = re.match(r'(\d{2,4})', text)
    if m:
        n = int(m.group(1))
        if 100 <= n <= 999:
            return n
    return None



def _td_at_logical_col(tds: list[Tag], logical_col: int) -> Tag | None:
    """Find the <td> element at a given logical column, accounting for colspan."""
    col = 0
    for td in tds:
        cs = int(td.get('colspan', 1))
        if col <= logical_col < col + cs:
            return td
        col += cs
    return None


def _extract_name(row: Tag, name_col: int) -> tuple[str, str]:
    """Extract the channel name and alias from a table row.

    Uses logical column index (accounting for colspan) to find the name cell.
    Falls back to sortkey from the logo cell if name cell is empty.

    Returns (name, alias) where alias is the permanent name if a rebrand
    is detected, or "" otherwise.
    """
    tds = row.find_all('td')

    td = _td_at_logical_col(tds, name_col) if name_col >= 0 else None
    if td:
        name, alias = _cell_name(td)
        if name:
            return name, alias

    # Fallback: try sortkey from the logo cell (logical col before name)
    if name_col > 0:
        logo_td = _td_at_logical_col(tds, name_col - 1)
        if logo_td:
            span = logo_td.find('span', attrs={'data-sort-value': True})
            if span:
                sv = span['data-sort-value']
                m = re.search(r'encode\|(.+?)(?:\s*!|\}\})', sv)
                if m:
                    return m.group(1).strip(), ""

    return "", ""


def parse(content: str, region: dict | None = None, country_rules=None) -> list[tuple[int, str, str, list[str]]]:
    """Parse TVChannelLists HTML into (number, name, category, aliases) tuples.

    Table structure varies by section:
      3-col EPG: Col 0: UK  |  Col 1: Region  |  Col 2: Ire
      2-col EPG: Col 0: UK  |  Col 1: Ire  (no Region column)

    region is a resolved region dict from the region mapping source.
    Used to filter rows by the Region column using the same annotation
    tokens as ViewersVault (Eng, Scot, ScotSTV, ScotITVB, NI, Wales).

    aliases contains alternate names (e.g. permanent name when a channel
    is temporarily rebranded, or old/new names from channel changes).
    """
    if not content:
        return []

    from sources.sky import region_tokens as _region_tokens
    region_tokens = _region_tokens(region) if region else None

    soup = BeautifulSoup(content, 'html.parser')

    # Collect region variant values for matching against excl. column
    _META_KEYS = {'num', 'name', 'country'}
    region_values: set[str] = set()
    if region:
        for k, v in region.items():
            if k in _META_KEYS or not isinstance(v, str):
                continue
            v = v.strip()
            if v and v.lower() != 'network':
                region_values.add(v.lower())
        if region_tokens:
            for tok in region_tokens:
                region_values.add(tok.lower())
        if country_rules:
            abbrev_to_canon: dict[str, str] = {}
            for canon_name, abbrevs in country_rules.region_abbrevs.items():
                canon = canon_name.replace("_", " ").lower()
                for abbrev in abbrevs:
                    abbrev_to_canon[abbrev.lower()] = canon
            expanded: set[str] = set()
            for rv in region_values:
                rv_canon = abbrev_to_canon.get(rv)
                if rv_canon:
                    for other_abbrev, other_canon in abbrev_to_canon.items():
                        if other_canon == rv_canon:
                            expanded.add(other_abbrev)
            region_values |= expanded

    _country_tokens = country_rules.country_tokens if country_rules else set()

    parsed: list[tuple[int, str, str, list[str]]] = []
    regional_renames: dict[str, str] = {}
    regional_aliases: dict[str, list[str]] = {}
    current_category = ""

    # Walk through headings and their associated tables
    for heading in soup.find_all(['h2', 'h3', 'h4']):
        section = heading.get_text(strip=True)
        # Remove [edit] links that MediaWiki adds
        section = re.sub(r'\[edit\]', '', section).strip().lower()

        if section in SKIP_SECTIONS:
            current_category = ""
            continue

        if section in SECTION_CATEGORIES:
            current_category = SECTION_CATEGORIES[section]
        else:
            continue  # not a channel section

        if not current_category:
            continue

        # Find the next sortable wikitable after this heading
        table = heading.find_next('table', class_='sortable')
        if not table:
            continue

        is_regional = 'regional' in section
        uk_col, name_col, has_region = _find_columns(table)

        # Regional section: process excl. entries for renames/aliases
        if is_regional and region:
            from sources.sky import _resolve_rowspans
            grid = _resolve_rowspans(table)
            for grid_row in grid:
                if len(grid_row) < 3:
                    continue
                num_text = grid_row[0].strip()
                num_match = re.match(r'(\d{2,4})', num_text)
                if not num_match:
                    continue
                uk_num = int(num_match.group(1))
                if not (100 <= uk_num <= 999):
                    continue

                excl_text = grid_row[1].strip().lower() if len(grid_row) > 1 else ''
                if not excl_text.startswith('excl.'):
                    continue
                excl_region = excl_text.removeprefix('excl.').strip()

                raw_name = grid_row[name_col].strip() if len(grid_row) > name_col else ''
                if not raw_name:
                    continue
                base = re.sub(r'\s*(HD|SD|UHD|4K)\b', '', raw_name).strip()
                base = re.sub(r'\s*\([^)]+\)\s*$', '', base).strip()
                if not base:
                    continue

                suffix = excl_region.title()
                if country_rules:
                    for pattern, replacement in country_rules.excl_suffix_cleanup:
                        suffix = re.sub(pattern, replacement, suffix, flags=re.IGNORECASE)
                    suffix = suffix.strip()

                excl_parts = {p.strip() for p in re.split(r'[,&]', excl_region) if p.strip()}
                is_own_region = region_values and (
                    excl_region in region_values
                    or bool(excl_parts & region_values)
                )
                is_country_compound = (
                    ('&' in excl_region or ',' in excl_region)
                    and all(p in _country_tokens for p in excl_parts)
                )

                if is_own_region:
                    if suffix:
                        if is_country_compound:
                            for part in excl_parts:
                                alias = f"{base} {part.title()}"
                                regional_aliases.setdefault(base.lower(), []).append(alias)
                        else:
                            regional_renames[base.lower()] = f"{base} {suffix}"
                elif uk_num is not None:
                    if is_country_compound:
                        full_name = base
                    else:
                        full_name = f"{base} {suffix}" if suffix else base
                    parsed.append((uk_num, full_name, 'Regional', []))

            continue  # Don't fall through to regular row parsing

        # Track rowspanned names so continuation rows (e.g. BBC Alba,
        # S4C with multiple region rows) inherit the channel name.
        last_rowspan_name = ""
        last_rowspan_alias = ""

        for row in table.find_all('tr'):
            tds = row.find_all('td')
            if not tds:
                continue

            is_continuation = len(tds) < 3

            # Track rowspanned names on full rows BEFORE checking the EPG
            # number — continuation rows need the name context.
            if not is_continuation:
                name_td = _td_at_logical_col(tds, name_col) if name_col >= 0 else None
                if name_td:
                    if name_td.get('rowspan'):
                        rn, ra = _extract_name(row, name_col)
                        last_rowspan_name = rn or ""
                        last_rowspan_alias = ra or ""
                    else:
                        last_rowspan_name = ""
                        last_rowspan_alias = ""

            # Extract UK EPG number
            if is_continuation:
                if not last_rowspan_name:
                    continue
                # Continuation rows: td[0] is the UK EPG number
                # (rowspanned columns before it are gone)
                uk_num = _extract_number(tds[0]) if tds else None
                if uk_num is None:
                    continue
            else:
                uk_td = tds[uk_col] if uk_col < len(tds) else tds[0]
                uk_num = _extract_number(uk_td)
                if uk_num is None:
                    continue

            # Region filtering (only for tables with a Region column)
            if has_region and region_tokens:
                if is_continuation:
                    # Continuation rows: region is in tds[1] if available
                    region_td = tds[1] if len(tds) > 1 else None
                else:
                    region_td = _td_at_logical_col(tds, 1)
                if region_td:
                    region_text = region_td.get_text(strip=True).lower()
                    if region_text and not any(
                        tok.lower() in region_text for tok in region_tokens
                    ):
                        continue  # Not available in our region

            # Extract channel name and alias
            if is_continuation:
                name = last_rowspan_name
                alias = last_rowspan_alias
            else:
                name, alias = _extract_name(row, name_col)
                if not name:
                    name = last_rowspan_name
                    alias = last_rowspan_alias

            if not name or len(name) > 60:
                continue

            # Clean up
            name = re.sub(r'\s+', ' ', name).strip().rstrip('.,:;')
            name = re.sub(r'\{\{[^}]*\}\}', '', name).strip()
            if not name:
                continue

            # Skip alias for regional sections — <small>(Region)</small>
            # is a region annotation, not a rebrand.
            if alias and is_regional:
                alias = ""

            aliases = [alias] if alias else []
            parsed.append((uk_num, name, current_category, aliases))

    return _post_process_channels(
        parsed, soup,
        regional_renames=regional_renames or None,
        regional_aliases=regional_aliases or None,
    )
