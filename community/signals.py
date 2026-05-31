"""
Auto-create a CommunityProfile for every new User.

Handle generation: derive a slug from the email local-part, suffix with
a numeric counter if taken.  Falls back to the user's UUID if nothing
slugifies (rare — e.g. fully non-ASCII emails).
"""
import re
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.text import slugify


_HANDLE_MAX_LEN = 30
_HANDLE_RETRY = 50  # max numeric suffix attempts before falling back to uuid


def _generate_unique_handle(user) -> str:
    from .models import CommunityProfile

    seed_source = (user.email or '').split('@')[0] if user.email else ''
    if not seed_source:
        seed_source = (user.username or '') if hasattr(user, 'username') else ''

    base = slugify(seed_source)[:_HANDLE_MAX_LEN - 4] or str(user.id).replace('-', '')[:_HANDLE_MAX_LEN - 4]

    handle = base
    for i in range(2, _HANDLE_RETRY + 1):
        if not CommunityProfile.all_objects.filter(handle=handle).exists():
            return handle
        handle = f"{base}{i}"[:_HANDLE_MAX_LEN]

    # Extreme fallback — embed the UUID
    return str(user.id).replace('-', '')[:_HANDLE_MAX_LEN]


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_community_profile(sender, instance, created, **kwargs):
    if not created:
        return
    from .models import CommunityProfile
    if CommunityProfile.all_objects.filter(user=instance).exists():
        return
    CommunityProfile.objects.create(
        user=instance,
        handle=_generate_unique_handle(instance),
    )
