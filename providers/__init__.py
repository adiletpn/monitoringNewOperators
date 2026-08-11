from __future__ import annotations

from typing import Dict

from .base import CallRecord, TelephonyProvider
from .kcell import KcellProvider

__all__ = ["CallRecord", "TelephonyProvider", "KcellProvider", "get_provider"]


def get_provider(cfg, operators: Dict[str, Dict]) -> TelephonyProvider:
    name = (getattr(cfg, "telephony_provider", "") or "kcell").strip().lower()
    if name == "kcell":
        return KcellProvider(cfg, operators)
    raise RuntimeError(f"Unknown TELEPHONY_PROVIDER: {name!r} (expected 'kcell')")
