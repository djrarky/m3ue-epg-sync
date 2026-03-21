# m3ue-epg-sync

Sync IPTV channel metadata in m3u-editor from a trusted source list and guide XML.

Provider-agnostic, country-agnostic, and scraper-agnostic. The built-in focus is
on Sky UK/IE, but the repo is structured so you can add sources, countries, and scraping
backends without changing the core pipeline.

### Extending

See [docs/EXTENDING.md](docs/EXTENDING.md) for adding new sources, countries, and scrapers.

## What It Does

IPTV playlists arrive with messy, inconsistent channel names like `UK: BBC 1 HD ◉`,
`VIP: SKY SPORTS F1 ⁴ᵏ`, or `NOW: TNT SPORT ᵁᴴᴰ`, and overly broad groups like
`UK| GENERAL ᴴᴰ/ᴿᴬᵂ` or `VIP| SPORTS`. m3ue-epg-sync cleans these up by matching
channels against a trusted source of truth (e.g. the official Sky channel listing),
replacing groups with clean categories (Entertainment, Sports, Movies, News, etc.),
and writing correct metadata back to m3u-editor. If channel names change upstream,
m3ue-epg-sync will still match and update them on the next run.

The pipeline:

1. **Fetch source channels** from a web source (ViewersVault, TVChannelLists) or local CSV.
   These are the "correct" channel names, numbers, and categories.
2. **Fetch guide XML** from m3u-editor and build a `channel_number -> stream_id` map,
   so each matched channel gets the right EPG data linked.
3. **Fetch live IPTV channels** from the m3u-editor API - these are the messy ones to fix.
4. **Scope filter** - only touch channels that match your configured group prefixes.
   VOD, disabled, and non-linear content is left alone.
5. **Match** each IPTV channel to a source entry using a 4-tier matching engine:
   - **Tier 1 (Fingerprint)**: channel number already embedded in the IPTV stream ID
   - **Tier 2 (Exact)**: normalised IPTV name matches the source canonical name
   - **Tier 3 (Alias)**: normalised IPTV name matches a known alias (rebrands, abbreviations)
   - **Tier 4 (Fuzzy)**: multi-scorer fuzzy matching with margin-based confidence gating
6. **Write** diff-only updates back to m3u-editor when `sync.dry_run: false`.
   Only fields that differ are PATCHed. Already-correct channels are skipped entirely.

Normalisation strips IPTV provider prefixes (`UK:`, `VIP:`, `NOW:`), noise tokens
(`HD`, `SD`, `4K`, `FHD`), and applies country-specific canonical substitutions
(e.g. `BBC 1` -> `BBC ONE`, `SKY ACTION` -> `SKY CINEMA ACTION`). All rules live in
`countries/uk.yaml`, not in code.

## Quick Start

```bash
wget https://raw.githubusercontent.com/djrarky/m3ue-epg-sync/main/config.example.yaml
wget https://raw.githubusercontent.com/djrarky/m3ue-epg-sync/main/compose.yaml
cp config.example.yaml config.yaml
# Edit config.yaml - fill in app.base_url, app.api_key, and playlist settings
docker compose up -d
```

First run is always dry run (reports only, no writes). See `./out/<uuid>/` for results.

## Docker Compose

```yaml
services:
  m3ue-epg-sync:
    image: ghcr.io/djrarky/m3ue-epg-sync:latest
    restart: unless-stopped
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./out:/app/out
      - ./.cache:/app/.cache
```

## Configuration

`config.example.yaml` is the full reference. Key points:

| Key | Purpose |
|---|---|
| `app.base_url` | m3u-editor instance URL |
| `app.api_key` | Bearer token for API auth |
| `playlists[].uuid` | Playlist UUID (`"all"` for shared defaults) |
| `playlists[].country` | Country rules file (e.g. `"uk"`) |
| `playlists[].provider` | Source provider (e.g. `"sky"`) |
| `playlists[].provider_source` | Source module or file (e.g. `"viewersvault"`, `"channels.csv"`) |
| `playlists[].guide_epg_name` | EPG name or UUID in m3u-editor |
| `playlists[].region` | TV region number or name |
|  | Locate your region number <a href="https://viewersvault.com/Region_mapping_on_Sky_(United_Kingdom_and_Ireland)">here</a> |
| `playlists[].scraping_api` | Scraper backend (`"olostep"` or `"scrapedo"`) |
| `playlists[].scraping_api_key` | Scraper API key |
| `sync.dry_run` | `true` (default) for reports only, `false` for writes |
| `sync.schedule` | Repeat interval (e.g. `"6h"`, `"30m"`, `"1d"`). Omit for single run. |
| `sync.disable_other_groups` | Disable live groups not managed by m3ue-epg-sync |
| `playlists[].include_group_prefixes` | Limit scope to specific group prefixes (e.g. `["UK\|"]`) |
| `playlists[].min_channels` | Source guard: reject if fewer than N channels parsed (default 100) |
| `playlists[].max_drop_percent` | Source guard: reject if count drops more than N% vs cache (default 20) |

