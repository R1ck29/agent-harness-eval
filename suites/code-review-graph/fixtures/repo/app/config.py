"""Application settings."""

SETTINGS = {
    "DB_PATH": "orders.db",
    "MAX_RETRIES": 0,
    "BULK_THRESHOLD": 10,
    "TASK_HANDLER_PREFIX": "on_",
    "DEFAULT_TOPIC": "order.created",
}


def get(key, default=None):
    return SETTINGS.get(key, default)
