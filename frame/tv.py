"""Talking to a Samsung Frame TV (samsungtvws).

:class:`FrameClient` is the small surface the rest of Frame uses, so tests
can swap in a fake with :func:`set_factory`.
"""
import logging
import threading

import requests

log = logging.getLogger(__name__)

#: The name the TV shows when asking to allow the connection.
CLIENT_NAME = "BDH Frame"
MY_PHOTOS = "MY-C0002"
TIMEOUT = 15
PAIR_TIMEOUT = 45


class TVError(Exception):
    pass


class Offline(TVError):
    """The TV is off (or unplugged); try again later."""


class NotPaired(TVError):
    """The TV refused us: someone needs to pair (press Allow on the TV)."""


def info(host, timeout=4):
    """The TV's ``/api/v2/`` device info, or raise :class:`Offline`."""
    try:
        r = requests.get(f"http://{host}:8001/api/v2/", timeout=timeout)
        r.raise_for_status()
        return (r.json() or {}).get("device") or {}
    except (requests.RequestException, ValueError) as exc:
        raise Offline(str(exc)) from exc


def describe(host):
    """``{"uid", "name", "model", "frame", "on"}`` for the TV at ``host``."""
    d = info(host)
    return {
        "uid": str(d.get("id") or "")[:64],
        "name": str(d.get("name") or "Samsung TV").strip(),
        "model": str(d.get("modelName") or ""),
        "frame": str(d.get("FrameTVSupport", "")).lower() == "true",
        "on": str(d.get("PowerState", "on")).lower() == "on",
    }


class FrameClient:
    """One TV's art channel. Not thread-safe; the worker owns it."""

    def __init__(self, host, token):
        self.host = host
        self.token = token
        self._art = None

    def _conn(self):
        if self._art is None:
            from samsungtvws import SamsungTVWS

            tv = SamsungTVWS(self.host, port=8002, token=self.token or None, name=CLIENT_NAME,
                             timeout=TIMEOUT)
            self._art = tv.art()
        return self._art

    def _call(self, fn, *args, **kw):
        try:
            return fn(self._conn(), *args, **kw)
        except TVError:
            raise
        except Exception as exc:  # noqa: BLE001 - websocket, socket and protocol errors
            self.close()
            text = str(exc)
            if "unauthorized" in text.lower() or "ms.channel.unauthorized" in text:
                raise NotPaired(text) from exc
            raise TVError(text or type(exc).__name__) from exc

    def my_photos(self):
        """Content ids in the TV's My Photos."""
        items = self._call(lambda a: a.available(MY_PHOTOS))
        return {i["content_id"] for i in items or () if i.get("content_id")}

    def upload(self, data, matte="none"):
        return self._call(lambda a: a.upload(data, file_type="JPEG", matte=matte, portrait_matte=matte))

    def delete(self, content_ids):
        ids = list(content_ids)
        if ids:
            self._call(lambda a: a.delete_list(ids))

    def current(self):
        return (self._call(lambda a: a.get_current()) or {}).get("content_id", "")

    def art_mode(self):
        return str(self._call(lambda a: a.get_artmode()) or "").lower() == "on"

    def show(self, content_id):
        self._call(lambda a: a.select_image(content_id, show=True))

    def set_slideshow(self, minutes, shuffle):
        self._call(lambda a: a.set_slideshow_status(duration=minutes, type=shuffle, category_id=MY_PHOTOS))

    def close(self):
        art, self._art = self._art, None
        if art is not None:
            try:
                art.close()
            except Exception:  # noqa: BLE001
                pass


def pair(host):
    """Ask the TV to let Frame in; returns the token once someone presses Allow.

    Blocks until then (or :data:`PAIR_TIMEOUT`). Raises :class:`NotPaired` if
    they press Deny or nobody answers.
    """
    from samsungtvws import SamsungTVWS

    tv = SamsungTVWS(host, port=8002, name=CLIENT_NAME, timeout=PAIR_TIMEOUT)
    try:
        tv.open()
        if not tv.token:
            raise NotPaired("The TV didn't give Frame a token")
        return tv.token
    except TVError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NotPaired(str(exc) or "Nobody allowed Frame on the TV") from exc
    finally:
        try:
            tv.close()
        except Exception:  # noqa: BLE001
            pass


_factory = FrameClient
_pairer = pair
_lock = threading.Lock()


def client(host, token):
    return _factory(host, token)


def do_pair(host):
    return _pairer(host)


def set_factory(factory=None, pairer=None):
    """Tests: replace the TV client (and pairing). ``None`` restores the real ones."""
    global _factory, _pairer
    with _lock:
        _factory = factory or FrameClient
        _pairer = pairer or pair
