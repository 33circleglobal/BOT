from django.core.management.base import BaseCommand
from django.utils import timezone
from decimal import Decimal
import random
import uuid

from apps.accounts.models import User
from apps.trade.models import SpotOrder, FutureOrder, FutureTakeProfit


SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "TON/USDT",
]


class Command(BaseCommand):
    help = "Seed demo Spot and Futures orders for a superuser"

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="Username to seed for (defaults to first superuser)",
        )
        parser.add_argument(
            "--count", type=int, default=10, help="How many of each to create"
        )

    def handle(self, *args, **options):
        username = options.get("username")
        count = int(options.get("count") or 10)

        user = None
        if username:
            try:
                user = User.objects.get(username=username)
            except User.DoesNotExist:
                self.stderr.write(self.style.ERROR(f"User '{username}' not found."))
                return
        else:
            user = (
                User.objects.filter(is_superuser=True).first() or User.objects.first()
            )

        if not user:
            self.stderr.write(
                self.style.ERROR("No users found. Please create a user first.")
            )
            return

        self.stdout.write(
            self.style.NOTICE(f"Seeding demo data for user: {user.username}")
        )

        now = timezone.now()

        # Create Spot orders
        for i in range(count):
            symbol = random.choice(SYMBOLS)
            direction = random.choice(
                [SpotOrder.TradeDirection.LONG, SpotOrder.TradeDirection.SHORT]
            )
            qty = Decimal(random.uniform(0.01, 1.5)).quantize(Decimal("0.0001"))
            entry = Decimal(random.uniform(10, 70000)).quantize(Decimal("0.0001"))
            exitp = entry * Decimal(random.uniform(0.95, 1.05))
            pnl = (
                (exitp - entry)
                * qty
                * (
                    Decimal(1)
                    if direction == SpotOrder.TradeDirection.LONG
                    else Decimal(-1)
                )
            )
            pnl_pct = ((exitp - entry) / entry) * Decimal(100)
            status = random.choice(
                [
                    SpotOrder.TradeStatus.CLOSED,
                ]
            )
            created_at = now - timezone.timedelta(
                days=random.randint(0, 20), hours=random.randint(0, 23)
            )
            closed_at = (
                created_at + timezone.timedelta(hours=random.randint(1, 72))
                if status == SpotOrder.TradeStatus.CLOSED
                else None
            )

            SpotOrder.objects.create(
                order_id=str(uuid.uuid4()),
                symbol=symbol,
                direction=direction,
                order_quantity=qty,
                final_quantity=qty,
                entry_price=entry,
                exit_price=(
                    exitp if status == SpotOrder.TradeStatus.CLOSED else Decimal("0")
                ),
                total_cost=(qty * entry),
                entry_fee=Decimal("0.5"),
                exit_fee=Decimal("0.5"),
                total_fee=Decimal("1.0"),
                pnl=pnl if status == SpotOrder.TradeStatus.CLOSED else Decimal("0"),
                pnl_percentage=(
                    pnl_pct if status == SpotOrder.TradeStatus.CLOSED else Decimal("0")
                ),
                status=status,
                is_spot=True,
                leverage=1,
                user=user,
                created_at=created_at,
                updated_at=created_at,
                closed_at=closed_at,
            )

        # Create Futures orders
        for i in range(count):
            symbol = random.choice(SYMBOLS)
            direction = random.choice(
                [FutureOrder.TradeDirection.LONG, FutureOrder.TradeDirection.SHORT]
            )
            qty = Decimal(random.uniform(0.001, 0.5)).quantize(Decimal("0.0001"))
            entry = Decimal(random.uniform(10, 70000)).quantize(Decimal("0.0001"))
            lev = random.choice([2, 3, 5, 10, 20])
            # For demo, compute a fake pnl and statuses
            status = random.choice(
                [
                    FutureOrder.TradeStatus.CLOSED,
                    FutureOrder.TradeStatus.POSITION,
                    FutureOrder.TradeStatus.OPEN,
                ]
            )
            # Futures pnl demo: +/- up to 4% notional / leverage effect implicit
            notional = qty * entry
            pnl = (notional * Decimal(random.uniform(-0.04, 0.04))).quantize(
                Decimal("0.0001")
            )
            pnl_pct = Decimal(random.uniform(-8, 8)).quantize(Decimal("0.01"))
            created_at = now - timezone.timedelta(
                days=random.randint(0, 20), hours=random.randint(0, 23)
            )
            closed_at = (
                created_at + timezone.timedelta(hours=random.randint(1, 72))
                if status == FutureOrder.TradeStatus.CLOSED
                else None
            )

            f = FutureOrder.objects.create(
                order_id=str(uuid.uuid4()),
                symbol=symbol,
                direction=direction,
                status=status,
                leverage=lev,
                order_quantity=qty,
                entry_price=entry,
                entry_fee=Decimal("0.8"),
                entry_fee_currency="USDT",
                stop_loss_order_id=str(uuid.uuid4()),
                stop_loss_price=(entry * Decimal("0.97")).quantize(Decimal("0.0001")),
                stop_loss_fee=Decimal("0.2"),
                stop_loss_status=random.choice(
                    [
                        FutureOrder.TradeStatus.OPEN,
                        FutureOrder.TradeStatus.CANCELLED,
                        FutureOrder.TradeStatus.CLOSED,
                    ]
                ),
                total_fee=Decimal("1.2"),
                pnl=pnl if status == FutureOrder.TradeStatus.CLOSED else Decimal("0"),
                pnl_percentage=(
                    pnl_pct
                    if status == FutureOrder.TradeStatus.CLOSED
                    else Decimal("0")
                ),
                user=user,
                created_at=created_at,
                updated_at=created_at,
                closed_at=closed_at,
            )
            # Add some child TPs for demo
            for _ in range(random.randint(0, 3)):
                pct = Decimal(random.choice([10, 20, 25, 50]))
                tp_price = (entry * Decimal("1.02")).quantize(Decimal("0.0001")) if direction == FutureOrder.TradeDirection.LONG else (entry * Decimal("0.98")).quantize(Decimal("0.0001"))
                FutureTakeProfit.objects.create(
                    order=f,
                    tp_order_id=str(uuid.uuid4()),
                    price=tp_price,
                    percent=pct,
                    quantity=(qty * pct / Decimal(100)).quantize(Decimal("0.0001")),
                    status=random.choice([
                        FutureTakeProfit.TradeStatus.POSITION,
                        FutureTakeProfit.TradeStatus.CLOSED,
                        FutureTakeProfit.TradeStatus.CANCELLED,
                    ]),
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {count} Spot and {count} Futures demo orders for {user.username}."
            )
        )
