"""Alert channels: phone push (ntfy), email/SMS gateway, macOS notification."""

from .dispatch import AlertResult, deliver, notify_test

__all__ = ["AlertResult", "deliver", "notify_test"]
