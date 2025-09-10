from django.shortcuts import render, redirect
from django.contrib.auth import login, logout, authenticate
from django.contrib import messages
from .forms import RegistrationForm, LoginForm
from django.contrib.auth.decorators import login_required
from django.db.models import Sum, Count, Q, F, DecimalField, ExpressionWrapper
from django.utils import timezone
from datetime import timedelta, datetime
from apps.trade.models import SpotOrder, FutureOrder
import json


@login_required
def home(request):
    user = request.user

    # Core aggregates
    spot_closed = SpotOrder.objects.filter(user=user, status=SpotOrder.TradeStatus.CLOSED)
    fut_closed = FutureOrder.objects.filter(user=user, status=FutureOrder.TradeStatus.CLOSED)

    spot_pnl = spot_closed.aggregate(total=Sum("pnl"))["total"] or 0
    fut_pnl = fut_closed.aggregate(total=Sum("pnl"))["total"] or 0

    spot_wins = spot_closed.filter(pnl__gt=0).count()
    spot_total = spot_closed.count()
    fut_wins = fut_closed.filter(pnl__gt=0).count()
    fut_total = fut_closed.count()
    spot_win_rate = round((spot_wins / spot_total) * 100, 2) if spot_total else 0
    fut_win_rate = round((fut_wins / fut_total) * 100, 2) if fut_total else 0

    # Volume approximations
    spot_volume = SpotOrder.objects.filter(user=user).aggregate(total=Sum("total_cost"))["total"] or 0
    fut_expr = ExpressionWrapper(F("order_quantity") * F("entry_price"), output_field=DecimalField(max_digits=20, decimal_places=10))
    fut_volume = FutureOrder.objects.filter(user=user).annotate(val=fut_expr).aggregate(total=Sum("val"))["total"] or 0

    # Open positions count
    spot_open = SpotOrder.objects.filter(user=user, status=SpotOrder.TradeStatus.POSITION).count()
    fut_open = FutureOrder.objects.filter(user=user, status=FutureOrder.TradeStatus.POSITION).count()

    # PnL over last 30 days (by closed_at date)
    since = timezone.now() - timedelta(days=30)
    spot_daily = (
        spot_closed.filter(closed_at__gte=since)
        .values("closed_at__date")
        .annotate(total=Sum("pnl"))
        .order_by("closed_at__date")
    )
    fut_daily = (
        fut_closed.filter(closed_at__gte=since)
        .values("closed_at__date")
        .annotate(total=Sum("pnl"))
        .order_by("closed_at__date")
    )

    # Normalize dates and combine series
    date_set = sorted({*(d["closed_at__date"] for d in spot_daily), *(d["closed_at__date"] for d in fut_daily)})
    labels = [d.strftime("%Y-%m-%d") for d in date_set]
    spot_map = {d["closed_at__date"].strftime("%Y-%m-%d"): float(d["total"]) for d in spot_daily}
    fut_map = {d["closed_at__date"].strftime("%Y-%m-%d"): float(d["total"]) for d in fut_daily}
    spot_series = [spot_map.get(day, 0) for day in labels]
    fut_series = [fut_map.get(day, 0) for day in labels]

    # Trades by symbol (top 8)
    spot_by_symbol = (
        SpotOrder.objects.filter(user=user)
        .values("symbol")
        .annotate(ct=Count("id"))
        .order_by("-ct")[:8]
    )
    fut_by_symbol = (
        FutureOrder.objects.filter(user=user)
        .values("symbol")
        .annotate(ct=Count("id"))
        .order_by("-ct")[:8]
    )

    context = {
        "spot_pnl": float(spot_pnl),
        "fut_pnl": float(fut_pnl),
        "spot_win_rate": spot_win_rate,
        "fut_win_rate": fut_win_rate,
        "spot_volume": float(spot_volume),
        "fut_volume": float(fut_volume),
        "spot_open": spot_open,
        "fut_open": fut_open,
        "labels_json": json.dumps(labels),
        "spot_series_json": json.dumps(spot_series),
        "fut_series_json": json.dumps(fut_series),
        "spot_by_symbol": list(spot_by_symbol),
        "fut_by_symbol": list(fut_by_symbol),
    }
    return render(request, "dashboard.html", context)


