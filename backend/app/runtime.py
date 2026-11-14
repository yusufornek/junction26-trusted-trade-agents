"""Request-local environment and fixture time; never changes the live process clock."""
from contextvars import ContextVar
from contextlib import contextmanager

ENVIRONMENT = ContextVar("environment", default="live")
CLOCK = ContextVar("fixture_clock", default=None)
SUPPLIER = ContextVar("supplier", default="valio")
TRANSPORT = ContextVar("transport", default="rest")


@contextmanager
def scope(environment="live", clock=None):
    env_token, clock_token = ENVIRONMENT.set(environment), CLOCK.set(clock)
    try:
        yield
    finally:
        CLOCK.reset(clock_token)
        ENVIRONMENT.reset(env_token)
