from django.db import models


class WebhookLog(models.Model):
    """Every payload received on the TradingView webhook endpoint, kept
    verbatim (both the raw request body and, when it parses, the JSON
    itself) so a signal can be decoded/replayed later regardless of
    whether processing succeeded."""

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSED = "PROCESSED", "Processed"
        ERROR = "ERROR", "Error"

    raw_body = models.TextField(blank=True, default="")
    payload = models.JSONField(null=True, blank=True)

    # Denormalized from `payload` for quick filtering/sorting in the UI —
    # not authoritative, `payload` always is.
    action = models.CharField(max_length=50, blank=True, default="")
    symbol = models.CharField(max_length=20, blank=True, default="")
    side = models.CharField(max_length=10, blank=True, default="")
    market = models.CharField(max_length=10, blank=True, default="")
    exchange = models.CharField(max_length=20, blank=True, default="")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    error_message = models.TextField(blank=True, default="")

    remote_addr = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "webhook_logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["symbol", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        label = self.symbol or self.action or "webhook"
        return f"{label} @ {self.created_at:%Y-%m-%d %H:%M:%S}"
