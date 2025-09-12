from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trade", "0008_alter_futureorder_tp_order_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="spotorder",
            name="stop_loss_order_id",
            field=models.CharField(default="", blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="spotorder",
            name="stop_loss_price",
            field=models.DecimalField(default=0, max_digits=20, decimal_places=10),
        ),
        migrations.AddField(
            model_name="spotorder",
            name="stop_loss_status",
            field=models.CharField(
                default="POSITION",
                max_length=20,
                choices=[
                    ("OPEN", "Open"),
                    ("POSITION", "Position"),
                    ("CLOSED", "Closed"),
                    ("CANCELLED", "Cancelled"),
                    ("FAILED", "Failed"),
                ],
            ),
        ),
    ]

