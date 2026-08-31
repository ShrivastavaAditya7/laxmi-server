from rest_framework.permissions import BasePermission
from .models import Role


def _role(request):
    profile = getattr(request.user, "staff_profile", None)
    return profile.role if profile else None


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return _role(request) == Role.ADMIN


class IsBillingOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return _role(request) in (Role.BILLING, Role.ADMIN)


class IsWarehouseOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return _role(request) in (Role.WAREHOUSE, Role.ADMIN)


class IsPackagingOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return _role(request) in (Role.PACKAGING, Role.ADMIN)


class IsAnyStaff(BasePermission):
    def has_permission(self, request, view):
        return _role(request) is not None
