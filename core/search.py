"""Search filtering for the public product catalogue."""
import operator
from functools import reduce

from django.db import models
from rest_framework.filters import SearchFilter


def term_variants(term):
    """
    Spellings a shopper might have typed for the same word.

    Matching is substring-based, so a singular term already finds plural text
    ("light" is inside "Lights"). The reverse is not true — which is why
    "led lights" found nothing against a product named "LED Light" — and
    -y/-ies words break in both directions ("berry" is not inside "Berries").
    So we generate both, cheaply, with no stemming library and nothing
    Postgres-specific: dev runs on SQLite.

    Order is preserved and duplicates dropped, so the original term stays first.
    """
    t = term.lower()
    out = [t]

    # Plural → singular.
    if len(t) >= 4:
        if t.endswith('ies'):
            out.append(t[:-3] + 'y')
        elif t.endswith('ves'):
            out.append(t[:-3] + 'f')       # leaves → leaf
            out.append(t[:-3] + 'fe')      # knives → knife
        elif t.endswith(('ches', 'shes', 'sses', 'xes', 'zes')):
            out.append(t[:-2])
        elif t.endswith('es'):
            out.append(t[:-2])
            out.append(t[:-1])             # rose ← roses, and box ← boxes
        elif t.endswith('s') and not t.endswith('ss'):
            out.append(t[:-1])

    # Singular → plural. Skipped for anything already ending in -s so we don't
    # invent "mosss" from "moss".
    if len(t) >= 3 and not t.endswith('s'):
        if t.endswith('y') and t[-2] not in 'aeiou':
            out.append(t[:-1] + 'ies')     # berry → berries
        elif t.endswith(('ch', 'sh', 'x', 'z')):
            out.append(t + 'es')
        else:
            out.append(t + 's')

    return list(dict.fromkeys(out))


class ForgivingSearchFilter(SearchFilter):
    """
    DRF's SearchFilter ANDs every whitespace-separated term and matches each one
    literally, so a single mistyped plural drops the whole result set to zero.
    This widens each term to its singular/plural variants and keeps a product if
    ANY variant hits. Terms are still ANDed, so precision across words is intact
    — only the exactness of each individual word is relaxed.
    """

    def filter_queryset(self, request, queryset, view):
        search_fields = self.get_search_fields(view, request)
        search_terms = self.get_search_terms(request)

        if not search_fields or not search_terms:
            return queryset

        orm_lookups = [
            self.construct_search(str(search_field), queryset)
            for search_field in search_fields
        ]

        base = queryset
        conditions = (
            reduce(
                operator.or_,
                (
                    models.Q(**{orm_lookup: variant})
                    for orm_lookup in orm_lookups
                    for variant in term_variants(term)
                ),
            )
            for term in search_terms
        )
        queryset = queryset.filter(reduce(operator.and_, conditions))

        # Same de-duplication DRF does for m2m search fields (tags__name here).
        if self.must_call_distinct(queryset, search_fields):
            queryset = queryset.filter(pk=models.OuterRef('pk'))
            queryset = base.filter(models.Exists(queryset))
        return queryset
