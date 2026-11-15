"""Optional CPU Timestral forecaster (timestral 1.0 API: TimestralForecaster.from_pretrained / forecast); importing the gateway never imports torch.

    Local weights must be the official mistralai/Timestral-28M checkpoint. Loading
    happens once during Gateway construction, never while checking an order.
"""
import logging
from pathlib import Path


class TimestralForecaster:
    def __init__(self, model_path):
        if not model_path or not Path(model_path).is_dir():
            raise ValueError("FORECAST_MODEL_PATH must name a local Timestral-28M directory")
        from timestral import TimestralForecaster as _Forecaster
        self.model = _Forecaster.from_pretrained(str(model_path), device="cpu", local_files_only=True)

    def predict(self, series, horizon):
        import torch
        with torch.inference_mode():
            forecast = self.model.forecast([[float(x) for x in series]], prediction_length=horizon)
        # Adapter contract: scalar lower/upper bounds under the keys .05/.95 for the final horizon step. For anomaly
        # detection the model's 1 % and 99 % quantiles are used: its 5-95 % band flags ordinary values (checked
        # against the real Timestral-28M weights on 15.11).
        low = forecast.quantile(0.01).reshape(-1)[-1]
        high = forecast.quantile(0.99).reshape(-1)[-1]
        return {.05: float(low), .95: float(high)}


def load_forecaster(enabled, model_path):
    if not enabled:
        return None
    try:
        return TimestralForecaster(model_path)
    except Exception:
        logging.getLogger(__name__).warning("Timestral unavailable; using statistical anomaly bands", exc_info=True)
        return None
