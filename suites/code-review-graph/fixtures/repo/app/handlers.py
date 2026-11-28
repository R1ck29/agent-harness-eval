"""Event handlers, wired by topic string."""
from app import events


def on_order_created(payload):
    return f"handled {payload.get('id')}"


events.subscribe("order_created", on_order_created)
