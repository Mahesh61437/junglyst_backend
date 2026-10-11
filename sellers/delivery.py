"""
Where a seller's products can be delivered.

Store-wide settings (SellerProfile.ships_to_* + excluded states) apply to every
product; SellerCategoryRegion rules can only narrow them for a category. Shared
by the shop feed, the cart's shipping-configs endpoint and checkout so all three
agree.
"""
from django.db.models import Exists, OuterRef

from .models import REGION_SHIPS_FIELDS, SellerCategoryRegion


def category_rule_map(seller_user_ids) -> dict:
    """{(seller_user_id, category_id): [regions]} for the given sellers, in one query."""
    rules = SellerCategoryRegion.objects.filter(seller__user_id__in=seller_user_ids).select_related('seller')
    return {(str(rule.seller.user_id), rule.category_id): rule.regions for rule in rules}


def product_regions(store_regions, seller_user_id, category_ids, rule_map) -> list:
    """Regions a product can go to: the store's regions narrowed by every rule on its categories."""
    regions = list(store_regions)
    for category_id in category_ids:
        allowed = rule_map.get((str(seller_user_id), category_id))
        if allowed is not None:
            regions = [region for region in regions if region in allowed]
    return regions


def blocking_category(seller_user_id, categories, region, rule_map):
    """The first of `categories` whose rule keeps it out of `region`, or None."""
    for category in categories:
        allowed = rule_map.get((str(seller_user_id), category.id))
        if allowed is not None and region not in allowed:
            return category
    return None


def exclude_category_blocked(product_queryset, region):
    """Drop products whose seller restricted one of their categories away from `region`."""
    ships_field = REGION_SHIPS_FIELDS.get(region)
    if not ships_field:
        return product_queryset
    blocking_rule = SellerCategoryRegion.objects.filter(
        seller__user_id=OuterRef('seller_id'),
        category__products=OuterRef('pk'),
        **{ships_field: False},
    )
    return product_queryset.exclude(Exists(blocking_rule))
