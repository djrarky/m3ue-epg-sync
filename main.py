#!/usr/bin/env python3
"""
m3ue-epg-sync - Apply Sky source and guide data to IPTV playlist channels.

Fetch source channel data, match against IPTV channels, and optionally
write updates (stream_id, title, name, channel_number, sort_order, group_id)
back to m3u-editor. Set sync.dry_run: false to enable writes.
"""
from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("Missing dependency: pip install pyyaml")

from countries import get_country


def _parse_bool(value: object, key: str) -> bool:
    """Parse a config value as bool. Rejects quoted strings like "false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0", ""):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    sys.exit(f"Config error: {key} must be true or false, got {value!r}")
from guide import fetch_guide
from iptv import IPTVClient, apply_scope, apply_writes
from matcher import MatchResult, Matcher, SourceEntry
from reporter import update_summary_writes, write_reports
from sources import fetch_source

logger = logging.getLogger(__name__)

# Keys that must be present after merging "all" defaults with per-playlist overrides.
# Keys with None default are required; keys with a value have that default.
_PLAYLIST_DEFAULTS = {
    "country": None,
    "provider": None,
    "provider_source": None,
    "guide_epg_name": None,
    "scraping_api": None,
    "scraping_api_key": None,
    "region": None,
    "include_group_prefixes": [],
    "include_managed_groups": True,
    "exclude_group_regex": [],
    "preserve_groups": [],
    "preserve_group_name_regex": [],
    "preserve_channel_title_regex": [],
    "fuzzy_threshold": 90,
    "min_auto_score": 90,
    "min_auto_margin": 12,
    "scraping_timeout_seconds": 60,
    "source_cache_days": 2,
    "source_cache_dir": ".cache/scraper",
    "min_channels": 100,
    "max_drop_percent": 20,
}

# Keys only required for web sources (have file extension = file source)
_WEB_ONLY_KEYS = {"scraping_api", "scraping_api_key"}


def _load_config(path: Path) -> dict:
    """Load and validate the top-level config file."""
    if not path.exists():
        sys.exit(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        sys.exit(f"Config file has invalid YAML syntax: {exc}")

    if not isinstance(data, dict):
        sys.exit(f"Config error: expected a YAML mapping, got {type(data).__name__}")

    # Validate required global keys
    app = data.get("app", {})
    if not isinstance(app, dict):
        sys.exit("Config error: 'app' must be a mapping")
    if not app.get("base_url"):
        sys.exit("Config error: app.base_url is required")
    if not app.get("api_key"):
        sys.exit("Config error: app.api_key is required")

    return data


_SOURCE_KWARG_MAP = {
    "scraping_api": "scraper_name",
    "scraping_api_key": "api_key",
    "source_cache_days": "cache_days",
    "region": "region_value",
    "scraping_timeout_seconds": "scraping_timeout",
    "min_channels": "min_channels",
    "max_drop_percent": "max_drop_percent",
}


def _validate_playlist_config(config: dict) -> tuple[dict, list[dict], bool]:
    """Validate playlist config and merge 'all' defaults. Fail-fast on errors.

    Called before any API calls so config errors surface immediately.

    Returns: (all_defaults, explicit_playlists, needs_discovery)
        - all_defaults: merged defaults from uuid: "all"
        - explicit_playlists: list of validated+merged playlist dicts
        - needs_discovery: True if uuid: "all" is the only entry
          (playlist UUIDs must be discovered from the API)
    """
    playlists_raw = config.get("playlists", [])
    if not isinstance(playlists_raw, list):
        sys.exit("Config error: 'playlists' must be a list")
    if not playlists_raw:
        sys.exit("Config error: no playlists defined")

    all_defaults = {}
    real_playlists = []
    for p in playlists_raw:
        if not isinstance(p, dict):
            sys.exit(f"Config error: each playlist entry must be a mapping, got {type(p).__name__}")
        if p.get("uuid") == "all":
            all_defaults = {k: v for k, v in p.items() if k != "uuid"}
        else:
            real_playlists.append(p)

    needs_discovery = not real_playlists

    # Validate defaults have required keys (except uuid) when using discovery
    if needs_discovery:
        # Validate against a dummy playlist - all keys come from all_defaults
        test_merged = {**all_defaults}
        for key, default in _PLAYLIST_DEFAULTS.items():
            if key not in test_merged and default is not None:
                test_merged[key] = default
        is_file_source = "." in str(test_merged.get("provider_source", ""))
        missing = []
        for key, default in _PLAYLIST_DEFAULTS.items():
            if default is None and key not in test_merged:
                if key in _WEB_ONLY_KEYS and is_file_source:
                    continue
                missing.append(key)
        if missing:
            sys.exit(f"Config error: 'all' defaults missing required keys: {', '.join(missing)}")

    # Validate explicit playlists
    prepared = []
    for playlist in real_playlists:
        if not playlist.get("uuid"):
            sys.exit("Config error: each playlist entry must have a 'uuid' key")
        merged = {**all_defaults, **{k: v for k, v in playlist.items() if v is not None}}

        for key, default in _PLAYLIST_DEFAULTS.items():
            if key not in merged and default is not None:
                merged[key] = default

        is_file_source = "." in str(merged.get("provider_source", ""))

        missing = []
        for key, default in _PLAYLIST_DEFAULTS.items():
            if default is None and key not in merged:
                if key in _WEB_ONLY_KEYS and is_file_source:
                    continue
                missing.append(key)
        if missing:
            logger.error(
                "Playlist %s missing required keys: %s - skipping",
                merged.get("uuid", "?"), ", ".join(missing),
            )
            continue

        prepared.append(merged)

    if not needs_discovery and not prepared:
        sys.exit("Config error: no valid playlists after validation")

    return all_defaults, prepared, needs_discovery


def _prepare_playlists(
    all_defaults: dict,
    validated: list[dict],
    needs_discovery: bool,
    api_playlists: list[dict],
) -> list[dict]:
    """Finalize playlist list and pre-compute source kwargs.

    If needs_discovery is True, UUIDs are taken from api_playlists.
    Otherwise, validated playlists are used as-is.

    Each returned dict has all playlist config keys plus a 'source_kwargs'
    dict ready to pass to fetch_source().
    """
    if needs_discovery:
        playlists = []
        for pl in api_playlists:
            pl_uuid = pl.get("uuid")
            if pl_uuid:
                merged = {**all_defaults, "uuid": pl_uuid}
                for key, default in _PLAYLIST_DEFAULTS.items():
                    if key not in merged and default is not None:
                        merged[key] = default
                playlists.append(merged)
        if not playlists:
            sys.exit("Config error: no playlists found (none in config, none from API)")
    else:
        playlists = validated

    # Pre-compute source kwargs
    for merged in playlists:
        source_kwargs = {"cache_dir": Path(merged["source_cache_dir"])}
        for config_key, kwarg_name in _SOURCE_KWARG_MAP.items():
            if config_key in merged:
                source_kwargs[kwarg_name] = merged[config_key]
        merged["source_kwargs"] = source_kwargs

    return playlists



def _validate_data(
    source_entries: list[SourceEntry],
    guide_map: dict[int, str],
    playlist_uuid: str,
) -> bool:
    """Validate source entries and guide map before matching."""
    if not source_entries:
        logger.error("Playlist %s: source returned 0 entries - skipping", playlist_uuid)
        return False

    dupes = [n for n, c in Counter(e.number for e in source_entries).items() if c > 1]
    if dupes:
        logger.error("Playlist %s: duplicate source numbers %s - skipping", playlist_uuid, dupes)
        return False

    if not guide_map:
        logger.error("Playlist %s: guide map is empty - skipping", playlist_uuid)
        return False

    return True



def _run_playlist(playlist_config: dict, app_config: dict, sync_config: dict, client: IPTVClient, epg_list: list[dict] | None = None, known_uuids: set[str] | None = None) -> bool:
    """Run the full pipeline for one playlist. Returns True on success."""
    uuid = playlist_config["uuid"]
    base_url = app_config["base_url"]
    api_key = app_config["api_key"]
    dry_run = _parse_bool(sync_config.get("dry_run", True), "sync.dry_run")
    max_retries = int(sync_config.get("max_retries", 3))
    request_timeout = int(sync_config.get("request_timeout_seconds", 20))

    total_steps = 6 if dry_run else 7
    step = 0

    # ── Step 1: Pre-flight validation ────────────────────────────────────
    step += 1
    print(f"[{step}/{total_steps}] Loading config...")

    country_rules = get_country(playlist_config["country"])

    if not client.validate_playlist(uuid, known_uuids=known_uuids):
        logger.error("Playlist %s: playlist UUID validation failed - skipping", uuid)
        return False

    # ── Step 2: Fetch source ─────────────────────────────────────────────
    step += 1
    provider = playlist_config["provider"]
    provider_source = playlist_config["provider_source"]
    source_kwargs = {
        **playlist_config["source_kwargs"],
        "country_rules": country_rules,
        "max_retries": max_retries,
    }

    print(f"[{step}/{total_steps}] Fetching source ({provider_source})...", end="  ", flush=True)

    raw_tuples = fetch_source(provider, provider_source, **source_kwargs)
    source_entries = [SourceEntry(*t) for t in raw_tuples]

    print(f"{len(source_entries)} channels")

    # ── Step 3: Fetch guide XML ──────────────────────────────────────────
    step += 1
    print(f"[{step}/{total_steps}] Fetching guide XML...", end="  ", flush=True)

    guide_map = fetch_guide(
        base_url=base_url, api_key=api_key,
        epg_name=playlist_config["guide_epg_name"],
        timeout=request_timeout, max_retries=max_retries,
        epg_list=epg_list,
    )

    print(f"{len(guide_map)} channels")

    if not _validate_data(source_entries, guide_map, uuid):
        return False

    # ── Step 4: Fetch IPTV channels ──────────────────────────────────────
    step += 1
    print(f"[{step}/{total_steps}] Fetching IPTV channels...", end="  ", flush=True)

    all_channels = client.fetch_channels(uuid)

    # Auto-include groups managed by m3ue-epg-sync (derived from source categories)
    if playlist_config.get("include_managed_groups", False) and _parse_bool(playlist_config["include_managed_groups"], "include_managed_groups"):
        managed_names = sorted({e.category for e in source_entries})
        existing = list(playlist_config.get("include_group_prefixes", []))
        merged = list(dict.fromkeys(existing + managed_names))  # preserve order, dedupe
        playlist_config = {**playlist_config, "include_group_prefixes": merged}
        logger.info("include_managed_groups: added %d source categories to include_group_prefixes", len(managed_names))

    in_scope, excluded = apply_scope(all_channels, playlist_config, country_rules)

    print(f"{len(all_channels):,} fetched, {len(in_scope)} in scope, {len(excluded)} excluded")

    if not in_scope:
        logger.error("Playlist %s: 0 channels in scope after filtering - skipping", uuid)
        return False

    # ── Step 5: Match ────────────────────────────────────────────────────
    step += 1
    print(f"[{step}/{total_steps}] Matching...", end="  ", flush=True)

    matcher = Matcher(
        source_entries=source_entries,
        guide_map=guide_map,
        country_rules=country_rules,
        playlist_config=playlist_config,
    )

    match_results: list[tuple[dict, MatchResult]] = []
    for ch in in_scope:
        result = matcher.match(ch)
        match_results.append((ch, result))

    # Count decisions
    counts: dict[str, int] = {}
    for _, r in match_results:
        counts[r.decision] = counts.get(r.decision, 0) + 1

    auto = counts.get("auto_apply", 0) + counts.get("fingerprint", 0)
    review = counts.get("needs_review", 0)
    no_match = counts.get("unmatched", 0)

    print(f"{auto} auto_apply, {review} needs_review, {no_match} unmatched")

    # Warn about source channels with no IPTV match
    matched_numbers = {r.source.number for _, r in match_results if r.source}
    no_iptv = sum(1 for e in source_entries if e.number not in matched_numbers)
    if no_iptv:
        print(f"      \u26a0 {no_iptv} source channels had no IPTV match (see source_map.yaml)")

    # ── Step 6: Report ───────────────────────────────────────────────────
    step += 1
    output_dir = Path(sync_config.get("output_dir", "./out"))
    playlist_out = output_dir / uuid

    print(f"[{step}/{total_steps}] Writing reports...", end="  ", flush=True)

    summary = write_reports(
        output_dir=playlist_out,
        source_entries=source_entries,
        match_results=match_results,
        excluded_channels=excluded,
        guide_map=guide_map,
    )

    print(f"./{playlist_out}/")

    if dry_run:
        print("Done. (dry run \u2014 no writes)")
        return True

    # ── Step 7: Apply writes ──────────────────────────────────────────────
    step += 1
    print(f"[{step}/{total_steps}] Applying writes...", end="  ", flush=True)

    # Only auto_apply and fingerprint decisions get written
    writable = [
        (ch, result) for ch, result in match_results
        if result.decision in ("auto_apply", "fingerprint") and result.source
    ]

    # 7a. Ensure groups exist and sort_order is correct
    write_summary = apply_writes(
        client, uuid, writable, source_entries, guide_map,
        disable_other_groups=_parse_bool(sync_config.get("disable_other_groups", False), "sync.disable_other_groups"),
        all_iptv_channels=all_channels,
    )

    written = write_summary["channels_written"]
    skipped = write_summary["channels_skipped"]
    failed_writes = write_summary["channels_failed"]
    aborted = write_summary["aborted"]

    if aborted:
        print(f"ABORTED (group write failed)")
    else:
        print(f"{written} written, {skipped} skipped, {failed_writes} failed")

    update_summary_writes(playlist_out, write_summary)

    print("Done.")
    return not aborted


def _parse_schedule(value: str) -> int | None:
    """Parse a schedule interval string into seconds.

    Supported formats: "30m", "6h", "1d", or raw seconds as int.
    Returns None if not set or empty.
    """
    if not value:
        return None
    value = value.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value[-1] in multipliers:
        try:
            seconds = int(value[:-1]) * multipliers[value[-1]]
        except ValueError:
            seconds = None
        else:
            if seconds > 0:
                return seconds
            sys.exit(f"Config error: schedule must be positive, got '{value}'")
    try:
        seconds = int(value)
    except ValueError:
        sys.exit(f"Config error: invalid schedule '{value}' - expected e.g. '6h', '30m', '1d'")
    if seconds <= 0:
        sys.exit(f"Config error: schedule must be positive, got '{value}'")
    return seconds


def _run_all(config: dict) -> bool:
    """Run the full pipeline for all playlists. Returns True if all succeeded."""
    app_config = config.get("app", {})
    sync_config = config.get("sync", {})
    reporting_config = config.get("reporting", {})

    if not isinstance(reporting_config, dict):
        sys.exit(f"Config error: 'reporting' must be a mapping, got {type(reporting_config).__name__}")

    # Merge reporting output_dir into sync_config for convenience
    if "output_dir" in reporting_config:
        sync_config["output_dir"] = reporting_config["output_dir"]

    # 1. Validate and coerce sync values before any use
    try:
        max_retries = int(sync_config.get("max_retries", 3))
        request_timeout = int(sync_config.get("request_timeout_seconds", 20))
    except (ValueError, TypeError) as exc:
        sys.exit(f"Config error: sync.max_retries and sync.request_timeout_seconds must be integers: {exc}")
    if max_retries < 1:
        sys.exit(f"Config error: sync.max_retries must be >= 1, got {max_retries}")
    if request_timeout < 1:
        sys.exit(f"Config error: sync.request_timeout_seconds must be >= 1, got {request_timeout}")

    all_defaults, validated, needs_discovery = _validate_playlist_config(config)

    # 2. Pre-flight: create client, validate API key, cache EPG + playlist lists
    client = IPTVClient(
        base_url=app_config["base_url"], api_key=app_config["api_key"],
        timeout=request_timeout, max_retries=max_retries,
    )
    try:
        epg_list = client.list_epgs()
        api_playlists = client.list_playlists()
    except RuntimeError:
        logger.error("API key validation failed")
        return False

    known_uuids = {p["uuid"] for p in api_playlists if p.get("uuid")}

    # 3. Finalize playlist list (discover UUIDs from API if needed)
    playlists = _prepare_playlists(all_defaults, validated, needs_discovery, api_playlists)
    if not playlists:
        logger.error("No valid playlists to process")
        return False

    failed: list[str] = []
    for playlist_config in playlists:
        uuid = playlist_config["uuid"]
        print(f"\n{'=' * 60}")
        print(f"Playlist: {uuid}")
        print(f"{'=' * 60}")

        try:
            success = _run_playlist(playlist_config, app_config, sync_config, client, epg_list, known_uuids)
            if not success:
                failed.append(uuid)
        except Exception:
            logger.exception("Playlist %s failed with exception", uuid)
            failed.append(uuid)

    if failed:
        print(f"\n{len(failed)} playlist(s) failed: {', '.join(failed)}", file=sys.stderr)
        return False
    return True


def main() -> None:
    """Entry point - run all playlists, optionally on a schedule."""
    import time as _time

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    config = _load_config(Path("config.yaml"))
    sync_config = config.get("sync", {})
    if not isinstance(sync_config, dict):
        sys.exit(f"Config error: 'sync' must be a mapping, got {type(sync_config).__name__}")
    interval = _parse_schedule(str(sync_config.get("schedule", "")))

    if interval is None:
        # Single run
        success = _run_all(config)
        if not success:
            sys.exit(1)
    else:
        # Scheduled loop with consecutive failure limit
        try:
            max_consecutive_failures = int(sync_config.get("max_consecutive_failures", 5))
        except (ValueError, TypeError):
            sys.exit("Config error: sync.max_consecutive_failures must be an integer")
        if max_consecutive_failures < 1:
            sys.exit(f"Config error: sync.max_consecutive_failures must be >= 1, got {max_consecutive_failures}")
        consecutive_failures = 0
        schedule_label = sync_config.get("schedule", "")
        print(f"Schedule: every {schedule_label} ({interval}s)")

        while True:
            try:
                success = _run_all(config)
                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
            except Exception:
                logger.exception("Run failed")
                consecutive_failures += 1

            if consecutive_failures >= max_consecutive_failures:
                sys.exit(f"Aborting: {max_consecutive_failures} consecutive failures")

            if consecutive_failures > 0:
                print(
                    f"\nRun failed ({consecutive_failures}/{max_consecutive_failures} "
                    f"before abort). Next attempt in {schedule_label}...",
                    file=sys.stderr,
                )
            else:
                print(f"\nNext run in {schedule_label}. Waiting...")
            _time.sleep(interval)


if __name__ == "__main__":
    main()
