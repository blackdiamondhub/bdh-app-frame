"""Talking to a Samsung Frame TV (samsungtvws).

:class:`FrameClient` is the small surface the rest of Frame uses, so tests
can swap in a fake with :func:`set_factory`.
"""
import logging
import socket
import threading
import time

import requests

log = logging.getLogger(__name__)

#: The name the TV shows when asking to allow the connection.
CLIENT_NAME = "BDH Frame"
MY_PHOTOS = "MY-C0002"
TIMEOUT = 15
#: How long to wait while the TV shows its Allow prompt (when pairing, or when a paired TV
#: asks again) before giving up for now.
ALLOW_WAIT = 60
PAIR_TIMEOUT = ALLOW_WAIT
#: Longest one request to a TV may take. samsungtvws waits for its reply with no overall
#: limit, so a TV that drops a reply (seen mid-upload) would otherwise hang the worker for good.
CALL_DEADLINE = 60
UPLOAD_DEADLINE = 180


class TVError(Exception):
    pass


class Offline(TVError):
    """The TV is off (or unplugged); try again later."""


class NotPaired(TVError):
    """The TV refused us: someone needs to pair (press Allow on the TV)."""


class Standby(TVError):
    """The TV answers but its screen is off; it only asks to allow Frame when it's on."""


class WaitingForAllow(TVError):
    """The TV is on but didn't let Frame in: it's showing an Allow prompt for BDH Frame.

    Frame keeps its token (the TV takes it back once someone presses Allow) but
    mustn't reconnect every pass: each try puts the prompt up again.
    """


class PairDenied(NotPaired):
    """Someone pressed Deny, or the TV is set to block new devices."""


class PairTimeout(NotPaired):
    """Nobody pressed Allow in time (or the TV never asked)."""


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
        # Despite the name, the MAC of whichever network the TV is on (wired too).
        "mac": str(d.get("wifiMac") or "").lower()[:17],
    }


# --- power -----------------------------------------------------------------------------
#
# 2024+ Frames treat the websocket power key as "Art Mode <-> TV" (2025 ones always do),
# so Frame turns TVs off and on through Samsung's IP Control instead: JSON-RPC over
# HTTPS on port 1516, on once "IP Remote" is turned on in the TV's Expert Settings.
# A TV that has been off a while stops answering on the network; a Wake-on-LAN
# packet brings that back. TVs without IP Control fall back to the power key.

IP_PORT = 1516
#: How long turning a TV on may take (a sleeping TV's network takes ~a minute to wake).
POWER_ON_WAIT = 90
POWER_OFF_WAIT = 20
#: The TV's answer to powerOn whether or not it worked ("Server error", data "Failed").
_IP_ON_REPLY = -32002


def _ip_rpc(host, method, params=None, timeout=10):
    import http.client
    import json
    import ssl

    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:  # the TV rejects an empty "params" (HTTP 400)
        body["params"] = params
    raw = json.dumps(body, separators=(",", ":")).encode()
    conn = http.client.HTTPSConnection(host, IP_PORT, timeout=timeout, context=ssl._create_unverified_context())
    try:
        conn.request("POST", "/", raw, {"Content-Type": "application/json", "Accept": "application/json",
                                         "Content-Length": str(len(raw))})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
    except TimeoutError:
        raise
    except OSError as exc:
        raise Offline(str(exc) or type(exc).__name__) from exc
    finally:
        conn.close()
    try:
        return json.loads(text)
    except ValueError:
        raise TVError(f"IP Control answered {resp.status}") from None


def ip_pair(host):
    """Ask the TV for an IP Control token; returns it once someone presses Allow.

    The TV only asks while it's showing TV (not Art Mode). Raises :class:`Offline`
    when IP Remote is off, :class:`PairTimeout` or :class:`PairDenied` otherwise.
    """
    try:
        reply = _ip_rpc(host, "createAccessToken", timeout=PAIR_TIMEOUT)
    except TimeoutError as exc:
        raise PairTimeout("Nobody pressed Allow") from exc
    token = ((reply or {}).get("result") or {}).get("AccessToken")
    if not token:
        raise PairDenied(str((reply or {}).get("error") or "The TV didn't give Frame a token"))
    return str(token)


