from django.contrib import admin

from .models import Order

# Register your models here.


@admin.register(Order)
class OrderKeyAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "order_id", "symbol", "direction", "status"]
