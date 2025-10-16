from rest_framework.permissions import BasePermission

class AllowAny(BasePermission):
    def has_permission(self, request, view): return True

class IsAuthenticated(BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and getattr(request.user, "is_authenticated", False))

class RoleRequired(BasePermission):
    allowed = set()

    @classmethod
    def any_of(cls, *roles):
        class _P(cls):
            allowed = set(roles)
        return _P

    def has_permission(self, request, view):
        if not request.user or not getattr(request.user, "is_authenticated", False):
            return False
        return getattr(request.user, "role", None) in self.allowed
