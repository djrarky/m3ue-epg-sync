"""
countries - Pluggable country-specific broadcast normalization rules.

To add a new country, create a YAML file in this directory (e.g. us.yaml).
The file name (without .yaml) becomes the country name.

The country is selected by the per-playlist config key `country`.

See countries/uk.yaml for a full example of all supported fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    import sys
    sys.exit("Missing dependency: pip install pyyaml")


@dataclass
class CountryRules:
    """Loaded normalization rules for a single country."""
    name: str
    canonical_subs: list[tuple[str, str]] = field(default_factory=list)
    region_abbrevs: dict[str, list[str]] = field(default_factory=dict)
    noise_tokens: set[str] = field(default_factory=set)
    non_linear_group_regex: list[str] = field(default_factory=list)
    non_linear_title_regex: list[str] = field(default_factory=list)
    country_tokens: set[str] = field(default_factory=set)
    excl_suffix_cleanup: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: Path, name: str = "") -> CountryRules:
        """Parse a country YAML file into a CountryRules instance."""
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

        canonical_subs: list[tuple[str, str]] = []
        for entry in data.get("canonical_subs", []):
            if not isinstance(entry, dict):
                continue
            pat = entry.get("pattern", "")
            rep = entry.get("replace", "")
            if pat:
                canonical_subs.append((pat, rep))

        region_abbrevs = {
            str(region): [str(a) for a in abbrs]
            for region, abbrs in (data.get("region_abbrevs") or {}).items()
        }

        excl_suffix_cleanup: list[tuple[str, str]] = []
        for entry in data.get("excl_suffix_cleanup", []):
            if not isinstance(entry, dict):
                continue
            pat = entry.get("pattern", "")
            rep = entry.get("replace", "")
            if pat:
                excl_suffix_cleanup.append((pat, rep))

        return cls(
            name=(name or path.stem).upper(),
            canonical_subs=canonical_subs,
            region_abbrevs=region_abbrevs,
            noise_tokens={str(t).upper() for t in (data.get("noise_tokens") or [])},
            non_linear_group_regex=[str(p) for p in (data.get("non_linear_group_regex") or [])],
            non_linear_title_regex=[str(p) for p in (data.get("non_linear_title_regex") or [])],
            country_tokens={str(t).lower() for t in (data.get("country_tokens") or [])},
            excl_suffix_cleanup=excl_suffix_cleanup,
        )


# ── Country discovery ─────────────────────────────────────────────────────────

_COUNTRIES_DIR = Path(__file__).parent


def available_countries(search_dir: Path | None = None) -> list[str]:
    """List available country names (auto-discovered from this directory)."""
    d = search_dir if search_dir is not None else _COUNTRIES_DIR
    return sorted(p.stem for p in d.glob("*.yaml") if not p.stem.startswith("_"))


_cache: dict[tuple[str, Path | None], CountryRules] = {}


def get_country(name: str, search_dir: Path | None = None) -> CountryRules:
    """Load country rules from a YAML file (cached).

    Auto-discovers from this directory, or from search_dir if provided.
    """
    cache_key = (name.lower(), search_dir)
    if cache_key in _cache:
        return _cache[cache_key]

    if search_dir is None:
        search_dir = _COUNTRIES_DIR

    path = search_dir / f"{name.lower()}.yaml"
    if not path.exists():
        names = available_countries(search_dir)
        raise ValueError(f"Unknown country '{name}'. Available: {', '.join(names)}")

    rules = CountryRules.from_yaml(path, name=name)
    _cache[cache_key] = rules
    return rules