`uuid: "all"` provides shared defaults. Real playlist entries inherit from it and
can override any key. If `"all"` is the only entry, playlist UUIDs are discovered
from the API.

### Scraper Setup

m3ue-epg-sync uses a web scraper to fetch source channel listings. Two backends are
included:

| Backend | `scraping_api` | Sign up |
|---|---|---|
| [scrape.do](https://scrape.do) | `"scrapedo"` | [scrape.do/pricing](https://scrape.do/pricing/) |
| [Olostep](https://olostep.com) | `"olostep"` | [olostep.com](https://www.olostep.com/) |

Set `scraping_api` and `scraping_api_key` in your playlist config. Both backends
support JavaScript rendering, which is required for some source pages.

Scraper responses are cached locally (default 2 days, configurable via
`source_cache_days`). If a fresh fetch fails, the pipeline falls back to stale
cache when available. Source validation guards reject badly rendered pages before
they can be cached or acted on.

To add a new scraper backend, see [docs/EXTENDING.md](docs/EXTENDING.md).

### UK Sky Guide Setup

For Sky UK, the recommended guide source is
[`sky_epg_grab`](https://github.com/djrarky/sky_epg_grab). Keep its `REGION`
aligned with `playlists[].region` so regional BBC/ITV/Channel 4 variants get
correct `stream_id` coverage.

```yaml
services:
  sky_epg_grab:
    image: ghcr.io/djrarky/sky_epg_grab:latest
    ports: ["8855:8855"]
    environment:
      REGION: 7
      EPG_DAYS: 7
      REFRESH_HOURS: 48
    restart: unless-stopped
```

### Recommended m3u-editor Configuration

1. Add the `sky_epg_grab` feed as an EPG source in m3u-editor and set
   `guide_epg_name` in your m3ue-epg-sync config to that EPG source name.
2. In your playlist settings, enable auto-enable for new content:

   **Processing > Auto-Enable Settings**
   - ☑ Enable new channels  
      -  ☑ Enable EPG mapping by default  
      -  ☑ Enable merging by default
   - ☑ Enable new series

3. Run your initial playlist sync.
4. Run m3ue-epg-sync (first run is always dry run).
5. Update your playlist settings for EPG mapping and channel merging:

   **Processing > Auto-Merge Processing**
   - ☑ Enable auto-merge after sync

6. Re-run playlist sync.
7. Map EPG to playlist:
   - ☑ Overwrite channels with existing mappings
     (since m3ue-epg-sync uses channel numbers, there is no risk of overwriting unintended channels)
   - ☑ Recurring
   - ☑ Skip channels without EPG ID
   - ☑ Set preferred icon to EPG
   - ☑ Set minimum similarity to 100%

## What Gets Written

| Field | Value | From |
|---|---|---|
| `stream_id` | e.g. `"303.uk"` | Guide XML |
| `title` | e.g. `"Sky Cinema Rom-Coms"` | Source canonical name |
| `name` | e.g. `"Sky Cinema Rom-Coms"` | Source canonical name |
| `channel_number` | e.g. `303` | Source number |
| `sort_order` | e.g. `303` | Source number |
| `group_id` | Custom live group ID | Source category |

Never written: `url`, `stream_id_original`, `enabled`, `is_vod`.

### Group Management

Before writing channels, m3ue-epg-sync ensures a custom live group exists in
m3u-editor for each source category (Entertainment, Sports, Movies, News, etc.).
Groups are created via `POST /group` with `sort_order` assigned by the order
categories appear in the source data. On re-runs, `sort_order` drift is corrected
via `PATCH /group/{id}`.

When `sync.disable_other_groups: true`, live groups not managed by m3ue-epg-sync
are disabled after writes complete. Channels in those unmanaged groups are also
disabled. Channels that were reassigned to a managed group during the current run
are not affected.

## Outputs

Each playlist gets its own directory at `./out/<uuid>/`.

| File | Purpose |
|---|---|
| `auto_apply.csv` | Confident matches (written or would-be-written) |
| `needs_review.csv` | Matches below auto threshold |
| `unmatched.csv` | In-scope channels with no match |
| `excluded.csv` | Channels excluded by scope rules |
| `source_map.yaml` | Source-centric coverage view |
| `summary.json` | Match and write totals |

## Safety

- `sync.dry_run: true` is the default. Writes require explicit opt-out.
- Only `auto_apply` and `fingerprint` decisions are written.
- Writes are diff-only. Already-correct channels are skipped.
- Source validation rejects badly rendered pages (channel count floor + drop guard).
- Guide validation rejects duplicate numbers and duplicate stream IDs.
- Scope and preserve rules keep untouched channels untouched.
- Scheduled mode exits after consecutive failures (configurable via `sync.max_consecutive_failures`).
- Client errors (401, 403, 404) fail immediately without retrying.

## License

See [LICENSE.md](LICENSE.md).
