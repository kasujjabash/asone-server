"""Authentication and user administration routes, mounted at /api/auth/."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "accounts"

# The router generates list/detail routes and the @action endpoints for us —
# adding a viewset here is all it takes to expose a new resource.
router = DefaultRouter()
router.register("users", views.UserViewSet, basename="user")
router.register("login-attempts", views.LoginAttemptViewSet, basename="login-attempt")

urlpatterns = [
    # Tokens
    path("login/", views.LoginView.as_view(), name="login"),
    path("refresh/", views.RefreshView.as_view(), name="refresh"),
    path("verify/", views.VerifyView.as_view(), name="verify"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    # The signed-in user
    path("me/", views.MeView.as_view(), name="me"),
    path("password/change/", views.PasswordChangeView.as_view(), name="password-change"),
    # Reference data for building screens
    path("roles/", views.RoleListView.as_view(), name="roles"),
    # Administering other people (Program Lead and Operations Manager only)
    path("", include(router.urls)),
]
