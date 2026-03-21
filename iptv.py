"""
iptv - m3u-editor API client.

Read and write operations: fetch channels/groups, scope filtering,
create/update groups, update channels.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

from countries import CountryRules
from http_utils import request_with_retry

logger = logging.getLogger(__name__)


def group_name(ch: dict) -> str:
    """Extract group name from a channel dict (group may be a dict or string)."""
    group = ch.get("group", "")
    if isinstance(group, dict):
        return group.get("name", "")
    return str(group) if group else ""


class IPTVClient:
    """m3u-editor API client (read + write)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: int = 20,
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout
        self._max_retries = max_retries

    def _get(self, path: str, params: dict | None = None) -> Any:
        """GET with retries."""
        resp = request_with_retry(
            "GET", f"{self._base_url}{path}",
            params=params, session=self._session,
            timeout=self._timeout, max_retries=self._max_retries,
            context=f"GET {path}",
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"API returned invalid JSON for GET {path}") from exc

    @staticmethod
    def _extract_items(data: Any, *keys: str) -> list[dict]:
        """Extract item list from API response (handles list or dict wrapper).

        Validates the result is actually a list. Raises RuntimeError if the
        API returns an unexpected shape (e.g. a string or nested dict).
        """
        result = None
        if isinstance(data, list):
            result = data
        elif isinstance(data, dict):
            for key in keys:
                if key in data:
                    result = data[key]
                    break
        if result is None:
            return []
        if not isinstance(result, list):
            raise RuntimeError(
                f"API returned unexpected data shape: expected list, "
                f"got {type(result).__name__}"
            )
        return [item for item in result if isinstance(item, dict)]

    def fetch_channels(
        self,
        playlist_uuid: str,
        enabled: bool = True,
        is_vod: bool = False,
    ) -> list[dict]:
        """Fetch all channels for a playlist (paginated)."""
        channels: list[dict] = []
        limit = 500
        offset = 0

        while True:
            params: dict[str, Any] = {
                "playlist_uuid": playlist_uuid,
                "limit": limit,
                "offset": offset,
            }
            if enabled is not None:
                params["enabled"] = str(enabled).lower()
            if is_vod is not None:
                params["is_vod"] = str(is_vod).lower()

            data = self._get("/channel/get", params=params)
            batch = self._extract_items(data, "data", "channels")

            channels.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

        return channels

    def fetch_groups(self, playlist_uuid: str) -> list[dict]:
        """Fetch all groups for a playlist."""
        data = self._get("/group/get", params={"playlist_uuid": playlist_uuid})
        return self._extract_items(data, "data", "groups")

    def list_playlists(self) -> list[dict]:
        """Fetch all playlists for the authenticated user."""
        data = self._get("/user/playlists")
        return self._extract_items(data, "data")

    def list_epgs(self) -> list[dict]:
        """Fetch all EPG sources for the authenticated user."""
        data = self._get("/user/epgs")
        return self._extract_items(data, "data")

    def validate_playlist(self, playlist_uuid: str, known_uuids: set[str] | None = None) -> bool:
        """Pre-flight check: verify playlist UUID is valid.

        If known_uuids is provided (from list_playlists), validates against
        the cached set without a network call. Otherwise falls back to
        fetching groups.
        """
        if known_uuids is not None:
            return playlist_uuid in known_uuids
        try:
            self.fetch_groups(playlist_uuid)
            return True
        except RuntimeError:
            return False

    # ── Write methods ─────────────────────────────────────────────────────

    def _post(self, path: str, json_data: dict) -> Any:
        """POST with retries."""
        resp = request_with_retry(
            "POST", f"{self._base_url}{path}",
            json=json_data, session=self._session,
            timeout=self._timeout, max_retries=self._max_retries,
            context=f"POST {path}",
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"API returned invalid JSON for POST {path}") from exc

    def _patch(self, path: str, json_data: dict) -> Any:
        """PATCH with retries."""
        resp = request_with_retry(
            "PATCH", f"{self._base_url}{path}",
            json=json_data, session=self._session,
            timeout=self._timeout, max_retries=self._max_retries,
            context=f"PATCH {path}",
        )
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"API returned invalid JSON for PATCH {path}") from exc

    def create_group(
        self,
        playlist_uuid: str,
        name: str,
        sort_order: int,
        group_type: str = "live",
    ) -> dict:
        """Create a custom group. Returns the created group dict."""
        resp = self._post("/group", {
            "playlist_uuid": playlist_uuid,
            "name": name,
            "sort_order": sort_order,
            "type": group_type,
            "enabled": True,
        })
        # API returns {"success": true, "data": {...group...}}
        if not isinstance(resp, dict):
            raise RuntimeError(f"create_group: expected dict response, got {type(resp).__name__}")
        result = resp.get("data", resp)
        if isinstance(result, list):
            result = result[0] if result else None
        if not isinstance(result, dict) or "id" not in result:
            raise RuntimeError(f"create_group: response missing 'id': {result!r}")
        return result

    def update_group(self, group_id: int, updates: dict) -> dict:
        """Update a group (sort_order, name, enabled, type)."""
        data = self._patch(f"/group/{group_id}", updates)
        return data.get("data", data) if isinstance(data, dict) else data

    def update_channel(self, channel_id: int, updates: dict) -> dict:
        """Update a single channel. Only send fields that differ."""
        data = self._patch(f"/channel/{channel_id}", updates)
        return data.get("data", data) if isinstance(data, dict) else data


