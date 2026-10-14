from django.urls import path

from . import views

urlpatterns = [
    # Public + buyer
    path('', views.WorkshopListView.as_view(), name='workshop_list'),
    path('my-bookings/', views.MyBookingsView.as_view(), name='workshop_my_bookings'),
    path('bookings/by-order/<str:order_number>/', views.BookingByOrderView.as_view(), name='workshop_booking_by_order'),
    path('bookings/by-order/<str:order_number>/release/', views.ReleaseBookingHoldView.as_view(), name='workshop_booking_release'),

    # Seller (organiser)
    path('seller/bookings/', views.SellerBookingListView.as_view(), name='workshop_seller_bookings'),
    path('seller/summary/', views.SellerWorkshopSummaryView.as_view(), name='workshop_seller_summary'),

    # Super admin
    path('admin/workshops/', views.AdminWorkshopListCreateView.as_view(), name='workshop_admin_list'),
    path('admin/workshops/<uuid:pk>/', views.AdminWorkshopDetailView.as_view(), name='workshop_admin_detail'),
    path('admin/workshops/<uuid:pk>/financials/', views.AdminWorkshopFinancialsView.as_view(), name='workshop_admin_financials'),
    path('admin/organisers/', views.AdminOrganiserListView.as_view(), name='workshop_admin_organisers'),
    path('admin/bookings/', views.AdminBookingListView.as_view(), name='workshop_admin_bookings'),
    path('admin/bookings/<uuid:pk>/', views.AdminBookingDetailView.as_view(), name='workshop_admin_booking_detail'),
    path('admin/financials/', views.AdminFinancialsOverviewView.as_view(), name='workshop_admin_financials_overview'),
    path('admin/payouts/', views.AdminPayoutCreateView.as_view(), name='workshop_admin_payout_create'),
    path('admin/payouts/<int:pk>/', views.AdminPayoutDeleteView.as_view(), name='workshop_admin_payout_delete'),

    # Slug routes last so they never shadow the fixed paths above.
    path('<slug:slug>/', views.WorkshopDetailView.as_view(), name='workshop_detail'),
    path('<slug:slug>/book/', views.WorkshopBookView.as_view(), name='workshop_book'),
]
