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

    @property
    def encrypted_api_key(self):
        return self._api_key

    @property
    def encrypted_api_secret(self):
        return self._api_secret

    def save(self, *args, **kwargs):
        if self._api_key and not self._api_key.startswith("gAAAA"):
            self._api_key = encrypt_value(self._api_key)
        if self._api_secret and not self._api_secret.startswith("gAAAA"):
            self._api_secret = encrypt_value(self._api_secret)
        super().save(*args, **kwargs)

    class Meta:
        db_table = "user_keys"


class UserHyperLiquidKey(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    master_wallet_address = models.CharField(max_length=255)
    api_wallet_address = models.CharField(max_length=255)
    api_private_key = models.CharField(max_length=255)
    api_valid_days = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_master_wallet_address(self, value):
        self.master_wallet_address = encrypt_value(value)

    def set_api_wallet_address(self, value):
        self.api_wallet_address = encrypt_value(value)

    def set_api_private_key(self, value):
        self.api_private_key = encrypt_value(value)

    def get_master_wallet_address(self):
        return decrypt_value(self.master_wallet_address)

    def get_api_wallet_address(self):
        return decrypt_value(self.api_wallet_address)

    def get_api_private_key(self):
        return decrypt_value(self.api_private_key)

    @property
    def encrypted_master_wallet_address(self):
        return self.master_wallet_address

    @property
    def encrypted_api_wallet_address(self):
        return self.api_wallet_address

    @property
    def encrypted_api_private_key(self):
        return self.api_private_key

    def save(self, *args, **kwargs):
        if self.master_wallet_address and not self.master_wallet_address.startswith(
            "gAAAA"
        ):
            self.master_wallet_address = encrypt_value(self.master_wallet_address)

        if self.api_wallet_address and not self.api_wallet_address.startswith("gAAAA"):
            self.api_wallet_address = encrypt_value(self.api_wallet_address)

        if self.api_private_key and not self.api_private_key.startswith("gAAAA"):
            self.api_private_key = encrypt_value(self.api_private_key)

        super().save(*args, **kwargs)

    class Meta:
        db_table = "user_hyperliquid_keys"

    def __str__(self):
        return f"{self.user} HyperLiquid Key"


class IPAddress(models.Model):
    ip_address = models.GenericIPAddressField(unique=True)
    http_proxy = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Example: http://user:pass@host:port",
    )
    https_proxy = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Example: http://user:pass@host:port",
    )
    is_active = models.BooleanField(default=True)
    last_used = models.DateTimeField(auto_now_add=True)
    usage_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "ip_addresses"
        indexes = [
            models.Index(fields=["is_active", "usage_count"]),
        ]
