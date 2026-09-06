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
        }
        labels = {
            "futures_max_positions": "Max total futures trades (open at once)",
            "futures_max_long": "Max LONG futures trades",
            "futures_max_short": "Max SHORT futures trades",
            "futures_position_pct": "Futures position size (% of available balance per trade)",
            "futures_leverage": "Futures leverage",
            "spot_max_positions": "Max total spot trades (open at once)",
            "spot_position_pct": "Spot position size (% of available balance per trade)",
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
        return cleaned