def register_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Registration successful. Please log in.")
        return redirect("login")
    return render(request, "accounts/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("accounts:home")

    form = LoginForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            username = form.cleaned_data["username"]
            password = form.cleaned_data["password"]
            user = authenticate(request, username=username, password=password)

            if user is not None and not user.is_superuser:
                messages.error(request, "Your are not allowed to access this")
                return render(request, "auth/login.html", {"form": form})

            if user is not None:
                login(request, user)
                messages.success(request, "Login successful.")
                return redirect("accounts:home")
            else:
                messages.error(request, "Invalid username or password.")
    return render(request, "accounts/login.html", {"form": form})


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def stats_view(request):
    user = request.user
    market = request.GET.get("market", "both")  # spot | futures | both
    symbol = request.GET.get("symbol") or None
    days = int(request.GET.get("days", 30))
    since = timezone.now() - timedelta(days=days)

    # Select base querysets
    spot_qs = SpotOrder.objects.filter(user=user, status=SpotOrder.TradeStatus.CLOSED, closed_at__gte=since)
    fut_qs = FutureOrder.objects.filter(user=user, status=FutureOrder.TradeStatus.CLOSED, closed_at__gte=since)
    if symbol:
        spot_qs = spot_qs.filter(symbol=symbol)
        fut_qs = fut_qs.filter(symbol=symbol)
    if market == "spot":
        fut_qs = FutureOrder.objects.none()
    elif market == "futures":
        spot_qs = SpotOrder.objects.none()

    # Daily pnl
    spot_daily = spot_qs.values("closed_at__date").annotate(total=Sum("pnl")).order_by("closed_at__date")
    fut_daily = fut_qs.values("closed_at__date").annotate(total=Sum("pnl")).order_by("closed_at__date")
    date_set = sorted({*(d["closed_at__date"] for d in spot_daily), *(d["closed_at__date"] for d in fut_daily)})
    labels = [d.strftime("%Y-%m-%d") for d in date_set]
    spot_map = {d["closed_at__date"].strftime("%Y-%m-%d"): float(d["total"]) for d in spot_daily}
    fut_map = {d["closed_at__date"].strftime("%Y-%m-%d"): float(d["total"]) for d in fut_daily}
    spot_series = [spot_map.get(day, 0) for day in labels]
    fut_series = [fut_map.get(day, 0) for day in labels]

    # Cumulative pnl
    cum_spot, cum_fut = [], []
    run = 0
    for v in spot_series:
        run += v
        cum_spot.append(run)
    run = 0
    for v in fut_series:
        run += v
        cum_fut.append(run)

    # Performance by symbol
    spot_by_symbol = spot_qs.values("symbol").annotate(pnl=Sum("pnl"), trades=Count("id")).order_by("-pnl")[:10]
    fut_by_symbol = fut_qs.values("symbol").annotate(pnl=Sum("pnl"), trades=Count("id")).order_by("-pnl")[:10]

    context = {
        "labels_json": json.dumps(labels),
        "spot_series_json": json.dumps(spot_series),
        "fut_series_json": json.dumps(fut_series),
        "cum_spot_json": json.dumps(cum_spot),
        "cum_fut_json": json.dumps(cum_fut),
        "spot_by_symbol": list(spot_by_symbol),
        "fut_by_symbol": list(fut_by_symbol),
        "market": market,
        "symbol": symbol or "",
        "days": days,
    }
    return render(request, "stats.html", context)


@login_required
def history_view(request):
    user = request.user
    market = request.GET.get("market", "both")
    status_val = request.GET.get("status", "")
    symbol = request.GET.get("symbol", "")
    date_from = request.GET.get("from", "")
    date_to = request.GET.get("to", "")

    spot_qs = SpotOrder.objects.filter(user=user)
    fut_qs = FutureOrder.objects.filter(user=user)

    if market == "spot":
        fut_qs = FutureOrder.objects.none()
    elif market == "futures":
        spot_qs = SpotOrder.objects.none()

    if status_val:
        spot_qs = spot_qs.filter(status=status_val)
        fut_qs = fut_qs.filter(status=status_val)
    if symbol:
        spot_qs = spot_qs.filter(symbol=symbol)
        fut_qs = fut_qs.filter(symbol=symbol)

    if date_from:
        try:
            df = datetime.fromisoformat(date_from)
            df = timezone.make_aware(df) if timezone.is_naive(df) else df
            spot_qs = spot_qs.filter(created_at__gte=df)
            fut_qs = fut_qs.filter(created_at__gte=df)
        except Exception:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            dt = timezone.make_aware(dt) if timezone.is_naive(dt) else dt
            spot_qs = spot_qs.filter(created_at__lte=dt)
            fut_qs = fut_qs.filter(created_at__lte=dt)
        except Exception:
            pass

    # Normalize to common dicts and sort
    records = []
    for o in spot_qs.select_related("user")[:2000]:
        records.append({
            "market": "Spot",
            "symbol": o.symbol,
            "direction": o.direction,
            "status": o.status,
            "pnl": float(o.pnl),
            "pnl_pct": float(o.pnl_percentage),
            "entry_price": float(o.entry_price),
            "exit_price": float(o.exit_price),
            "quantity": float(o.final_quantity or o.order_quantity),
            "created_at": o.created_at,
            "closed_at": o.closed_at,
        })
    for o in fut_qs.select_related("user")[:2000]:
        records.append({
            "market": "Futures",
            "symbol": o.symbol,
            "direction": o.direction,
            "status": o.status,
            "pnl": float(o.pnl),
            "pnl_pct": float(o.pnl_percentage),
            "entry_price": float(o.entry_price),
            "exit_price": None,
            "quantity": float(o.order_quantity),
            "created_at": o.created_at,
            "closed_at": o.closed_at,
        })

    records.sort(key=lambda r: r["created_at"], reverse=True)

    context = {
        "records": records[:500],
        "market": market,
        "status": status_val,
        "symbol": symbol,
        "date_from": date_from,
        "date_to": date_to,
    }
    return render(request, "history.html", context)
