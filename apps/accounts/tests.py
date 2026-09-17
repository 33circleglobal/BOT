from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import UserHyperLiquidKey, UserKey

User = get_user_model()


class UserHyperLiquidKeyModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="alice", email="alice@example.com", password="pass12345"
        )

    def test_encrypts_fields_on_save(self):
        key = UserHyperLiquidKey(
            user=self.user,
            master_wallet_address="0xMaster",
            api_wallet_address="0xApiWallet",
            api_private_key="secret-priv-key",
            api_valid_days=30,
        )
        key.save()

        self.assertTrue(key.master_wallet_address.startswith("gAAAA"))
        self.assertTrue(key.api_wallet_address.startswith("gAAAA"))
        self.assertTrue(key.api_private_key.startswith("gAAAA"))
        self.assertEqual(key.get_master_wallet_address(), "0xMaster")
        self.assertEqual(key.get_api_wallet_address(), "0xApiWallet")
        self.assertEqual(key.get_api_private_key(), "secret-priv-key")

    def test_set_and_get_helpers_round_trip(self):
        key = UserHyperLiquidKey(user=self.user)
        key.set_master_wallet_address("0xMaster")
        key.set_api_wallet_address("0xApiWallet")
        key.set_api_private_key("secret-priv-key")
        key.save()

        self.assertEqual(key.get_master_wallet_address(), "0xMaster")
        self.assertEqual(key.get_api_wallet_address(), "0xApiWallet")
        self.assertEqual(key.get_api_private_key(), "secret-priv-key")

    def test_does_not_double_encrypt_already_encrypted_value(self):
        key = UserHyperLiquidKey(user=self.user)
        key.set_master_wallet_address("0xMaster")
        key.api_wallet_address = "0xApiWallet"
        key.api_private_key = "secret-priv-key"
        key.save()
        first_master = key.master_wallet_address

        key.save()

        self.assertEqual(key.master_wallet_address, first_master)


class ProfileViewHyperLiquidKeyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="bob", email="bob@example.com", password="pass12345"
        )
        self.client.login(username="bob", password="pass12345")

    def test_create_hyperliquid_key(self):
        response = self.client.post(
            reverse("accounts:profile"),
            {
                "action": "save_hyperliquid",
                "master_wallet_address": "0xMaster",
                "api_wallet_address": "0xApiWallet",
                "api_private_key": "secret-priv-key",
                "api_valid_days": "30",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("accounts:profile"))
        key = UserHyperLiquidKey.objects.get(user=self.user)
        self.assertEqual(key.get_master_wallet_address(), "0xMaster")
        self.assertEqual(key.api_valid_days, 30)
        self.assertTrue(key.is_active)

    def test_edit_hyperliquid_key(self):
        key = UserHyperLiquidKey(user=self.user)
        key.set_master_wallet_address("0xOld")
        key.set_api_wallet_address("0xOldApi")
        key.set_api_private_key("old-priv-key")
        key.save()

        self.client.post(
            reverse("accounts:profile"),
            {
                "action": "save_hyperliquid",
                "master_wallet_address": "0xNew",
                "api_wallet_address": "0xNewApi",
                "api_private_key": "new-priv-key",
                "api_valid_days": "10",
                "is_active": "",
            },
        )
        key.refresh_from_db()
        self.assertEqual(key.get_master_wallet_address(), "0xNew")
        self.assertFalse(key.is_active)

    def test_delete_hyperliquid_key(self):
        key = UserHyperLiquidKey(user=self.user)
        key.set_master_wallet_address("0xOld")
        key.set_api_wallet_address("0xOldApi")
        key.set_api_private_key("old-priv-key")
        key.save()

        self.client.post(
            reverse("accounts:profile"), {"action": "delete_hyperliquid"}
        )
        self.assertFalse(UserHyperLiquidKey.objects.filter(user=self.user).exists())

    def test_binance_and_hyperliquid_keys_are_independent(self):
        self.client.post(
            reverse("accounts:profile"),
            {
                "action": "save",
                "api_key": "binance-key",
                "api_secret": "binance-secret",
                "is_active": "on",
            },
        )
        self.client.post(
            reverse("accounts:profile"),
            {
                "action": "save_hyperliquid",
                "master_wallet_address": "0xMaster",
                "api_wallet_address": "0xApiWallet",
                "api_private_key": "secret-priv-key",
                "api_valid_days": "5",
                "is_active": "on",
            },
        )

        self.assertTrue(UserKey.objects.filter(user=self.user).exists())
        self.assertTrue(UserHyperLiquidKey.objects.filter(user=self.user).exists())
