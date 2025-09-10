from django.urls import path
from .views import register_view, login_view, logout_view, home, stats_view, history_view

app_name = "accounts"

urlpatterns = [
    path("", home, name="home"),
    path("register/", register_view, name="register"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("stats/", stats_view, name="stats"),
    path("history/", history_view, name="history"),
]