def apply_writes(
    client: IPTVClient,
    playlist_uuid: str,
    writable: list[tuple[dict, Any]],
    source_entries: list[Any],
    guide_map: dict[int, str],
    disable_other_groups: bool = False,
    all_iptv_channels: list[dict] | None = None,
) -> dict:
    """Apply group management and channel writes. Returns write summary.

    Steps:
      1. Ensure groups exist (POST /group for missing, PATCH for sort_order drift)
      2. Optionally disable live groups not managed by us
      3. Build category → group_id map
      4. Diff-only PATCH /channel/{id} for each writable channel

    Aborts channel writes if any group write fails.
    """
    summary = {
        "channels_written": 0,
        "channels_skipped": 0,
        "channels_failed": 0,
        "aborted": False,
    }

    # ── Group management ──────────────────────────────────────────────────
    category_order: dict[str, int] = {}
    for entry in source_entries:
        if entry.category not in category_order:
            category_order[entry.category] = len(category_order) + 1

    existing_groups = client.fetch_groups(playlist_uuid)
    existing_by_name: dict[str, dict] = {}
    for g in existing_groups:
        if g.get("type") == "live" and g.get("name"):
            if g["name"] in existing_by_name:
                logger.warning(
                    "Duplicate live group name %r (ids %d and %d) - using latest",
                    g["name"], existing_by_name[g["name"]]["id"], g["id"],
                )
            existing_by_name[g["name"]] = g

    category_to_group_id: dict[str, int] = {}
    try:
        for category, sort_order in category_order.items():
            if category in existing_by_name:
                group = existing_by_name[category]
                category_to_group_id[category] = group["id"]
                current_sort = group.get("sort_order")
                try:
                    current_sort = int(float(current_sort)) if current_sort is not None else None
                except (ValueError, TypeError):
                    current_sort = None
                if current_sort != sort_order:
                    client.update_group(group["id"], {"sort_order": sort_order})
                    logger.info("Updated group '%s' sort_order → %d", category, sort_order)
            else:
                group = client.create_group(
                    playlist_uuid=playlist_uuid,
                    name=category,
                    sort_order=sort_order,
                    group_type="live",
                )
                if not isinstance(group, dict) or "id" not in group:
                    raise RuntimeError(
                        f"Unexpected response creating group '{category}': {group!r}"
                    )
                category_to_group_id[category] = group["id"]
                logger.info("Created group '%s' (id=%d, sort_order=%d)", category, group["id"], sort_order)

    except Exception as exc:
        logger.error("Group write failed: %s - aborting channel writes", exc)
        summary["aborted"] = True
        return summary

    # ── Channel writes ────────────────────────────────────────────────────
    written_ids: set[int] = set()
    for ch, result in writable:
        channel_id = ch.get("id")
        source = result.source
        if not channel_id or not source:
            continue

        stream_id = guide_map.get(source.number, "")
        group_id = category_to_group_id.get(source.category)

        desired = {
            "stream_id": stream_id,
            "title": source.name,
            "name": source.name,
            "channel_number": source.number,
            "sort_order": source.number,
        }
        if group_id is not None:
            desired["group_id"] = group_id

        diff: dict = {}
        for field, value in desired.items():
            current = ch.get(field)
            if isinstance(value, int) and current is not None:
                try:
                    current = int(current)
                except (ValueError, TypeError):
                    pass
            if current != value:
                diff[field] = value

        if not diff:
            summary["channels_skipped"] += 1
            continue

        try:
            client.update_channel(channel_id, diff)
            summary["channels_written"] += 1
            written_ids.add(channel_id)
        except Exception as exc:
            logger.warning("Failed to update channel %s (%s): %s",
                           channel_id, ch.get("title", "?"), exc)
            summary["channels_failed"] += 1

    # ── Disable channels in unmanaged groups ──────────────────────────────
    if disable_other_groups and all_iptv_channels is not None:
        our_group_ids = set(category_to_group_id.values())
        unmanaged_group_ids = set()
        for g in existing_groups:
            if g.get("type") == "live" and g.get("id") not in our_group_ids:
                unmanaged_group_ids.add(g["id"])

        if unmanaged_group_ids:
            disabled_count = 0
            for ch in all_iptv_channels:
                cid = ch.get("id")
                if cid in written_ids:
                    continue  # reassigned to our group this run
                g = ch.get("group")
                raw_gid = g.get("id") if isinstance(g, dict) else g
                try:
                    gid = int(raw_gid) if raw_gid is not None else None
                except (ValueError, TypeError):
                    gid = None
                if gid in unmanaged_group_ids and ch.get("enabled", True):
                    try:
                        client.update_channel(cid, {"enabled": False})
                        disabled_count += 1
                    except Exception as exc:
                        logger.warning("Failed to disable channel %s (%s): %s",
                                       cid, ch.get("title", "?"), exc)
            if disabled_count:
                logger.info(
                    "Disabled %d channel(s) in %d unmanaged group(s)",
                    disabled_count, len(unmanaged_group_ids),
                )

    return summary


