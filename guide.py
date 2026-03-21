"""
guide - Fetch guide XML and build channel number → stream_id map.

The guide XML uses XMLTV format. Each <channel> element has an id attribute
(the stream_id) and one or more <display-name> children. The number is
extracted from <display-name> elements, not from the id string - the id
format is opaque and varies between guide sources.

Returns a dict[int, str] mapping channel number → stream_id.
"""
from __future__ import annotations

import logging
from xml.etree.ElementTree import fromstring

from http_utils import request_with_retry

logger = logging.getLogger(__name__)


def _resolve_guide_uuid(
    base_url: str,
    api_key: str,
    epg_name: str,
    timeout: int = 20,
    max_retries: int = 3,
) -> str:
    """Resolve a guide EPG name to its UUID via GET /user/epgs."""
    resp = request_with_retry(
        "GET", f"{base_url}/user/epgs",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout, max_retries=max_retries,
        context="EPG list",
    )

    epgs = resp.json()
    if isinstance(epgs, dict):
        epgs = epgs.get("data", epgs.get("epgs", []))

    for epg in epgs:
        if epg.get("name") == epg_name or epg.get("uuid") == epg_name:
            uuid = epg.get("uuid")
            if not uuid:
                raise ValueError(f"Guide EPG '{epg_name}' matched but has no UUID")
            return uuid

    available = [e.get("name", "?") for e in epgs[:10]]
    raise ValueError(
        f"Guide EPG '{epg_name}' not found. Available: {', '.join(available)}"
    )


def _fetch_guide_xml(
    base_url: str,
    guide_uuid: str,
    timeout: int = 20,
    max_retries: int = 3,
) -> str:
    """Download guide XML - public endpoint, no auth required."""
    resp = request_with_retry(
        "GET", f"{base_url}/epgs/{guide_uuid}/epg.xml",
        timeout=timeout, max_retries=max_retries,
        context="Guide XML",
    )
    return resp.text


def _parse_guide_xml(xml_text: str) -> dict[int, str]:
    """Parse XMLTV guide XML into a number → stream_id map.

    Raises ValueError on duplicate numbers or duplicate stream_ids -
    either would corrupt Tier 1 fingerprinting or guide lookups.
    """
    try:
        root = fromstring(xml_text)
    except Exception as exc:
        raise ValueError(f"Failed to parse guide XML: {exc}") from exc
    guide_map: dict[int, str] = {}
    reverse: dict[str, int] = {}

    for channel_el in root.findall("channel"):
        stream_id = channel_el.get("id", "")
        if not stream_id:
            continue

        number = None
        for dn in channel_el.findall("display-name"):
            text = (dn.text or "").strip()
            if text.isdigit() and not text.startswith("0"):
                number = int(text)
                break

        if number is not None:
            if number in guide_map:
                raise ValueError(
                    f"Duplicate guide number {number}: "
                    f"{guide_map[number]} and {stream_id}"
                )
            if stream_id in reverse:
                raise ValueError(
                    f"Duplicate guide stream_id {stream_id!r}: "
                    f"numbers {reverse[stream_id]} and {number}"
                )
            guide_map[number] = stream_id
            reverse[stream_id] = number

    return guide_map


def resolve_epg_uuid(epg_name: str, epg_list: list[dict]) -> str:
    """Resolve an EPG name to its UUID from a pre-fetched EPG list."""
    for epg in epg_list:
        if epg.get("name") == epg_name or epg.get("uuid") == epg_name:
            uuid = epg.get("uuid")
            if not uuid:
                raise ValueError(f"Guide EPG '{epg_name}' matched but has no UUID")
            return uuid
    available = [e.get("name", "?") for e in epg_list[:10]]
    raise ValueError(
        f"Guide EPG '{epg_name}' not found. Available: {', '.join(available)}"
    )


def fetch_guide(
    base_url: str,
    api_key: str,
    epg_name: str,
    timeout: int = 20,
    max_retries: int = 3,
    epg_list: list[dict] | None = None,
) -> dict[int, str]:
    """Fetch guide XML and return number → stream_id map.

    If epg_list is provided, resolves the UUID from it (avoids a
    redundant /user/epgs API call).
    """
    if epg_list is not None:
        guide_uuid = resolve_epg_uuid(epg_name, epg_list)
    else:
        guide_uuid = _resolve_guide_uuid(
            base_url, api_key, epg_name,
            timeout=timeout, max_retries=max_retries,
        )
    logger.info("Resolved guide '%s' → UUID %s", epg_name, guide_uuid)

    xml_text = _fetch_guide_xml(
        base_url, guide_uuid,
        timeout=timeout, max_retries=max_retries,
    )
    logger.info("Guide XML: %s chars received.", f"{len(xml_text):,}")

    guide_map = _parse_guide_xml(xml_text)
    logger.info("Guide map: %d channels parsed.", len(guide_map))

    return guide_map
