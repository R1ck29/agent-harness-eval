"""HTTP layer."""
from app import orders

ROUTES = {}


def route(path):
    def deco(fn):
        ROUTES[path] = fn
        return fn
    return deco


@route("/orders")
def create(payload):
    customer_id = payload["customerId"]
    return orders.create_order(customer_id, payload["quantity"], payload["unit_cents"])
