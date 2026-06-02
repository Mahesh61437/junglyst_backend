from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import (
    SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView,
)
from core.views import robots_txt, sitemap_xml

urlpatterns = [
    path('robots.txt', robots_txt, name='robots_txt'),
    path('sitemap.xml', sitemap_xml, name='sitemap_xml'),
    path('admin/', admin.site.urls),
    path('api/core/', include('core.urls')),
    path('api/cart/', include('cart.urls')),
    path('api/orders/', include('orders.urls')),
    path('api/shipping/', include('shipping.urls')),
    path('api/sellers/', include('sellers.urls')),
    path('api/analytics/', include('analytics.urls')),
    path('api/notifications/', include('notifications.urls')),
    path('api/payments/', include('payments.urls')),
    path('api/competition/', include('competition.urls')),

    # API documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# Community is feature-flagged off until launch — see settings.FEATURE_COMMUNITY_ENABLED.
# When the flag is False, /api/community/* is not routed and returns 404 to clients.
if getattr(settings, 'FEATURE_COMMUNITY_ENABLED', False):
    urlpatterns.insert(-3, path('api/community/', include('community.urls')))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
