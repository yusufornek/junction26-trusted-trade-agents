"""Optional CPU Timestral forecaster; importing the gateway never imports torch.

    Local weights must be the official mistralai/Timestral-28M checkpoint. Loading
    happens once during Gateway construction, never while checking an order.
"""
import logging
from pathlib import Path


class TimestralForecaster:
    def __init__(self, model_path):
        if not model_path or not Path(model_path).is_dir():
            raise ValueError("FORECAST_MODEL_PATH must name a local Timestral-28M directory")
        from timestral import Timestral
        self.model = Timestral.from_pretrained(str(model_path), local_files_only=True).to("cpu")
        self.model.eval()

    def predict(self, series, horizon):
        import torch
        with torch.inference_mode():
            prediction = self.model.predict(torch.tensor([list(series)], dtype=torch.float32, device="cpu"),
                                            prediction_length=horizon, quantiles=[.05, .95])
        # Adapter contract is scalar bounds for the final horizon step.
        values = prediction.quantiles if hasattr(prediction, "quantiles") else prediction
        values = torch.as_tensor(values).reshape(-1, horizon, 2)
        return {.05: float(values[0, -1, 0]), .95: float(values[0, -1, 1])}


def load_forecaster(enabled, model_path):
    if not enabled:
        return None
    try:
        return TimestralForecaster(model_path)
    except Exception:
        logging.getLogger(__name__).warning("Timestral unavailable; using statistical anomaly bands", exc_info=True)
        return None
