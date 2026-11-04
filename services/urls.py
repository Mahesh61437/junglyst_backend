from django.urls import path

from . import views

urlpatterns = [
    # Public catalogue + quote request
    path('groups/', views.ServiceGroupListView.as_view(), name='service_group_list'),
    path('uploads/', views.ReferenceUploadView.as_view(), name='service_reference_upload'),
    path('groups/<slug:slug>/', views.ServiceGroupDetailView.as_view(), name='service_group_detail'),
    path('offerings/<slug:slug>/', views.ServiceOfferingDetailView.as_view(), name='service_offering_detail'),
    path('offerings/<slug:slug>/request/', views.QuoteRequestCreateView.as_view(), name='service_quote_create'),

    # Customer
    path('my-requests/', views.MyQuoteListView.as_view(), name='service_my_requests'),
    path('my-requests/<str:reference>/', views.MyQuoteDetailView.as_view(), name='service_my_request_detail'),
    path('my-requests/<str:reference>/pay/', views.QuotePayView.as_view(), name='service_quote_pay'),
    path('my-requests/<str:reference>/cancel/', views.MyQuoteCancelView.as_view(), name='service_my_request_cancel'),
    path('my-requests/<str:reference>/charges/<uuid:charge_id>/pay/', views.QuoteChargePayView.as_view(), name='service_charge_pay'),

    # Assigned seller
    path('seller/requests/', views.SellerQuoteListView.as_view(), name='service_seller_requests'),
    path('seller/requests/<uuid:pk>/', views.SellerQuoteDetailView.as_view(), name='service_seller_request_detail'),
    path('seller/requests/<uuid:pk>/charges/', views.SellerChargeCreateView.as_view(), name='service_seller_charge_create'),
    path('seller/requests/<uuid:pk>/charges/<uuid:charge_id>/cancel/', views.SellerChargeCancelView.as_view(), name='service_seller_charge_cancel'),

    # Super admin
    path('admin/groups/', views.AdminGroupListCreateView.as_view(), name='service_admin_groups'),
    path('admin/groups/<int:pk>/', views.AdminGroupDetailView.as_view(), name='service_admin_group_detail'),
    path('admin/offerings/', views.AdminOfferingListCreateView.as_view(), name='service_admin_offerings'),
    path('admin/offerings/<int:pk>/', views.AdminOfferingDetailView.as_view(), name='service_admin_offering_detail'),
    path('admin/sellers/', views.AdminSellerListView.as_view(), name='service_admin_sellers'),
    path('admin/summary/', views.AdminSummaryView.as_view(), name='service_admin_summary'),
    path('admin/requests/', views.AdminQuoteListCreateView.as_view(), name='service_admin_requests'),
    path('admin/requests/<uuid:pk>/', views.AdminQuoteDetailView.as_view(), name='service_admin_request_detail'),
    path('admin/requests/<uuid:pk>/assign/', views.AdminAssignSellerView.as_view(), name='service_admin_assign'),
    path('admin/requests/<uuid:pk>/send-pay-request/', views.AdminSendPayRequestView.as_view(), name='service_admin_send_pay_request'),
    path('admin/requests/<uuid:pk>/decline/', views.AdminDeclineView.as_view(), name='service_admin_decline'),
    path('admin/requests/<uuid:pk>/renew/', views.AdminRenewView.as_view(), name='service_admin_renew'),
    path('admin/requests/<uuid:pk>/cancel/', views.AdminCancelView.as_view(), name='service_admin_cancel'),
    path('admin/requests/<uuid:pk>/dismiss-cancel/', views.AdminDismissCancelView.as_view(), name='service_admin_dismiss_cancel'),
    path('admin/requests/<uuid:pk>/charges/<uuid:charge_id>/payout-done/', views.AdminExtraPayoutView.as_view(), name='service_admin_extra_payout'),
    path('admin/requests/<uuid:pk>/mark-delivered/', views.AdminMarkDeliveredView.as_view(), name='service_admin_mark_delivered'),
    path('admin/requests/<uuid:pk>/charges/', views.AdminChargeCreateView.as_view(), name='service_admin_charge_create'),
    path('admin/requests/<uuid:pk>/charges/<uuid:charge_id>/cancel/', views.AdminChargeCancelView.as_view(), name='service_admin_charge_cancel'),
]
