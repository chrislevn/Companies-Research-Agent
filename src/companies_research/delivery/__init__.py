"""Delivery — the last step, and the only one that can leave the machine."""

from __future__ import annotations

from ..config import SETTINGS
from .base import DeliveryError, DeliveryOutcome, DeliveryProvider

__all__ = ["DeliveryError", "DeliveryOutcome", "DeliveryProvider",
           "PROVIDERS", "build_delivery"]

PROVIDERS = ("file", "gmail_send")


def build_delivery(name: str | None = None) -> DeliveryProvider:
    key = (name or SETTINGS.delivery_provider or "file").strip().lower()
    if key == "file":
        from .file import FileDelivery

        return FileDelivery()
    if key == "gmail_send":
        from .gmail_send import GmailSendDelivery

        return GmailSendDelivery()
    raise DeliveryError(
        f"Unknown DELIVERY_PROVIDER {key!r}. Choose one of: {', '.join(PROVIDERS)}."
    )
