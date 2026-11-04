from django.db import models


class WebhookForwardTarget(models.Model):
    """Extra URL that accepted webhooks are re-POSTed to (managed in admin).
    Risk-setting messages go to RISK targets, trade signals to TRADE targets.
    No secret/header is added to forwarded requests."""

    class Kind(models.TextChoices):
        TRADE = "trade", "Trade signals"
        RISK = "risk", "Risk settings"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    url = models.URLField(max_length=500)
    enabled = models.BooleanField(default=True)
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "id"]
        unique_together = [("kind", "url")]

    def __str__(self):
        return f"{self.kind}: {self.url}"

    @classmethod
    def urls_for(cls, kind):
        return list(cls.objects.filter(kind=kind, enabled=True).values_list("url", flat=True))


class WebhookForwardLog(models.Model):
    """One forwarding attempt (visible read-only in admin). The URL is stored
    without its query string so embedded secrets are never persisted."""

    kind = models.CharField(max_length=10, blank=True, default="")
    url = models.CharField(max_length=500)
    ok = models.BooleanField(default=False)
    status_code = models.IntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")
    duration_ms = models.IntegerField(default=0)
    event_id = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} {self.url} -> {self.status_code or self.error}"
