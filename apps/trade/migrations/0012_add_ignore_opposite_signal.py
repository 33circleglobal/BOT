from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("trade", "0011_drop_futureorder_parent_tp_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="spotorder",
            name="ignore_opposite_signal",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="futureorder",
            name="ignore_opposite_signal",
            field=models.BooleanField(default=False),
        ),
    ]

