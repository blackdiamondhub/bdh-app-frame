"""Make each TV's My Photos match what Frame wants there.

Deletes come first (privacy: family photos leave the TV before guest photos
go up). Photos on the TV that Frame didn't upload are never touched.
"""
import logging
import threading
import time

from django import db
from django.utils import timezone

from . import discovery, service, tv as tvlib
from .models import TV, Placement

log = logging.getLogger(__name__)

#: How often Frame asks an unpaired TV to let it in (each ask pops up on the TV).
PAIR_RETRY = 30 * 60

_pair_tries = {}  # tv id -> time.monotonic() of the last ask
_pair_lock = threading.Lock()


def pair_waiting(tv_id):
    """True while the TV is showing Frame's Allow prompt."""
    last = _pair_tries.get(tv_id)
    return last is not None and time.monotonic() - last < tvlib.PAIR_TIMEOUT + 5


def _close_db():
    db.connection.close()


def _pair(tv_id, host):
    try:
        token = tvlib.do_pair(host)
    except tvlib.TVError as exc:
        log.info("TV %s at %s didn't pair: %s", tv_id, host, exc)
        return
    try:
        TV.objects.filter(id=tv_id).update(token=token, error="")
    finally:
        _close_db()
    log.info("TV %s at %s paired", tv_id, host)
    service.wake_worker()


def maybe_pair(t):
    """Ask ``t`` to let Frame in, unless it was asked recently. Returns True if asked now."""
    now = time.monotonic()
    with _pair_lock:
        last = _pair_tries.get(t.id)
        if last is not None and now - last < PAIR_RETRY:
            return False
        _pair_tries[t.id] = now
    _spawn(_pair, t.id, t.host)
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
            _mark(t, online=False)
            return False
    except tvlib.Offline:
        _mark(t, online=False)
        return False
    now = timezone.now()
    if not t.token:
        _mark(t, online=True, last_seen=now, error="Not paired")
        if use:
            maybe_pair(t)
        return False
    client = tvlib.client(t.host, t.token)
    try:
        return _sync(t, client, active, now, use)
    except tvlib.NotPaired as exc:
        _mark(t, online=True, last_seen=now, token="", error="The TV stopped allowing Frame; it will ask again")
        log.warning("%s unpaired: %s", t, exc)
        return False
    except tvlib.TVError as exc:
        _mark(t, online=True, last_seen=now, error=str(exc)[:250])
        log.warning("%s: %s", t, exc)
        return False
    finally:
        client.close()


def _sync(t, client, active, now, use=True):
    on_tv = client.my_photos()
    wanted = service.wanted_on(t, active) if use else []
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

    # 3. Slideshow over My Photos (off when there's nothing of ours to show).
    ours = set(placed.values())
    show = service.slideshow_value(bool(ours))
    if show != t.slideshow:
        if show == "off":
            client.set_slideshow(0, False)
        else:
            minutes, shuffle = show.split(":")
            client.set_slideshow(int(minutes), shuffle == "1")
        t.slideshow = show

    # 4. Show a just-added photo right away (otherwise it waits its turn in the
    # slideshow), and never leave a photo that's no longer wanted on screen.
    if ours and client.art_mode():
        cur = client.current()
        if uploaded:
            newest = max(uploaded, key=lambda p: p.added_at)
            client.show(placed[newest.id])
        # Something we just took off is still on screen. Anything else (their
        # own photo, Samsung art) was picked on the TV; leave it.
        elif cur in deleted or (cur.startswith("MY_") and cur not in on_tv):
            client.show(placed[wanted[0].id])

    _mark(t, online=True, last_seen=now, last_sync=now, error="",
          unmanaged=len(on_tv - ours), slideshow=t.slideshow)
    return True
