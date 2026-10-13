from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("admin/", admin.site.urls),
    # The standalone app is self-contained; mount it at the same path the ERP
    # used so all existing ``{% url 'mock_interview:...' %}`` tags keep working.
    path(
        "mock-interview/",
        include(("mock_interview.urls", "mock_interview"), namespace="mock_interview"),
    ),
    # Auth entry points. ``login_view`` is referenced by mock_interview code and
    # ``faculty_dashboard`` is referenced by views/hod.py (non-namespaced).
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="login.html"),
        name="login_view",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "faculty/",
        RedirectView.as_view(
            pattern_name="mock_interview:faculty_dashboard", permanent=False
        ),
        name="faculty_dashboard",
    ),
    path(
        "",
        RedirectView.as_view(pattern_name="mock_interview:dashboard", permanent=False),
        name="home",
    ),
]
