"""Turning TVs off (standby) and on from the app's power buttons.

Turning a TV on can take a minute (a sleeping TV's network has to wake first),
so it runs on a thread and the pages poll :func:`pending` / :func:`failed`.
Frame never touches the TVs' outlets: a TV it can't reach stays off.
"""
import logging
import threading
import time

from django import db

from . import service, tv as tvlib
from .models import TV

log = logging.getLogger(__name__)

ON, STANDBY, OFFLINE = "on", "standby", "offline"
#: How long a failed power change is shown under the TV.
FAILED_FOR = 2 * 60

_pending = {}  # tv id -> True (turning on) / False (turning off)
_failed = {}  # tv id -> (message, time.monotonic())
_lock = threading.Lock()


def state(t):
    """``"on"``, ``"standby"`` (off, but Frame can turn it on) or ``"offline"`` (can't reach it)."""
    if t.online:
        return ON
    return STANDBY if t.asleep else OFFLINE


def can(t):
    """Frame has a way to switch this TV, and it isn't unreachable."""
    return bool(t.token or t.ip_token) and state(t) != OFFLINE


def pending(tv_id):
    """``"on"``/``"off"`` while Frame is turning the TV on/off, else None."""
    p = _pending.get(tv_id)
    return None if p is None else ("on" if p else "off")


def failed(tv_id):
    """Why the last power change didn't work, for a couple of minutes; else ""."""
    f = _failed.get(tv_id)
    if not f or time.monotonic() - f[1] > FAILED_FOR:
        return ""
    return f[0]


def request(t, on):
    """Start turning ``t`` on/off. False if it's already doing that or can't be switched."""
    if not can(t):
        return False
    with _lock:
        if t.id in _pending:
            return False
        _pending[t.id] = bool(on)
        _failed.pop(t.id, None)
    _start(_run, t.id, bool(on))
    return True


def _thread(fn, *args):
    def go():
        try:
            fn(*args)
        finally:
            db.connection.close()
    threading.Thread(target=go, name="frame-power", daemon=True).start()


#: How :func:`request` runs the switch (tests run it inline).
_start = _thread


def _run(tv_id, on):
    try:
        t = TV.objects.filter(id=tv_id).first()
        if t is not None:
            _switch(t, on)
    except Exception as exc:  # noqa: BLE001 - shown under the TV
        log.exception("Turning TV %s %s failed", tv_id, "on" if on else "off")
        _failed[tv_id] = (str(exc) or type(exc).__name__, time.monotonic())
    finally:
        _pending.pop(tv_id, None)
        service.wake_worker()


def _switch(t, on):
    from . import sync

    try:
        if on:
            tvlib.power_on(t.host, t.token, t.ip_token, t.mac)
        else:
            tvlib.power_off(t.host, t.token, t.ip_token)
    except tvlib.NotPaired as exc:
        if t.ip_token:
            # The TV forgot Frame's power-control token: fall back to its power key.
            log.warning("%s dropped the IP Control token: %s", t, exc)
            TV.objects.filter(id=t.id).update(ip_token="")
            t.ip_token = ""
            return _switch(t, on)
        raise
    except tvlib.TVError as exc:
        log.info("%s didn't turn %s: %s", t, "on" if on else "off", exc)
        _failed[t.id] = (str(exc), time.monotonic())
        return
    sync._synced.pop(t.id, None)  # look at it afresh on the next pass
    TV.objects.filter(id=t.id).update(online=on, asleep=not on)
    log.info("Turned %s %s", t, "on" if on else "off")


def reset():
    """Tests: forget everything in flight."""
    _pending.clear()
    _failed.clear()
    _ip_pair.clear()


# --- setting up IP Control (an admin, at the TVs page) -------------------------------------

_ip_pair = {}  # tv id -> {"state", "message", "at"}


def ip_pair_status(tv_id):
    """The last power-control pairing for the TVs page: ``{"state", "message", "seconds_left"}`` or None."""
    from . import sync

    st = _ip_pair.get(tv_id)
    if not st:
        return None
    age = time.monotonic() - st["at"]
    if st["state"] == sync.WAITING:
        if age >= tvlib.PAIR_TIMEOUT + 5:
            return None
        return {"state": sync.WAITING, "message": "", "seconds_left": max(0, int(tvlib.PAIR_TIMEOUT - age))}
    if age >= sync.PAIR_RESULT_FOR:
        return None
    return {"state": st["state"], "message": st["message"], "seconds_left": 0}


def _ip_set(tv_id, state_, message=""):
    _ip_pair[tv_id] = {"state": state_, "message": message[:200], "at": time.monotonic()}


def ip_pair_now(t):
    """Ask the TV for a power-control token; False if it's already asking."""
    from . import sync

    with _lock:
        st = _ip_pair.get(t.id)
        if st and st["state"] == sync.WAITING and time.monotonic() - st["at"] < tvlib.PAIR_TIMEOUT + 5:
            return False
        _ip_set(t.id, sync.WAITING)
    _start(_ip_pair_run, t.id, t.host)
    return True


def _ip_pair_run(tv_id, host):
    from . import sync

    try:
        token = tvlib.ip_pair(host)
    except tvlib.Offline as exc:
        _ip_set(tv_id, sync.OFFLINE, str(exc))
    except tvlib.PairDenied as exc:
        _ip_set(tv_id, sync.DENIED, str(exc))
    except tvlib.PairTimeout as exc:
        _ip_set(tv_id, sync.TIMEOUT, str(exc))
    except Exception as exc:  # noqa: BLE001 - shown on the TVs page
        _ip_set(tv_id, sync.FAILED, str(exc) or type(exc).__name__)
    else:
        TV.objects.filter(id=tv_id).update(ip_token=token)
        _ip_set(tv_id, sync.PAIRED)
        log.info("TV %s at %s: power control set up", tv_id, host)
