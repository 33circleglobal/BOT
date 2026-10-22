from django import forms

from apps.trade.models import TradeSettings


class TradeSettingsForm(forms.ModelForm):
    class Meta:
        model = TradeSettings
        fields = [
            "futures_max_positions",
            "futures_max_long",
            "futures_max_short",
            "futures_position_pct",
            "futures_leverage",
            "spot_max_positions",
            "spot_position_pct",
            "sync_risk_from_webhook",
            "max_market_data_age_minutes",
            "hl_sync_from_webhook",
            "hl_spot_max_positions",
            "hl_futures_max_long",
            "hl_futures_max_short",
            "hl_futures_max_positions",
            "hl_futures_leverage",
            "hl_spot_trade_pct",
            "hl_futures_margin_pct",
        ]
        widgets = {
            "futures_max_positions": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
            "futures_max_long": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
            "futures_max_short": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
            "futures_position_pct": forms.NumberInput(
                attrs={"class": "form-control", "min": 0.01, "max": 100, "step": "0.01"}
            ),
            "futures_leverage": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 125}
            ),
            "spot_max_positions": forms.NumberInput(
                attrs={"class": "form-control", "min": 0}
            ),
            "spot_position_pct": forms.NumberInput(
                attrs={"class": "form-control", "min": 0.01, "max": 100, "step": "0.01"}
            ),
            "sync_risk_from_webhook": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
            "max_market_data_age_minutes": forms.NumberInput(
                attrs={"class": "form-control", "min": 1, "max": 1440}
            ),
            "hl_sync_from_webhook": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "hl_spot_max_positions": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "hl_futures_max_long": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "hl_futures_max_short": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "hl_futures_max_positions": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "hl_futures_leverage": forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 125}),
            "hl_spot_trade_pct": forms.NumberInput(attrs={"class": "form-control", "min": 0.01, "max": 100, "step": "0.01"}),
            "hl_futures_margin_pct": forms.NumberInput(attrs={"class": "form-control", "min": 0.01, "max": 100, "step": "0.01"}),
        }
        labels = {
            "hl_sync_from_webhook": "Auto-update HyperLiquid limits from the 122Moon controller",
            "hl_spot_max_positions": "Max spot trades (manual, used when auto-update is off)",
            "hl_futures_max_long": "Max LONG futures trades (manual)",
            "hl_futures_max_short": "Max SHORT futures trades (manual)",
            "hl_futures_max_positions": "Max total futures trades (manual)",
            "hl_futures_leverage": "Max leverage ceiling",
            "hl_spot_trade_pct": "Spot position size (% of spot pool per trade)",
            "hl_futures_margin_pct": "Futures position size (% of futures pool, as margin)",
            "futures_max_positions": "Max total futures trades (open at once)",
            "futures_max_long": "Max LONG futures trades",
            "futures_max_short": "Max SHORT futures trades",
            "futures_position_pct": "Futures position size (% of available balance per trade)",
            "futures_leverage": "Futures leverage",
            "spot_max_positions": "Max total spot trades (open at once)",
            "spot_position_pct": "Spot position size (% of available balance per trade)",
            "sync_risk_from_webhook": "Auto-update these limits from the market-regime webhook",
            "max_market_data_age_minutes": "Stop opening new trades if no webhook update arrives within (minutes)",
        }

    def clean(self):
        cleaned = super().clean()
        max_positions = cleaned.get("futures_max_positions")
        max_long = cleaned.get("futures_max_long")
        max_short = cleaned.get("futures_max_short")
        if max_positions is not None and max_long is not None and max_long > max_positions:
            self.add_error(
                "futures_max_long",
                "Cannot exceed max total futures trades.",
            )
        if max_positions is not None and max_short is not None and max_short > max_positions:
            self.add_error(
                "futures_max_short",
                "Cannot exceed max total futures trades.",
            )
        hl_total = cleaned.get("hl_futures_max_positions")
        for f in ("hl_futures_max_long", "hl_futures_max_short"):
            v = cleaned.get(f)
            if hl_total is not None and v is not None and v > hl_total:
                self.add_error(f, "Cannot exceed max total futures trades.")
        return cleaned
