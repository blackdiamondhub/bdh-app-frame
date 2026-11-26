"""Background worker: keeps the TVs in step with Frame.

Every ``INTERVAL`` seconds (sooner when poked by an upload, delete or stay
change) it asks the hub who is staying, ends guest stays that are over, and
syncs every TV that is on.
"""
import logging
import os
import sys
import threading
import time

from django.db import close_old_connections

from . import discovery, service, sync

log = logging.getLogger(__name__)

START_DELAY = 10
INTERVAL = 60
#: How often to look for Frame TVs on the network.
DISCOVER_EVERY = 10 * 60

_wake = threading.Event()
_started = False
_start_lock = threading.Lock()
_last_discover = None
_discovering = False


def discovering():
    """True from an admin pressing "Look for TVs now" until the search is done."""
    return _discovering


def discover_now():
    global _last_discover, _discovering
    _discovering = True
    _last_discover = None
    poke()


def _is_runtime(path):
    parts = path.replace("\\", "/").split("/")
    return parts[-3:] == ["bdh_sdk", "runtime", "__main__.py"]


def should_start(argv=None):
    """Start only in the server process (the app's container, Daphne, or runserver's reloaded child)."""
    if os.environ.get("BDH_FRAME_WORKER", "").lower() in ("0", "false", "off", "no"):
        return False
    argv = sys.argv if argv is None else argv
    if argv and _is_runtime(argv[0]):
        return True
    prog = os.path.basename(argv[0]) if argv else ""
    if "daphne" in prog:
        return True
    if len(argv) > 1 and prog in ("manage.py", "django-admin", "__main__.py"):
        if argv[1] == "runserver":
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv
    return False


def _discover_if_due():
    global _last_discover, _discovering
    now = time.monotonic()
    if _last_discover is not None and now - _last_discover < DISCOVER_EVERY:
        return
    _last_discover = now
    try:
        discovery.discover()
    except Exception:  # noqa: BLE001
        log.exception("Looking for Frame TVs failed")
    finally:
        _discovering = False


def tick():
    close_old_connections()
    _discover_if_due()
    try:
        active = service.evaluate()
    except Exception:  # noqa: BLE001
        log.exception("Checking the guest stay failed")
        return
    sync.sync_all(active)


def _loop():
    _wake.wait(START_DELAY)
    while True:
        _wake.clear()
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("Frame worker tick failed")
        _wake.wait(INTERVAL)


def poke():
    _wake.set()


def start():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    service.set_waker(poke)
    threading.Thread(target=_loop, name="frame-worker", daemon=True).start()
