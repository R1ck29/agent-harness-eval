"""Background jobs using dynamic dispatch."""
from app import config, handlers


def run_task(name, payload):
    fn_name = config.get("TASK_HANDLER_PREFIX") + name
    fn = getattr(handlers, fn_name, None)
    if fn is None:
        raise LookupError(fn_name)
    return fn(payload)
