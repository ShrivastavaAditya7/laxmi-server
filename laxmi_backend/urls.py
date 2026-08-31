from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny
from django.contrib.auth import authenticate
from rest_framework.authtoken.models import Token

from .views import (
    ItemTypeViewSet, BrandViewSet, SKUViewSet, StockMovementView,
    CheckoutView, TodaysBillsView, DashboardSummaryView, DashboardTrendView,
    StaffManagementViewSet, BrandManagementViewSet, SKUManagementViewSet,
)


class StaffLoginView(APIView):
    """
    Rural-friendly login: staff picks their name (client already knows the small
    directory per store) and enters a 4-6 digit PIN. This resolves to a normal
    Django auth token underneath — the PIN UX is a thin layer over standard auth.
    POST { "username": "ramesh.yadav", "pin": "4821" }
    """
    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username")
        pin = request.data.get("pin")
        user = authenticate(request, username=username, password=pin)
        if not user:
            return Response({"detail": "Invalid name or PIN."}, status=401)
        token, _ = Token.objects.get_or_create(user=user)
        profile = getattr(user, "staff_profile", None)
        return Response({
            "token": token.key,
            "role": profile.role if profile else None,
            "full_name": profile.full_name if profile else user.get_full_name(),
        })


router = DefaultRouter()
router.register("item-types", ItemTypeViewSet, basename="item-type")
router.register("brands", BrandViewSet, basename="brand")
router.register("skus", SKUViewSet, basename="sku")
router.register("admin/staff", StaffManagementViewSet, basename="admin-staff")
router.register("admin/brands", BrandManagementViewSet, basename="admin-brand")
router.register("admin/skus", SKUManagementViewSet, basename="admin-sku")

urlpatterns = [
    path("auth/login/", StaffLoginView.as_view(), name="staff-login"),
    path("stock/movement/", StockMovementView.as_view(), name="stock-movement"),
    path("billing/checkout/", CheckoutView.as_view(), name="checkout"),
    path("billing/today/", TodaysBillsView.as_view(), name="todays-bills"),
    path("dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("dashboard/trend/", DashboardTrendView.as_view(), name="dashboard-trend"),
    path("", include(router.urls)),
]
