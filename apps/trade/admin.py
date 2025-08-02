from django.contrib import admin

from .models import SpotOrder, FutureOrder

# Register your models here.


@admin.register(SpotOrder)
class SpotOrderAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "order_id", "symbol", "direction", "status"]


@admin.register(FutureOrder)
class FutureOrderAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "order_id", "symbol", "direction", "status"]
