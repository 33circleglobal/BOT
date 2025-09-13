from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("trade", "0009_spotorder_stop_loss_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="FutureTakeProfit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tp_order_id", models.CharField(blank=True, default="", max_length=100)),
                ("price", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("percent", models.DecimalField(decimal_places=3, default=0, max_digits=7)),
                ("quantity", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("POSITION", "Position"), ("CLOSED", "Closed"), ("CANCELLED", "Cancelled"), ("FAILED", "Failed")], default="POSITION", max_length=20)),
                ("fee", models.DecimalField(decimal_places=10, default=0, max_digits=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tps", to="trade.futureorder")),
            ],
            options={
                "db_table": "future_take_profits",
            },
        ),
        migrations.AddIndex(
            model_name="futuretakeprofit",
            index=models.Index(fields=["order", "status"], name="trade_fut_tp_status_idx"),
        ),
    ]

