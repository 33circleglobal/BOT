from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User, UserKey, UserHyperLiquidKey, IPAddress


# Register your models here.
@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    pass


@admin.register(UserKey)
class UserKeyAdmin(admin.ModelAdmin):
    list_display = ["user", "is_active"]


@admin.register(UserHyperLiquidKey)
class UserHyperLiquidKeyAdmin(admin.ModelAdmin):
    list_display = ["user", "is_active", "api_valid_days"]


@admin.register(IPAddress)
class IPAddressAdmin(admin.ModelAdmin):
    list_display = ["id", "ip_address", "is_active", "last_used", "usage_count"]
