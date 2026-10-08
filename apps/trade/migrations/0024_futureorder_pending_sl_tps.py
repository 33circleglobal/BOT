from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trade", "0023_tradesettings_last_market_risk_update_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="futureorder",
            name="pending_sl",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="futureorder",
            name="pending_tps",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