def apply_scope(
    channels: list[dict],
    playlist_config: dict,
    country_rules: CountryRules,
) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Split channels into (in_scope, excluded) based on scope rules.

    Each excluded entry is (channel_dict, reason_code).

    Reason codes:
        group_prefix    - group name didn't match include_group_prefixes
        group_excluded  - group name matched exclude_group_regex
        group_preserved - group name matched preserve_groups or preserve_group_name_regex
        title_preserved - title matched preserve_channel_title_regex
        non_linear_group - group matched non_linear_group_regex (from country rules)
        non_linear_title - title matched non_linear_title_regex (from country rules)
    """
    include_prefixes = playlist_config.get("include_group_prefixes", [])
    exclude_group_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in playlist_config.get("exclude_group_regex", [])
    ]
    preserve_group_names = set(playlist_config.get("preserve_groups", []))
    preserve_group_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in playlist_config.get("preserve_group_name_regex", [])
    ]
    preserve_title_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in playlist_config.get("preserve_channel_title_regex", [])
    ]
    non_linear_group_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in country_rules.non_linear_group_regex
    ]
    non_linear_title_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in country_rules.non_linear_title_regex
    ]

    in_scope: list[dict] = []
    excluded: list[tuple[dict, str]] = []

    for ch in channels:
        gname = group_name(ch)
        title = ch.get("title", "") or ""

        # 1. Include group prefixes (if configured)
        if include_prefixes:
            if not any(gname.startswith(p) for p in include_prefixes):
                excluded.append((ch, "group_prefix"))
                continue

        # 2. Exclude group regex
        if any(p.search(gname) for p in exclude_group_patterns):
            excluded.append((ch, "group_excluded"))
            continue

        # 3. Preserve groups (exact name)
        if gname in preserve_group_names:
            excluded.append((ch, "group_preserved"))
            continue

        # 4. Preserve group name regex
        if any(p.search(gname) for p in preserve_group_patterns):
            excluded.append((ch, "group_preserved"))
            continue

        # 5. Preserve channel title regex
        if any(p.search(title) for p in preserve_title_patterns):
            excluded.append((ch, "title_preserved"))
            continue

        # 6. Non-linear group (from country rules)
        if any(p.search(gname) for p in non_linear_group_patterns):
            excluded.append((ch, "non_linear_group"))
            continue

        # 7. Non-linear title (from country rules)
        if any(p.search(title) for p in non_linear_title_patterns):
            excluded.append((ch, "non_linear_title"))
            continue

        in_scope.append(ch)

    return in_scope, excluded
