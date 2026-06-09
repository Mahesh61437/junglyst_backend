"""
Lightweight content moderation for community text.

Week 1 implementation: a database-backed BlockedWord list. Week 2 layers
the `better-profanity` package on top + image NSFW classification.

Public API:
    check_text(text)  -> ('ok' | 'warn' | 'block', matched_word | '')
    raise_if_blocked(text)  -> raises serializers.ValidationError on 'block'
"""
import re
from functools import lru_cache
from typing import Tuple

from django.core.cache import cache
from rest_framework import serializers


_CACHE_KEY = 'community:blocked_words:v1'
_CACHE_TTL = 300  # 5 minutes — refresh after admin edits


def _load_word_lists() -> Tuple[set, set]:
    """Return (block_words, warn_words) sets, cached in Redis."""
    cached = cache.get(_CACHE_KEY)
    if cached is not None:
        return cached

    # Lazy import to avoid AppConfig import-time issues.
    from .models import BlockedWord, BlockedWordSeverity

    rows = BlockedWord.objects.values_list('word', 'severity')
    block_set: set = set()
    warn_set: set = set()
    for word, severity in rows:
        normalized = word.lower().strip()
        if not normalized:
            continue
        if severity == BlockedWordSeverity.BLOCK:
            block_set.add(normalized)
        else:
            warn_set.add(normalized)

    payload = (block_set, warn_set)
    cache.set(_CACHE_KEY, payload, _CACHE_TTL)
    return payload


def invalidate_blocked_word_cache() -> None:
    cache.delete(_CACHE_KEY)


_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str):
    return _TOKEN_RE.findall(text.lower())


def check_text(text: str) -> Tuple[str, str]:
    """
    Returns ('ok' | 'warn' | 'block', matched_word).

    Matches on whole-word tokens — avoids false positives on substrings
    inside legitimate words (e.g. 'analysis' won't trigger 'anal').
    """
    if not text:
        return ('ok', '')

    block_set, warn_set = _load_word_lists()
    if not block_set and not warn_set:
        return ('ok', '')

    tokens = set(_tokenize(text))

    hit = tokens & block_set
    if hit:
        return ('block', next(iter(hit)))

    hit = tokens & warn_set
    if hit:
        return ('warn', next(iter(hit)))

    return ('ok', '')


def raise_if_blocked(text: str) -> str:
    """
    Validate text against the blocklist. Raises serializers.ValidationError
    if 'block' severity. Returns the verdict ('ok' or 'warn') so callers
    can persist the warn on the model if desired.
    """
    verdict, word = check_text(text)
    if verdict == 'block':
        raise serializers.ValidationError(
            f"Your content contains a blocked term: '{word}'. "
            f"Please revise and try again."
        )
    return verdict
