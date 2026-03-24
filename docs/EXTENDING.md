# Extending m3ue-epg-sync

This page is for contributors who want to understand the repository layout or add new
sources, countries, or scraping backends.

## Repository Structure

```text
m3ue-epg-sync/
├── main.py                 # orchestration: config, pipeline, scheduling
├── guide.py                # guide XML fetch + number -> stream_id map
├── iptv.py                 # m3u-editor API client, scope filtering, writes
├── matcher.py              # matching engine and match result types
├── reporter.py             # CSV, YAML, and summary output
├── http_utils.py           # shared retry logic for HTTP calls
├── config.example.yaml     # reference configuration
├── countries/
│   ├── __init__.py         # country loader and CountryRules dataclass
│   └── uk.yaml             # built-in UK rules
├── scrapers/
│   ├── __init__.py         # scraper discovery + cached fetch helper
│   ├── olostep.py          # Olostep backend
│   └── scrapedo.py         # scrape.do backend
├── sources/
│   ├── __init__.py         # source discovery + fetch/load dispatch
│   ├── csv.py              # shared CSV file source
│   └── sky/
│       ├── __init__.py     # shared Sky parsing and region helpers
│       ├── viewersvault.py
│       └── tvchannellists.py
└── docs/
    └── EXTENDING.md
```

## Core Data Contracts

### Source output

Every source ultimately returns:

```python
list[tuple[int, str, str, list[str]]]
```

That tuple is:

1. `number`
2. `name`
3. `category`
4. `aliases`

Example:

```python
(401, "Sky Sports Main Event", "Sports", ["Sky Sports ME"])
```

### Scraper output

Every scraper backend returns the fetched page content as a single string.

### Country rules

Every country file is YAML loaded into `CountryRules`.

## Adding a New Source

There are two source styles:

- web source: implements `fetch(**kwargs)`
- file source: implements `load(path: Path)`

### Web sources

Put provider-specific sources in:

```text
sources/<provider>/<source_name>.py
```

Minimal shape:

```python
def fetch(**kwargs) -> list[tuple[int, str, str, list[str]]]:
    ...
```

The framework resolves it from:

- `provider`
- `provider_source`

The `main.py` pipeline maps config keys into source kwargs before calling `fetch_source()`.
For web sources, that includes:

- `scraping_api -> scraper_name`
- `scraping_api_key -> api_key`
- `source_cache_days -> cache_days`
- `source_cache_dir -> cache_dir`
- `region -> region_value`
- `scraping_timeout_seconds -> scraping_timeout`
- `min_channels`
- `max_drop_percent`
- `sync.max_retries -> max_retries`
- `country_rules`

Your source does not need to use every kwarg, but it should accept extras cleanly.

### File sources

Put shared file sources in:

```text
sources/<extension>.py
```

Example: `.csv` is handled by `sources/csv.py`.

Minimal shape:

```python
from pathlib import Path

def load(path: Path) -> list[tuple[int, str, str, list[str]]]:
    ...
```

If `provider_source` contains a file extension, m3ue-epg-sync resolves the module by extension
and calls `load(path)`.

## Adding a New Scraper Backend

Add a new file under `scrapers/`:

```text
scrapers/<name>.py
```

Implement:

```python
def fetch_url(api_key: str, url: str, fmt: str = "text", timeout: int = 60) -> str:
    ...
```

Rules:

- return the fetched page content as a string
- raise on failure
- do not implement caching in the backend itself

Caching, retries, stale-cache fallback, and cache file naming are all handled by
`scrapers/__init__.py`.

## Adding a New Country

Add:

```text
countries/<name>.yaml
```

Supported fields:

- `canonical_subs`
- `region_abbrevs`
- `noise_tokens`
- `non_linear_group_regex`
- `non_linear_title_regex`
- `country_tokens`
- `excl_suffix_cleanup`

### `canonical_subs`

Ordered regex substitutions:

```yaml
canonical_subs:
  - pattern: "\\bBBC\\s*1\\b"
    replace: "BBC ONE"
```

### `region_abbrevs`

Map full region names to acceptable abbreviations:

```yaml
region_abbrevs:
  NORTH_WEST:
    - "GRANADA"
    - "NW"
```

### `noise_tokens`

Country-specific tokens stripped during normalization:

```yaml
noise_tokens:
  - "UK"
  - "IE"
```

### Non-linear exclusions

Patterns used during scope filtering:

```yaml
non_linear_group_regex:
  - "\\bREPLAY\\b"

non_linear_title_regex:
  - "\\bSERIES\\s+\\d+\\b"
```

## Discovery Rules

All three plugin layers are auto-discovered from the filesystem:

- sources: `sources/`
- scrapers: `scrapers/`
- countries: `countries/`

Underscore-prefixed files are ignored.

## Development Notes

- The source of truth for writes is the m3u-editor API, not the playlist M3U file.
- The guide is fetched fresh each run.
- Matching is intentionally centralized in `matcher.py`; source modules should focus on parsing. All matching thresholds and scorer guards are configurable per-playlist — see `config.example.yaml` for the full list under "Advanced matching tuning".
- Shared HTTP retry behavior lives in `http_utils.py`. Scraper-level retries and stale-cache fallback are in `scrapers/__init__.py`.
- Shared Sky parsing helpers live in `sources/sky/__init__.py`.

## When to Add a New Module vs Reuse an Existing One

- add a new source when the upstream page or file format is materially different
- add a new country YAML when the normalization and scope rules differ
- add a new scraper when you need a different upstream fetch service
- reuse shared helpers when the contract is already the same
