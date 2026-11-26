"""Make each TV's My Photos match what Frame wants there.

Deletes come first (privacy: family photos leave the TV before guest photos
go up). Photos on the TV that Frame didn't upload are never touched.
"""
import logging
import threading
import random
import time

from django import db
from django.utils import timezone

from . import discovery, service, tv as tvlib
from .models import TV, Placement

log = logging.getLogger(__name__)

#: How often Frame asks an unpaired TV to let it in (each ask pops up on the TV).
PAIR_RETRY = 30 * 60
#: How long a pairing result (denied, timed out...) is shown on the TVs page.
PAIR_RESULT_FOR = 10 * 60
#: A TV whose photos and slideshow haven't changed is only re-checked this
#: often (every check opens a connection to the TV; they don't like many).
RECHECK = 10 * 60

_pair_tries = {}  # tv id -> time.monotonic() of the last ask
_pair_status = {}  # tv id -> {"state", "message", "at"}
_pair_lock = threading.Lock()
_synced = {}  # tv id -> (fingerprint, time.monotonic()) of the last good sync
_restart = set()  # tv ids to put back on a photo with the slideshow running


def restart_slideshows():
    """Someone pressed Start slideshow: on the next pass every TV in Art Mode
    shows one of our photos (which also leaves the Art Mode menu) and the
    slideshow is sent again."""
    _restart.update(TV.objects.exclude(token="").values_list("id", flat=True))

#: Pairing states shown on the TVs page.
WAITING, PAIRED, DENIED, TIMEOUT, STANDBY, OFFLINE, FAILED = (
    "waiting", "paired", "denied", "timeout", "standby", "offline", "failed")


def pair_waiting(tv_id):
    """True while the TV is showing Frame's Allow prompt."""
    st = _pair_status.get(tv_id)
    return bool(st) and st["state"] == WAITING and time.monotonic() - st["at"] < tvlib.PAIR_TIMEOUT + 5


def pair_status(tv_id):
    """The last pairing attempt for the TVs page: ``{"state", "message", "seconds_left"}`` or None."""
    st = _pair_status.get(tv_id)
    if not st:
        return None
    age = time.monotonic() - st["at"]
    if st["state"] == WAITING:
        if age >= tvlib.PAIR_TIMEOUT + 5:
            return None
        return {"state": WAITING, "message": "", "seconds_left": max(0, int(tvlib.PAIR_TIMEOUT - age))}
    if age >= PAIR_RESULT_FOR:
        return None
    return {"state": st["state"], "message": st["message"], "seconds_left": 0}


def _set_pair_status(tv_id, state, message=""):
    _pair_status[tv_id] = {"state": state, "message": message[:200], "at": time.monotonic()}


def _close_db():
    db.connection.close()


def _pair(tv_id, host):
    try:
        token = tvlib.do_pair(host)
    except tvlib.Standby as exc:
        _set_pair_status(tv_id, STANDBY, str(exc))
    except tvlib.Offline as exc:
        _set_pair_status(tv_id, OFFLINE, str(exc))
    except tvlib.PairDenied as exc:
        _set_pair_status(tv_id, DENIED, str(exc))
    except tvlib.PairTimeout as exc:
        _set_pair_status(tv_id, TIMEOUT, str(exc))
    except Exception as exc:  # noqa: BLE001 - shown on the TVs page
        _set_pair_status(tv_id, FAILED, str(exc) or type(exc).__name__)
    else:
        try:
            TV.objects.filter(id=tv_id).update(token=token, error="")
        finally:
            _close_db()
        _set_pair_status(tv_id, PAIRED)
        log.info("TV %s at %s paired", tv_id, host)
        service.wake_worker()
        return
    log.info("TV %s at %s didn't pair: %s", tv_id, host, _pair_status[tv_id]["state"])


def _start_pair(t, now):
    _pair_tries[t.id] = now
    _set_pair_status(t.id, WAITING)
    _spawn(_pair, t.id, t.host)


def maybe_pair(t):
    """Ask ``t`` to let Frame in, unless it was asked recently. Returns True if asked now."""
    now = time.monotonic()
    with _pair_lock:
        last = _pair_tries.get(t.id)
        if last is not None and now - last < PAIR_RETRY:
            return False
        _start_pair(t, now)
    return True


def pair_now(t):
    """An admin pressed Pair: ask the TV right away (unless it's already asking)."""
    with _pair_lock:
        if pair_waiting(t.id):
            return False
        _start_pair(t, time.monotonic())
    return True


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, name="frame-pair", daemon=True).start()


def sync_all(active=None):
    skip = discovery.skipped()
    for t in TV.objects.all():
        if t.host.startswith(discovery.MOVED):
            continue
        use = discovery.enabled(t, skip)
        # A TV an admin excluded still gets Frame's photos taken off it.
        if not use and not t.placements.exists():
            continue
        try:
            sync_tv(t, active, use=use)
        except Exception:  # noqa: BLE001 - one TV must not stop the others
            log.exception("Sync of %s failed", t)


