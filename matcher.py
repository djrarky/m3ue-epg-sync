"""
matcher - Channel matching engine.

Accepts CountryRules + source entries, normalizes titles internally,
and runs exact + fuzzy matching against source names and aliases.

Data classes (SourceEntry, MatchResult) are defined here and imported
by reporter.py and main.py.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz, process

from countries import CountryRules

logger = logging.getLogger(__name__)

# Stripped from every channel title regardless of country (quality/format tokens)
_UNIVERSAL_NOISE = {"HD", "SD", "UHD", "4K", "HEVC"}


@dataclass
class SourceEntry:
    number: int
    name: str
    category: str
    aliases: list[str]


@dataclass
class MatchResult:
    source: SourceEntry | None
    tier: int | None  # 1–4, or None when unmatched
    score: int  # 0–100 (100 for tiers 1–3)
    decision: Literal["fingerprint", "auto_apply", "needs_review", "unmatched"]
    matched_on: str | None  # which normalized string triggered the match
    candidates: list[tuple[SourceEntry, int]] = field(default_factory=list)


class Matcher:
    """Per-playlist matching engine.

    Instantiated once per playlist with that playlist's source entries,
    guide map, country rules, and config thresholds.
    """

    def __init__(
        self,
        source_entries: list[SourceEntry],
        guide_map: dict[int, str],  # number → stream_id
        country_rules: CountryRules,
        playlist_config: dict,
    ) -> None:
        self._compile_patterns(country_rules)

        # ── Guide data ───────────────────────────────────────────────────
        self._valid_numbers = set(guide_map.keys())
        self._stream_id_to_num = {v: k for k, v in guide_map.items()}

        # ── Thresholds ───────────────────────────────────────────────────
        try:
            self._fuzzy_threshold = int(playlist_config["fuzzy_threshold"])
            self._min_auto_score = int(playlist_config["min_auto_score"])
            self._min_auto_margin = int(playlist_config["min_auto_margin"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Matcher threshold config must be integers: {exc}") from exc
        self._sort_floor = max(self._fuzzy_threshold - 10, 0)

        # ── Derive aliases + build indexes ────────────────────────────────
        derived_aliases = self._derive_region_aliases(source_entries, country_rules)
        self._build_indexes(source_entries, derived_aliases)

    def _compile_patterns(self, country_rules: CountryRules) -> None:
        """Pre-compile all normalization patterns from country rules."""
        self._canonical_subs = [
            (re.compile(p, re.IGNORECASE), r)
            for p, r in country_rules.canonical_subs
        ]

        self._region_abbrevs: list[tuple[re.Pattern, str]] = []
        for full_name, abbrevs in country_rules.region_abbrevs.items():
            replacement = full_name.replace("_", " ")
            for abbrev in abbrevs:
                self._region_abbrevs.append(
                    (re.compile(r"\b" + re.escape(abbrev) + r"\b", re.IGNORECASE), replacement)
                )

        noise_tokens = _UNIVERSAL_NOISE | country_rules.noise_tokens
        self._noise_re = re.compile(
            r"\b(?:" + "|".join(re.escape(t) for t in noise_tokens) + r")\b",
            re.IGNORECASE,
        )
        self._unicode_strip_re = re.compile(r"[^\x00-\x7F\s]")

    def _derive_region_aliases(
        self,
        source_entries: list[SourceEntry],
        country_rules: CountryRules,
    ) -> dict[int, list[str]]:
        """Derive bare-name aliases for regional source entries.

        Returns a dict of entry.number → [alias, ...] without mutating
        the caller's SourceEntry objects.
        """
        region_patterns: list[re.Pattern] = []
        for full_name, abbrevs in country_rules.region_abbrevs.items():
            region_patterns.append(re.compile(
                r"\b" + re.escape(full_name.replace("_", " ").upper()) + r"\b"
            ))
            for abbrev in abbrevs:
                region_patterns.append(re.compile(
                    r"\b" + re.escape(abbrev.upper()) + r"\b"
                ))

        derived: dict[int, list[str]] = {}
        for entry in source_entries:
            name_upper = entry.name.upper()
            for pat in region_patterns:
                match = pat.search(name_upper)
                if match and match.start() > 1:
                    bare = entry.name[:match.start()].strip()
                    bare_words = bare.split()
                    if (bare and bare != entry.name
                            and (len(bare_words) >= 2 or len(bare) >= 4)
                            and bare not in entry.aliases):
                        derived.setdefault(entry.number, []).append(bare)
                    break
        return derived

    def _build_indexes(
        self,
        source_entries: list[SourceEntry],
        derived_aliases: dict[int, list[str]],
    ) -> None:
        """Build exact-match indexes, fuzzy key lists, and norm cache."""
        self._name_index: dict[str, SourceEntry] = {}
        self._alias_index: dict[str, SourceEntry] = {}
        self._number_index: dict[int, SourceEntry] = {}
        self._fuzzy_keys: list[str] = []
        self._fuzzy_entries: list[SourceEntry] = []
        self._norm_cache: dict[int, str] = {}

        for entry in source_entries:
            norm_name = self._normalize(entry.name)
            self._add_exact(self._name_index, norm_name, entry)
            self._number_index[entry.number] = entry
            self._norm_cache[entry.number] = norm_name
            self._fuzzy_keys.append(norm_name)
            self._fuzzy_entries.append(entry)

            # Index original aliases + derived aliases together
            all_aliases = list(entry.aliases) + derived_aliases.get(entry.number, [])
            for alias in all_aliases:
                norm_alias = self._normalize(alias)
                self._add_exact(self._alias_index, norm_alias, entry)
                self._fuzzy_keys.append(norm_alias)
                self._fuzzy_entries.append(entry)

    @staticmethod
    def _add_exact(index: dict[str, SourceEntry], key: str, entry: SourceEntry) -> None:
        """Add to an exact-match index. On collision, keep lower number."""
        if key in index and index[key].number != entry.number:
            existing = index[key]
            winner = existing if existing.number < entry.number else entry
            logger.debug(
                "Duplicate normalized key %r - source channels %d and %d; keeping %d",
                key, existing.number, entry.number, winner.number,
            )
            index[key] = winner
            return
        index[key] = entry

    def _normalize(self, title: str) -> str:
        """Normalize a channel title for comparison."""
        s = title.upper()
        s = self._unicode_strip_re.sub("", s)
        s = s.replace(".", "").replace("!", "")
        s = self._noise_re.sub(" ", s)
        s = " ".join(s.split())
        for pattern, replacement in self._canonical_subs:
            s = pattern.sub(replacement, s)
        s = s.replace("/", " ")
        for pattern, replacement in self._region_abbrevs:
            s = pattern.sub(replacement, s)
        return " ".join(s.split())

    def _guide_decision(self, entry: SourceEntry, tier: int, decision: str, matched_on: str) -> MatchResult:
        """Wrap a matched entry, downgrading to needs_review if not in guide."""
        if entry.number not in self._valid_numbers:
            decision = "needs_review"
        return MatchResult(entry, tier=tier, score=100,
                           decision=decision, matched_on=matched_on)

    def _fuzzy_match(self, norm_title: str) -> list[tuple[str, float, int, float]]:
        """Run tier 4 fuzzy matching across multiple scorers.

        Returns a list of (key, discovery_score, index, sort_score) tuples,
        sorted by sort_score descending. Empty list if no matches.
        """
        query_tokens = len(norm_title.split())
        query_len = len(norm_title)

        # Primary scorer
        hits = process.extract(
            norm_title, self._fuzzy_keys,
            scorer=fuzz.token_sort_ratio,
            score_cutoff=self._fuzzy_threshold,
        )

        # Build hit map with sort_score cache
        hit_map: dict[int, tuple[str, float]] = {}
        sort_cache: dict[int, float] = {}
        for key, score, idx in (hits or []):
            hit_map[idx] = (key, score)
            sort_cache[idx] = score

        def sort_score(idx: int) -> float:
            if idx not in sort_cache:
                sort_cache[idx] = fuzz.token_sort_ratio(norm_title, self._fuzzy_keys[idx])
            return sort_cache[idx]

        # token_set_ratio: subset matches (guarded by token count + sort floor)
        if query_tokens >= 2:
            set_hits = process.extract(
                norm_title, self._fuzzy_keys,
                scorer=fuzz.token_set_ratio,
                score_cutoff=self._fuzzy_threshold,
            )
            for key, score, idx in (set_hits or []):
                cand_tokens = len(self._fuzzy_keys[idx].split())
                if cand_tokens >= 2:
                    if query_tokens <= cand_tokens or sort_score(idx) >= self._sort_floor:
                        if idx not in hit_map or score > hit_map[idx][1]:
                            hit_map[idx] = (key, score)

        # ratio: character-level similarity (guarded by length ratio + sort floor)
        ratio_hits = process.extract(
            norm_title, self._fuzzy_keys,
            scorer=fuzz.ratio,
            score_cutoff=self._fuzzy_threshold,
        )
        for key, score, idx in (ratio_hits or []):
            cand_len = len(self._fuzzy_keys[idx])
            len_ratio = min(query_len, cand_len) / max(query_len, cand_len) if max(query_len, cand_len) > 0 else 0
            if len_ratio >= 0.7 and (score >= 90 or sort_score(idx) >= self._sort_floor):
                if idx not in hit_map or score > hit_map[idx][1]:
                    hit_map[idx] = (key, score)

        # partial_ratio: substring containment (guarded by length + ratio + sort floor)
        partial_hits = process.extract(
            norm_title, self._fuzzy_keys,
            scorer=fuzz.partial_ratio,
            score_cutoff=self._fuzzy_threshold,
        )
        for key, score, idx in (partial_hits or []):
            cand_len = len(self._fuzzy_keys[idx])
            min_len = min(query_len, cand_len)
            len_ratio = min_len / max(query_len, cand_len) if max(query_len, cand_len) > 0 else 0
            if len_ratio >= 0.65 and min_len >= 5 and fuzz.ratio(norm_title, self._fuzzy_keys[idx]) >= 80:
                if sort_score(idx) >= self._sort_floor:
                    if idx not in hit_map or score > hit_map[idx][1]:
                        hit_map[idx] = (key, score)

        if not hit_map:
            return []

        # Build result with sort_scores for ranking (uses cache - no redundant calls)
        result = [
            (key, score, idx, sort_score(idx))
            for idx, (key, score) in hit_map.items()
        ]
        result.sort(key=lambda x: (-x[3], -x[1]))
        return result

    def _validate_decision(self, decision: str, norm_title: str, best_entry: SourceEntry) -> str:
        """Post-match validation checks. May downgrade auto_apply to needs_review."""
        if decision != "auto_apply":
            return decision

        # Conflicting token check
        best_norm = self._norm_cache.get(best_entry.number, self._normalize(best_entry.name))
        q_tokens = set(norm_title.split())
        b_tokens = set(best_norm.split())
        shared = q_tokens & b_tokens
        q_only = q_tokens - shared
        b_only = b_tokens - shared
        if q_only and b_only:
            has_related = False
            for qt in q_only:
                for bt in b_only:
                    if min(len(qt), len(bt)) <= 2:
                        if qt == bt:
                            has_related = True
                    elif fuzz.ratio(qt, bt) >= 60:
                        has_related = True
            if not has_related:
                return "needs_review"

        # +1 mismatch check
        query_plus1 = "+1" in norm_title
        source_plus1 = "+1" in best_norm
        if query_plus1 != source_plus1:
            return "needs_review"

        # Guide validation
        if best_entry.number not in self._valid_numbers:
            return "needs_review"

        return decision

    def match(self, channel: dict) -> MatchResult:
        """Match an IPTV channel against source entries."""
        stream_id = channel.get("stream_id", "") or ""
        channel_number = channel.get("channel_number")
        try:
            channel_number = int(channel_number) if channel_number is not None else None
        except (ValueError, TypeError):
            channel_number = None

        norm_title = self._normalize(channel.get("title", ""))

        if not norm_title:
            return MatchResult(None, tier=None, score=0,
                               decision="unmatched", matched_on=None)

        # ── Tier 1: fingerprint ──────────────────────────────────────────
        fp_number = self._stream_id_to_num.get(stream_id)
        if fp_number is not None and fp_number == channel_number and fp_number in self._number_index:
            entry = self._number_index[fp_number]
            return self._guide_decision(entry, tier=1, decision="fingerprint", matched_on=stream_id)

        # ── Tier 2: exact canonical name ─────────────────────────────────
        if norm_title in self._name_index:
            entry = self._name_index[norm_title]
            return self._guide_decision(entry, tier=2, decision="auto_apply", matched_on=norm_title)

        # ── Tier 3: exact alias ──────────────────────────────────────────
        if norm_title in self._alias_index:
            entry = self._alias_index[norm_title]
            return self._guide_decision(entry, tier=3, decision="auto_apply", matched_on=norm_title)

        # ── Tier 4: fuzzy ────────────────────────────────────────────────
        hits = self._fuzzy_match(norm_title)
        if not hits:
            return MatchResult(None, tier=None, score=0,
                               decision="unmatched", matched_on=None)

        # Dedup by source entry number
        seen: set[int] = set()
        deduped: list[tuple[SourceEntry, str, float, float]] = []
        for key, score, idx, sort_sc in hits:
            entry = self._fuzzy_entries[idx]
            if entry.number not in seen:
                seen.add(entry.number)
                deduped.append((entry, key, score, sort_sc))

        best_entry, best_key, best_score, best_sort = deduped[0]
        runner_sort = deduped[1][3] if len(deduped) > 1 else 0
        margin = best_sort - runner_sort

        if best_score >= self._min_auto_score and margin >= self._min_auto_margin:
            decision = "auto_apply"
        elif best_score >= self._min_auto_score and len(deduped) > 1:
            # Tight margin - check if runner-up is a sibling channel
            runner_entry = deduped[1][0]
            best_norm = self._norm_cache.get(best_entry.number) or self._normalize(best_entry.name)
            runner_norm = self._norm_cache.get(runner_entry.number) or self._normalize(runner_entry.name)
            best_words = best_norm.split()
            runner_words = runner_norm.split()
            shared_prefix = 0
            for a, b in zip(best_words, runner_words):
                if a == b:
                    shared_prefix += 1
                else:
                    break
            # Sibling channel family check: the best and runner-up share
            # most of their name (e.g. "TNT SPORTS 1" vs "TNT SPORTS 2").
            # Only relax margin when the query's own distinguishing token
            # matches the best entry's (so "SPORT 1" → "SPORTS 1" is OK,
            # but "SPORT 5" → "SPORTS 1" is not - 5 doesn't exist).
            query_words = set(norm_title.split())
            best_tail = set(best_words[shared_prefix:])
            if (shared_prefix >= len(best_words) - 1
                    and shared_prefix >= 1
                    and best_tail
                    and best_tail & query_words):
                decision = "auto_apply"
            else:
                decision = "needs_review"
        else:
            decision = "needs_review"

        decision = self._validate_decision(decision, norm_title, best_entry)

        candidates = [(entry, int(score)) for entry, _, score, _ in deduped]

        return MatchResult(best_entry, tier=4, score=int(best_score),
                           decision=decision, matched_on=best_key,
                           candidates=candidates)
