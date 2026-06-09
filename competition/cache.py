"""
Caching for the public competition endpoints.

Why
---
Under ad-driven / share-driven traffic the gallery (`/entries/`) and winners
(`/winners/`) endpoints are read very heavily. The entries query annotates a
vote Count over every entry and serialises up to 500 rows — that's the slow
path. We cache the *shared* part of each response (the entry list + vote
counts) and overlay the per-user `has_voted` flag in the view, which is a
single cheap indexed lookup.

Freshness model
---------------
- Votes (EntryVote writes) DO NOT invalidate the cache — they're frequent.
  Vote counts are allowed to lag by up to ENTRIES_TTL seconds, which is fine
  for a gallery (the voter sees their own action immediately via the optimistic
  UI; everyone else catches up within the TTL).
- CompetitionEntry writes (admin assigning a winner, disqualifying, editing
  images) bump a version stamp, which instantly orphans the old cache keys so
  those changes show up right away.
"""

from django.core.cache import cache

# TTLs (seconds)
ENTRIES_TTL = 30
WINNERS_TTL = 120
STATUS_TTL = 15

_VERSION_KEY = 'competition:cache:version'
STATUS_KEY = 'competition:status'


def cache_version():
    """Monotonic stamp baked into entries/winners keys. Bumping it orphans them."""
    v = cache.get(_VERSION_KEY)
    if v is None:
        cache.set(_VERSION_KEY, 1, None)  # no expiry
        return 1
    return v


def bump_cache_version():
    """Call when a CompetitionEntry changes (winner assigned, disqualified, …)."""
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        # Key missing/expired — (re)initialise it.
        cache.set(_VERSION_KEY, 1, None)


def entries_key(sort, limit):
    return f'competition:entries:{cache_version()}:{sort}:{limit}'


def winners_key():
    return f'competition:winners:{cache_version()}'
