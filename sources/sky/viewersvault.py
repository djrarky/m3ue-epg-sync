"""
sources.sky.viewersvault - viewersvault.com Sky UK channel list parser.

fetch() handles scraper fetching, region resolution, and parsing.
Returns standardized (number, name, category) tuples.
Categories are read from the wiki section headers (h3/h2).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

from bs4 import BeautifulSoup, NavigableString, Tag

url = "https://viewersvault.com/List_of_channels_on_Sky_(United_Kingdom_and_Ireland)"
fmt = "html"

from sources.sky import (
    fetch_sky_source, _resolve_rowspans, _find_col,
    _cell_name, _post_process_channels,
)


def _find_columns(table: Tag) -> tuple[int, int]:
    """Find the Sky Q EPG column and Channel name column indexes.

    Returns (skyq_col, name_col). Searches header rows for:
    - 'Channel name' text → name_col (scans all header rows)
    - Sky_Q.svg href (in EPG columns before name_col) → skyq_col

    Falls back to skyq_col=1, name_col=-1 if not found.
    """
    # Pass 1: find name_col
    name_col = -1
    for row in table.find_all('tr'):
        col_idx = 0
        for cell in row.find_all(['th', 'td']):
            if 'channel name' in cell.get_text(strip=True).lower():
                name_col = col_idx
                break
            col_idx += int(cell.get('colspan', 1) or 1)
        if name_col >= 0:
            break

    # Pass 2: find skyq_col (only in EPG columns before name_col)
    skyq_col = 1  # fallback
    for row in table.find_all('tr')[:5]:
        col_idx = 0
        for cell in row.find_all(['th', 'td']):
            if col_idx >= name_col > 0:
                break
            a_tag = cell.find('a')
            href = a_tag.get('href', '') if a_tag else ''
            if 'Sky_Q' in href:
                skyq_col = col_idx
                return skyq_col, name_col
            col_idx += int(cell.get('colspan', 1) or 1)

    return skyq_col, name_col


def fetch(**kwargs) -> list[tuple[int, str, str, list[str]]]:
    """Fetch, resolve regions, parse, and return (number, name, category, aliases) tuples."""
    return fetch_sky_source(
        url=url, fmt=fmt,
        region_url=region_url, region_fmt=region_fmt,
        region_parse=region_parse, parse_fn=parse,
        source_name="viewersvault",
        **kwargs,
    )


# ── Region mapping (ViewersVault region page) ─────────────────────────────────

region_url = "https://viewersvault.com/Region_mapping_on_Sky_(United_Kingdom_and_Ireland)"
region_fmt = "html"


def _region_parse_table(table: Tag) -> list[dict]:
    """Parse a single region mapping table into region dicts."""
    grid = _resolve_rowspans(table)
    if len(grid) < 2:
        return []

    headers = grid[0]
    col_num  = _find_col(headers, 'region #', 'region')
    col_name = _find_col(headers, 'sky+ app', 'sky region', 'region name')
    col_bbc1 = _find_col(headers, 'bbc one')
    col_bbc2 = _find_col(headers, 'bbc two')
    col_itv  = _find_col(headers, 'itv1 hd', 'stv/itv1 hd', 'itv1')
    col_itv1 = _find_col(headers, 'itv1 +1')
    col_ch4  = _find_col(headers, 'channel 4 hd')
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
            'bbc_one':  _get(row, col_bbc1),
            'bbc_two':  _get(row, col_bbc2),
            'itv':      _get(row, col_itv),
            'itv_plus1': _get(row, col_itv1),
            'ch4':      _get(row, col_ch4),
            'ch4_plus1': _get(row, col_ch41),
        })
    return regions


def region_parse(content: str) -> list[dict]:
    """Parse ViewersVault region mapping HTML into a list of region dicts.

    Each dict contains:
        num: int          -- Sky region number
        name: str         -- Sky region name (e.g. "Granada")
        country: str      -- Country from section heading (e.g. "England", "Scotland")
        bbc_one: str      -- BBC One region name
        bbc_two: str      -- BBC Two region name
        itv: str          -- ITV1 region name
        itv_plus1: str    -- ITV1 +1 region name
        ch4: str          -- Channel 4 region name
        ch4_plus1: str    -- Channel 4 +1 region name
    """
    if not content:
        return []

    soup = BeautifulSoup(content, 'html.parser')
    regions: list[dict] = []

    for table in soup.find_all('table', class_='wikitable'):
        first_th = table.find('th')
        if not first_th or 'region' not in first_th.get_text(strip=True).lower():
            continue

        heading = table.find_previous(['h2', 'h3'])
        country = ''
        if heading:
            country = re.sub(r'\[edit\]', '', heading.get_text(strip=True)).strip()
            country = re.split(r'[(/]', country)[0].strip()

        table_regions = _region_parse_table(table)
        for r in table_regions:
            r['country'] = country
        regions.extend(table_regions)

    return regions


# ── Channel list (ViewersVault Sky channel list page) ─────────────────────────

# Map wiki section headers to normalized category names.
SECTION_CATEGORIES: dict[str, str] = {
    "entertainment & documentaries": "Entertainment",
    "entertainment & documentaries +1": "Entertainment",
    "entertainment & documentaries (regional & interactive)": "Regional",
    "regional & interactive": "Regional",
    "movies": "Movies",
    "music": "Music",
    "sports": "Sports",
    "news": "News",
    "religion": "Religion",
    "kids": "Children",
    "shopping": "Shopping",
    "international": "International",
    "secondary & information": "Entertainment",
    "adult": "Adult",
}

# Sections to skip (no useful linear TV channels)
SKIP_SECTIONS = {
    "radio",
    "fast",
    "catch-up & on-demand",
    "streaming apps",
}




def _extract_number(td: Tag, tokens: list[str] | None = None) -> int | None:
    """Extract an EPG number from a <td> cell, filtering by region annotation.

    Cells may contain region annotations like "103(Eng, Wales, ScotITVB)"
    or "103(ScotSTV)" or "103(NI)". If tokens is set, only return the
    number if any token matches the annotation (or there is no annotation).
    """
    text = td.get_text(strip=True)
    m = re.match(r'(\d{2,4})(.*)', text)
    if not m:
        return None
    n = int(m.group(1))
    if not (100 <= n <= 999):
        return None

    annotation = m.group(2).strip()
    if not annotation or not tokens:
        return n  # No annotation or no filter → keep

    # Annotation exists - check if any of our tokens are listed
    ann_lower = annotation.lower()
    for tok in tokens:
        if tok.lower() in ann_lower:
            return n

    return None  # Annotation doesn't include our region


def _extract_name(row: Tag, name_col: int) -> tuple[str, str]:
    """Extract the channel name and alias from a table row at the given column.

    Returns (name, alias) where alias is the permanent name if a rebrand
    is detected, or "" otherwise.
    """
    tds = row.find_all('td')
    if name_col >= 0 and len(tds) > name_col:
        name, alias = _cell_name(tds[name_col])
        if name:
            return name, alias

    # Fallback: look through non-number cells
    for td in tds[2:]:
        name, alias = _cell_name(td)
        if name and len(name) >= 2 and not re.match(r'^\d{1,4}$', name):
            return name, alias
    return "", ""


def _extract_regional_name(td: Tag) -> tuple[str, str]:
    """Extract base channel name and region from a regional section cell.

    Regional cells have format like "BBC One<span>HD</span><small>(North West)</small>"
    where the region is in a <small>(Region)</small> tag after the badge.
    Returns (base, region). E.g. → ("BBC One", "North West").

    Can't use _cell_name here because its <small> shortcut would
    return the region as the channel name.
    """
    # Extract region from <small>(Region)</small>
    region_name = ''
    small = td.find('small')
    if small:
        small_text = small.get_text(strip=True)
        m = re.match(r'^\((.+)\)$', small_text)
        if m:
            region_name = m.group(1).strip()

    # Extract base name: walk children, stop at first badge span
    parts: list[str] = []
    for child in td.children:
        if isinstance(child, NavigableString):
            parts.append(str(child).strip())
        elif child.name == 'a' and not child.find('img'):
            parts.append(child.get_text(strip=True))
        elif child.name in ('sup', 'small', 'img'):
            continue
        elif child.name == 'span':
            break
        else:
            break
    base = ' '.join(p for p in parts if p)
    return base, region_name


def parse(content: str, region: dict | None = None, country_rules=None) -> list[tuple[int, str, str, list[str]]]:
    """Parse ViewersVault HTML into (number, name, category, aliases) tuples.

    Each table row has 4 EPG number columns:
      Col 0: UK Sky Glass  |  Col 1: UK Sky Q  |  Col 2: IE Glass  |  Col 3: IE Q
    Colors: green/lightgreen = HD, no color = SD, red = +1.

    We prefer the UK Sky Q number (col 1) as it has unique ranges per
    section (600+ Kids, 400+ Sports).

    region is a resolved region dict from the region mapping source.
    Used to filter EPG numbers by annotation (e.g. "103(Eng, Wales, ScotITVB)").
    Regional channels are merged into main entries when the region dict is set.

    aliases contains alternate names (e.g. permanent name when a channel
    is temporarily rebranded).
    """
    from sources.sky import region_tokens
    tokens = region_tokens(region) if region else None
    if not content:
        return []

    soup = BeautifulSoup(content, 'html.parser')

    # Collect region variant values for matching against excl. column.
    # Includes broadcaster region names (e.g. "north west", "granada")
    # and country-level tokens (e.g. "eng", "scot") so that entries like
    # "excl. Eng & Scot" are recognized as the user's own region.
    _META_KEYS = {'num', 'name', 'country'}
    region_values: set[str] = set()
    if region:
        for k, v in region.items():
            if k in _META_KEYS or not isinstance(v, str):
                continue
            v = v.strip()
            if v and v.lower() != 'network':
                region_values.add(v.lower())
        # Add country-level tokens (Eng, Scot, NI, Wales, CI)
        if tokens:
            for tok in tokens:
                region_values.add(tok.lower())
        # Expand region_values using region_abbrevs so that abbreviated
        # region mapping values match their full forms from excl. columns.
        # E.g. "e yorks & lincs" → also adds "east yorkshire & lincolnshire".
        if country_rules:
            abbrev_to_canon: dict[str, str] = {}
            for canon_name, abbrevs in country_rules.region_abbrevs.items():
                canon = canon_name.replace("_", " ").lower()
                for abbrev in abbrevs:
                    abbrev_to_canon[abbrev.lower()] = canon
            # For each region_value, find its canonical form and add all
            # other abbreviations that map to the same canonical form.
            expanded: set[str] = set()
            for rv in region_values:
                rv_canon = abbrev_to_canon.get(rv)
                if rv_canon:
                    # Add all other abbreviations for the same canonical form
                    for other_abbrev, other_canon in abbrev_to_canon.items():
                        if other_canon == rv_canon:
                            expanded.add(other_abbrev)
            region_values |= expanded

    parsed: list[tuple[int, str, str, list[str]]] = []
    # Regional entries: (base_name, region_suffix) pairs keyed by excl. match
    # Used to rename main entries after parsing all sections.
    regional_renames: dict[str, str] = {}  # base_name_lower → "Base Region"
    regional_aliases: dict[str, list[str]] = {}  # base_name_lower → ["Base Country1", ...]

    # Country-level tokens from country rules (used to detect compound
    # country regions like "Eng & Scot" vs sub-regional "North East & Cumbria").
    _country_tokens = country_rules.country_tokens if country_rules else set()
    current_category = ""

    # Walk through headings and their associated tables
    for heading in soup.find_all(['h2', 'h3']):
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

        # Find the next sortable wikitable after this heading.
        # Small legend/key tables lack "sortable" - skip them.
        table = heading.find_next('table', class_='sortable')
        if not table:
            continue

        # Detect table layout
        skyq_col, name_col = _find_columns(table)

        # Regional section: emit other-region channels (951+) and build
        # rename map for the user's own region (to rename main entries).
        if current_category == 'Regional':
            for row in table.find_all('tr'):
                tds = row.find_all('td')
                if len(tds) < 3:
                    continue

                # Extract EPG number (same logic as regular sections,
                # but no region token filter - these are explicit per-region rows)
                if skyq_col > 0 and len(tds) > skyq_col:
                    uk_num = _extract_number(tds[skyq_col])
                else:
                    uk_num = _extract_number(tds[0])

                # Find excl. text in any cell before the name column.
                # Table layouts vary (2 or 3 EPG columns), so search
                # rather than hardcode the column index.
                excl_text = ''
                for td in tds[:name_col]:
                    t = td.get_text(strip=True).lower()
                    if t.startswith('excl.'):
                        excl_text = t
                        break

                if not excl_text:
                    continue
                excl_region = excl_text.removeprefix('excl.').strip()

                # Extract base name and combine with region from excl.
                # Some HTML versions include region in the cell (via <small>),
                # others have plain text - either way, excl_region is reliable.
                name_td = tds[name_col] if name_col >= 0 and len(tds) > name_col else tds[2]
                base, cell_region = _extract_regional_name(name_td)
                # Prefer cell region if present, otherwise derive from excl.
                # excl_region is lowercased - title-case it for the channel name.
                # Clean up region suffix for display using country rules'
                # excl_suffix_cleanup patterns.
                if cell_region:
                    region_suffix = cell_region
                else:
                    suffix = excl_region.title()
                    if country_rules:
                        for pattern, replacement in country_rules.excl_suffix_cleanup:
                            suffix = re.sub(pattern, replacement, suffix, flags=re.IGNORECASE)
                    suffix = suffix.strip()
                    region_suffix = suffix

                if not base:
                    continue

                # Check if this excl. region matches the user's region.
                # Split compound regions (e.g. "eng & scot", "eng, wales, scotstvb")
                # and check if any part matches a region value exactly.
                excl_parts = {
                    p.strip()
                    for p in re.split(r'[,&]', excl_region)
                    if p.strip()
                }
                is_own_region = region_values and (
                    excl_region in region_values
                    or bool(excl_parts & region_values)
                )

                if is_own_region:
                    # User's own region - already at the main channel number
                    # (e.g. 101), so just rename the main entry.
                    if region_suffix:
                        # Check if this is a country-level compound
                        # (e.g. "Eng & Scot") vs a regional compound
                        # (e.g. "North East & Cumbria").
                        # Country tokens: Eng, Scot, NI, Wales, CI etc.
                        is_country_compound = (
                            ('&' in excl_region or ',' in excl_region)
                            and all(p in _country_tokens for p in excl_parts)
                        )
                        if is_country_compound:
                            # Multiple countries share the same feed.
                            # Don't rename, but add aliases for each country
                            # so "BBC TWO SCOTLAND" matches the main entry.
                            for part in excl_parts:
                                alias = f"{base} {part.title()}"
                                regional_aliases.setdefault(base.lower(), []).append(alias)
                        else:
                            regional_renames[base.lower()] = f"{base} {region_suffix}"
                elif uk_num is not None:
                    # Other region - emit with the region suffix so each
                    # regional entry is distinguishable (e.g. "BBC One London",
                    # "BBC One Scotland"). For country-level compounds
                    # (e.g. "Eng & Scot"), use the base name only -
                    # the simulcast filter will remove it.
                    is_country_compound = (
                        ('&' in excl_region or ',' in excl_region)
                        and tokens
                        and all(p in {t.lower() for t in tokens} for p in excl_parts)
                    )
                    if is_country_compound:
                        full_name = base
                    else:
                        full_name = f"{base} {region_suffix}" if region_suffix else base
                    parsed.append((uk_num, full_name, 'Regional', []))
            continue  # Don't fall through to regular row parsing

        # Parse each data row (non-regional sections).
        # Track the last rowspanned name/alias so continuation rows
        # (e.g. BBC Alba with multiple EPG number rows) inherit it.
        # Track rowspanned names so continuation rows (e.g. BBC Alba
        # with multiple EPG number rows) inherit the channel name.
        last_rowspan_name = ""
        last_rowspan_alias = ""

        for row in table.find_all('tr'):
            tds = row.find_all('td')
            if not tds:
                continue

            is_continuation = len(tds) <= name_col

            # Track rowspanned names on full rows BEFORE checking the EPG
            # number - even if this row's number doesn't match our region,
            # continuation rows need the name context.
            if not is_continuation and len(tds) > name_col:
                cur_name, cur_alias = _extract_name(row, name_col)
                name_td = tds[name_col]
                if name_td.get('rowspan'):
                    last_rowspan_name = cur_name or ""
                    last_rowspan_alias = cur_alias or ""
                else:
                    last_rowspan_name = ""
                    last_rowspan_alias = ""

            if is_continuation:
                if not last_rowspan_name:
                    continue
                # Continuation rows may have 1-2 EPG tds. When there are 2,
                # td[1] is Sky Q (preferred) and td[0] is Glass. However,
                # td[1] might belong to a DIFFERENT channel if that channel
                # starts its rowspan on this row.
                #
                # Safety check: if td[0] also passes the region filter,
                # both tds belong to the same channel - safe to use td[1].
                # If only td[1] passes (td[0] fails), td[1] may be from
                # a different channel - fall back to td[0] only.
                uk_num = None
                if skyq_col > 0 and len(tds) >= 2:
                    num0 = _extract_number(tds[0], tokens)
                    num1 = _extract_number(tds[1], tokens)
                    if num0 is not None and num1 is not None:
                        # Both match - prefer the one with a region annotation
                        # matching our tokens (it's region-specific). A bare
                        # number is just a default. When Glass has rowspan,
                        # td[0] shifts to the Sky Q column position.
                        ann0 = '(' in tds[0].get_text(strip=True)
                        ann1 = '(' in tds[1].get_text(strip=True)
                        if ann0 and not ann1:
                            uk_num = num0  # td[0] is region-specific
                        else:
                            uk_num = num1  # prefer td[1] by default
                    elif num0 is not None:
                        uk_num = num0  # only td[0] matches
                    elif num1 is not None and (
                        tds[1].get('rowspan')
                        or '(' in tds[1].get_text(strip=True)
                    ):
                        # Safe when td[1] has rowspan (owns this row) or has
                        # a region annotation like "(Scot)" - the annotation
                        # proves it's the same channel's region-specific number.
                        uk_num = num1
                    # else: only td[1] matches with no rowspan/annotation - skip (likely different channel)
                elif len(tds) >= 1:
                    uk_num = _extract_number(tds[0], tokens)
                if uk_num is None:
                    continue
                name = last_rowspan_name
                alias = last_rowspan_alias
            else:
                if skyq_col > 0 and len(tds) > skyq_col:
                    uk_num = _extract_number(tds[skyq_col], tokens)
                else:
                    uk_num = _extract_number(tds[0], tokens)
                if uk_num is None:
                    continue
                name = cur_name if cur_name else last_rowspan_name
                alias = cur_alias if cur_name else last_rowspan_alias

            if not name or len(name) > 60:
                continue
            name = re.sub(r'\s+', ' ', name).strip().rstrip('.,:;')
            if not name:
                continue

            aliases = [alias] if alias else []
            parsed.append((uk_num, name, current_category, aliases))

    return _post_process_channels(
        parsed, soup,
        default_name_offset=3,
        regional_renames=regional_renames or None,
        regional_aliases=regional_aliases or None,
    )
