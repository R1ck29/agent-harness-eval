"""String-keyed pub/sub."""
from app.config import get

_SUBSCRIBERS = {}


def subscribe(topic, fn):
    _SUBSCRIBERS.setdefault(topic, []).append(fn)


def publish(topic, payload):
    for fn in _SUBSCRIBERS.get(topic, []):
        fn(payload)


def publish_order_created(payload):
    publish(get("DEFAULT_TOPIC"), payload)
