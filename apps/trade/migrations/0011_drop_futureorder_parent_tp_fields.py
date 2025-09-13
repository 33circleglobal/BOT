from django.db import migrations


def forwards(apps, schema_editor):
    FutureOrder = apps.get_model('trade', 'FutureOrder')
    FutureTakeProfit = apps.get_model('trade', 'FutureTakeProfit')
    # Migrate any legacy single TP into a child entry before dropping fields
    for o in FutureOrder.objects.all():
        tp_id = getattr(o, 'tp_order_id', '') if hasattr(o, 'tp_order_id') else ''
        tp_price = getattr(o, 'tp_price', 0) if hasattr(o, 'tp_price') else 0
        tp_status = getattr(o, 'tp_status', 'CANCELLED') if hasattr(o, 'tp_status') else 'CANCELLED'
        if (tp_id or tp_price) and not o.tps.exists():
            try:
                FutureTakeProfit.objects.create(
                    order=o,
                    tp_order_id=tp_id or '',
                    price=tp_price or 0,
                    percent=100.0,
                    quantity=o.order_quantity,
                    status=tp_status,
                )
            except Exception:
                pass


class Migration(migrations.Migration):

    dependencies = [
        ("trade", "0010_futuretakeprofit"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='futureorder', name='tp_order_id'),
        migrations.RemoveField(model_name='futureorder', name='tp_price'),
        migrations.RemoveField(model_name='futureorder', name='tp_fee'),
        migrations.RemoveField(model_name='futureorder', name='tp_status'),
    ]

