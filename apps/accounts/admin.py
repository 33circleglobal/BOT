from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User, UserKey


# Register your models here.
@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    pass


@admin.register(UserKey)
class UserKeyAdmin(admin.ModelAdmin):
    list_display = ["user", "is_active"]