def _ip_power(host, ip_token, power):
    """Send ``powerOn``/``powerOff``. A stale token raises :class:`NotPaired`."""
    try:
        reply = _ip_rpc(host, "powerControl", {"AccessToken": ip_token, "power": power})
    except TimeoutError as exc:
        raise Offline("The TV didn't answer") from exc
    err = (reply or {}).get("error") or (reply if "code" in (reply or {}) else None)
    if err and err.get("code") == -32700:  # what this firmware says for a missing/stale token
        raise NotPaired("The TV no longer accepts Frame's power-control token")
    if err and not (power == "powerOn" and err.get("code") == _IP_ON_REPLY):
        raise TVError(f"The TV said {err.get('message') or err}")


def wake_on_lan(host, mac):
    """Wake a TV whose network has gone to sleep."""
    try:
        packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16
    except ValueError:
        return
    targets = ["255.255.255.255"]
    if host.count(".") == 3:
        targets.append(host.rsplit(".", 1)[0] + ".255")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            try:
                s.sendto(packet, (target, 9))
            except OSError:
                pass


def _state(host):
    """True on, False standby, None not answering."""
    try:
        return describe(host)["on"]
    except Offline:
        return None


def _power_key(host, token, hold):
    from samsungtvws import SamsungTVWS

    tv = SamsungTVWS(host, port=8002, token=token or None, name=CLIENT_NAME, timeout=TIMEOUT)
    try:
        if hold:
            tv.hold_key("KEY_POWER", 3)
        else:
            tv.shortcuts().power()
    except Exception as exc:  # noqa: BLE001
        raise TVError(str(exc) or type(exc).__name__) from exc
    finally:
        try:
            tv.close()
        except Exception:  # noqa: BLE001
            pass


