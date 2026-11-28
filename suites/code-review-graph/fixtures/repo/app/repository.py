"""Repository layer: turns rows into models."""
from app import db
from app.config import get
from app.models import Order


def get_order(order_id):
    """Load an order by id, retrying per MAX_RETRIES."""
    attempts = get("MAX_RETRIES") + 1
    for _ in range(attempts):
        row = db.fetch_order_row(order_id)
        if row:
            return Order(*row)
    return None
