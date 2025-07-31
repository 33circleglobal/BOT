from django.db import models
from django.contrib.auth.models import AbstractUser
from django.db.models import UniqueConstraint
from django.db.models.functions import Lower

from apps.accounts.utils.encryption import encrypt_value, decrypt_value


class User(AbstractUser):
    email = models.EmailField(unique=True)
    case_insensitive_username = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "users"
        constraints = [
            UniqueConstraint(
                Lower("case_insensitive_username"), name="unique_lower_name"
            )
        ]


class UserKey(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    _api_key = models.CharField(max_length=255, db_column="api_key")
    _api_secret = models.CharField(max_length=255, db_column="api_secret")
    is_active = models.BooleanField(default=True)

    @property
    def api_key(self):
        return decrypt_value(self._api_key)

    @api_key.setter
    def api_key(self, value):
        self._api_key = encrypt_value(value)

    @property
    def api_secret(self):
        return decrypt_value(self._api_secret)

    @api_secret.setter
    def api_secret(self, value):
        self._api_secret = encrypt_value(value)

    def save(self, *args, **kwargs):
        if self._api_key and not self._api_key.startswith("gAAAA"):
            self._api_key = encrypt_value(self._api_key)
        if self._api_secret and not self._api_secret.startswith("gAAAA"):
            self._api_secret = encrypt_value(self._api_secret)
        super().save(*args, **kwargs)

    class Meta:
        db_table = "user_keys"