def _wait(host, on, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if _state(host) is on:
            return True
        time.sleep(2)
    return False


def power_off(host, token="", ip_token=""):
    """Put the TV in standby. Raises :class:`TVError` (or :class:`NotPaired` for a stale
    IP Control token) if it can't."""
    st = _state(host)
    if st is None:
        raise Offline("Frame can't reach the TV")
    if st is False:
        return
    if ip_token:
        _ip_power(host, ip_token, "powerOff")
    elif token:
        _power_key(host, token, hold=True)
    else:
        raise NotPaired("Frame isn't paired with the TV")
    if not _wait(host, False, POWER_OFF_WAIT):
        raise TVError("The TV didn't turn off")


def power_on(host, token="", ip_token="", mac=""):
    """Wake the TV up. Raises :class:`TVError` if it isn't on within :data:`POWER_ON_WAIT`."""
    end = time.monotonic() + POWER_ON_WAIT
    keyed = 0.0
    while time.monotonic() < end:
        st = _state(host)
        if st:
            return
        if st is None and mac:
            wake_on_lan(host, mac)
        if st is False:  # answering: ask it to turn on
            if ip_token:
                try:
                    _ip_power(host, ip_token, "powerOn")
                except Offline:
                    pass
            elif token and time.monotonic() - keyed > 15:
                # The power key toggles, so press it once and give the TV time.
                keyed = time.monotonic()
                try:
                    _power_key(host, token, hold=False)
                except TVError:
                    pass
            elif not token:
                raise NotPaired("Frame isn't paired with the TV")
        if _wait(host, True, 5):
            return
    raise TVError("The TV didn't turn on" if mac or st is not None else "Frame can't reach the TV")


def _asking(exc):
    """While a TV shows its Allow prompt, opening the channel times out, or the TV
    sends some other event (d2d_service_message) instead of ms.channel.connect."""
    return (isinstance(exc, TimeoutError) or "timed out" in str(exc).lower()
            or type(exc).__name__ in ("WebSocketTimeoutException", "ConnectionFailure"))


class FrameClient:
    """One TV's art channel. Not thread-safe; the worker owns it."""

    def __init__(self, host, token):
        self.host = host
        self.token = token
        # Set when the TV hands out a new token (it can after someone presses Allow);
        # the caller must save it or the TV asks again on the next connection.
        self.new_token = None
        self._art = None
        self._expired = False

    def _abort(self):
        """The call ran past its deadline: shut the socket so the blocked read fails."""
        self._expired = True
        sock = getattr(getattr(self._art, "connection", None), "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _conn(self):
        if self._art is None:
            from samsungtvws import SamsungTVWS

            # Open with room for someone to press Allow, then go back to the normal timeout.
            tv = SamsungTVWS(self.host, port=8002, token=self.token or None, name=CLIENT_NAME,
                             timeout=ALLOW_WAIT)
            self._art = tv.art()
            self._art.open()
            self._art.timeout = TIMEOUT
            if self._art.connection is not None:
                self._art.connection.settimeout(TIMEOUT)
        return self._art

    def _call(self, fn, *args, deadline=None, **kw):
        self._expired = False
        limit = (deadline or CALL_DEADLINE) + (ALLOW_WAIT if self._art is None else 0)
        timer = threading.Timer(limit, self._abort)
        timer.daemon = True
        timer.start()
        try:
            return fn(self._conn(), *args, **kw)
        except TVError:
            raise
        except Exception as exc:  # noqa: BLE001 - websocket, socket and protocol errors
            self._grab_token()
            connected = getattr(self._art, "connection", None) is not None
            self.close()
            if self._expired:
                raise TVError("The TV stopped answering") from exc
            text = str(exc)
            if "unauthorized" in text.lower() or "ms.channel.unauthorized" in text:
                raise NotPaired(text) from exc
            if not connected and _asking(exc):
                raise WaitingForAllow(text or type(exc).__name__) from exc
            raise TVError(text or type(exc).__name__) from exc
        finally:
            timer.cancel()
            self._grab_token()

    def _grab_token(self):
        got = getattr(self._art, "token", None)
        if got and got != (self.new_token or self.token):
            self.new_token = got

    def my_photos(self):
        """Content ids in the TV's My Photos."""
        items = self._call(lambda a: a.available(MY_PHOTOS))
        return {i["content_id"] for i in items or () if i.get("content_id")}

    def upload(self, data):
        # Never a matte: the art app crashes (error 40000) on a matted non-16:9 photo.
        return self._call(lambda a: a.upload(data, file_type="JPEG", matte="none", portrait_matte="none"),
                          deadline=UPLOAD_DEADLINE)

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

    def slideshow(self):
        """The TV's slideshow as ``"off"`` or ``"<minutes>:<shuffle 0|1>"`` (``""`` if it isn't over My Photos)."""
        s = self._call(lambda a: a.get_slideshow_status()) or {}
        value = str(s.get("value") or "off").lower()
        if value == "off":
            return "off"
        if s.get("category_id") != MY_PHOTOS:
            return ""
        return f"{value}:{1 if s.get('type') == 'shuffleslideshow' else 0}"

    def close(self):
        art, self._art = self._art, None
        if art is not None:
            try:
                art.close()
            except Exception:  # noqa: BLE001
                pass


def pair(host):
    """Ask the TV to let Frame in; returns the token once someone presses Allow.

    Blocks until then (or :data:`PAIR_TIMEOUT`). Raises :class:`Offline` or
    :class:`Standby` if the TV can't ask, :class:`PairDenied` if they press
    Deny, :class:`PairTimeout` if nobody answers.
    """
    from samsungtvws import SamsungTVWS

    if not describe(host)["on"]:
        raise Standby("The TV's screen is off")
    tv = SamsungTVWS(host, port=8002, name=CLIENT_NAME, timeout=PAIR_TIMEOUT)
    try:
        tv.open()
        if not tv.token:
            raise PairDenied("The TV didn't give Frame a token")
        return tv.token
    except TVError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise pair_error(exc) from exc
    finally:
        try:
            tv.close()
        except Exception:  # noqa: BLE001
            pass


def pair_error(exc):
    """What a failed :func:`pair` means, from samsungtvws/websocket's exception."""
    name = type(exc).__name__
    text = str(exc)
    if name == "UnauthorizedError" or "unauthorized" in text.lower():
        return PairDenied(text or "The TV said no")
    if "timeout" in name.lower() or "timed out" in text.lower():
        return PairTimeout(text or "Nobody pressed Allow")
    if isinstance(exc, OSError):
        return Offline(text or name)
    return NotPaired(text or name)


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