def _mark(t, **fields):
    for k, v in fields.items():
        setattr(t, k, v)
    t.save(update_fields=list(fields))


def sync_tv(t, active=None, use=True):
    """Sync one TV; returns True if it is now up to date. ``use=False`` empties it."""
    try:
        if not tvlib.describe(t.host)["on"]:
            _synced.pop(t.id, None)  # it may lose its slideshow while off: check it all when it's back
            _mark(t, online=False)
            return False
    except tvlib.Offline:
        _synced.pop(t.id, None)
        _mark(t, online=False)
        return False
    now = timezone.now()
    if not t.token:
        _mark(t, online=True, last_seen=now, error="Not paired")
        if use:
            maybe_pair(t)
        return False
    wanted = service.wanted_on(t, active) if use else []
    fp = _fingerprint(t, wanted, use)
    last = _synced.get(t.id)
    if last and last[0] == fp and time.monotonic() - last[1] < RECHECK and not t.error and t.id not in _restart:
        _mark(t, online=True, last_seen=now)
        return True
    client = tvlib.client(t.host, t.token)
    try:
        ok = _sync(t, client, wanted, now, use)
    except tvlib.NotPaired as exc:
        _synced.pop(t.id, None)
        _mark(t, online=True, last_seen=now, token="", error="The TV stopped allowing Frame; it will ask again")
        log.warning("%s unpaired: %s", t, exc)
        return False
    except tvlib.TVError as exc:
        _synced.pop(t.id, None)
        _mark(t, online=True, last_seen=now, error=str(exc)[:250])
        log.warning("%s: %s", t, exc)
        return False
    finally:
        client.close()
        _restart.discard(t.id)
    _synced[t.id] = (_fingerprint(t, wanted, use), time.monotonic())
    return ok


def _fingerprint(t, wanted, use):
    """What Frame wants on ``t``; when it is unchanged the TV needs no visit."""
    return (use, tuple(p.id for p in wanted), service.slideshow_value(bool(wanted)),
            service.setting("matte", "none"),
            tuple(Placement.objects.filter(tv=t).order_by("id").values_list("id", "photo_id")))


def _sync(t, client, wanted, now, use=True):
    on_tv = client.my_photos()
    wanted_ids = {p.id for p in wanted}
    placements = list(Placement.objects.filter(tv=t))

    # 1. Take off everything that shouldn't be there (or vanished from the TV).
    gone = [pl for pl in placements if pl.photo_id is None or pl.photo_id not in wanted_ids]
    missing = [pl for pl in placements if pl not in gone and pl.content_id not in on_tv]
    to_delete = [pl.content_id for pl in gone if pl.content_id in on_tv]
    deleted = {pl.content_id for pl in gone}
    if to_delete:
        client.delete(to_delete)
        on_tv -= set(to_delete)
    Placement.objects.filter(id__in=[pl.id for pl in gone + missing]).delete()

    # 2. Upload what's wanted but not there.
    placed = {pl.photo_id: pl.content_id for pl in placements if pl not in gone and pl not in missing}
    matte = service.setting("matte", "none") or "none"
    uploaded = []
    for p in wanted:
        if p.id in placed:
            continue
        cid = client.upload(service.read_image(p), matte=matte)
        if not cid:
            raise tvlib.TVError("The TV didn't accept a photo")
        Placement.objects.create(photo=p, tv=t, content_id=cid)
        placed[p.id] = cid
        on_tv.add(cid)
        uploaded.append(p)

    # 3. Show a just-added photo right away (otherwise it waits its turn in the
    # slideshow), and never leave a photo that's no longer wanted on screen.
    ours = set(placed.values())
    showed = False
    if ours and client.art_mode():
        cur = client.current()
        if uploaded:
            newest = max(uploaded, key=lambda p: p.added_at)
            client.show(placed[newest.id])
            showed = True
        # Something we just took off is still on screen. Anything else (their
        # own photo, Samsung art) was picked on the TV; leave it.
        elif cur in deleted or (cur.startswith("MY_") and cur not in on_tv):
            client.show(placed[wanted[0].id])
            showed = True
        elif t.id in _restart:
            client.show(cur if cur in ours else random.choice(sorted(ours)))
            showed = True

    # 4. Slideshow over My Photos (off when there's nothing of ours to show).
    # Asked of the TV, not remembered: a reboot or someone using the TV's
    # menus can change it. Picking a photo (step 3) can stop it, so it is
    # always sent again after that.
    show = service.slideshow_value(bool(ours))
    try:
        on = client.slideshow()
    except tvlib.NotPaired:
        raise
    except tvlib.TVError:  # an older TV that can't say: trust what Frame last sent
        on = t.slideshow
    if show != on or (showed and show != "off"):
        if show == "off":
            client.set_slideshow(0, False)
        else:
            minutes, shuffle = show.split(":")
            client.set_slideshow(int(minutes), shuffle == "1")
    t.slideshow = show

    _mark(t, online=True, last_seen=now, last_sync=now, error="",
          unmanaged=len(on_tv - ours), slideshow=t.slideshow)
    return True
