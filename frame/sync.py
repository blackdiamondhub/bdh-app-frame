"""Make each TV's My Photos match what Frame wants there.

Deletes come first (privacy: family photos leave the TV before guest photos
go up). Photos on the TV that Frame didn't upload are never touched.
"""
import logging

from django.utils import timezone

from . import service, tv as tvlib
from .models import TV, Placement

log = logging.getLogger(__name__)


def sync_all(active=None):
    for t in TV.objects.all():
        try:
            sync_tv(t, active)
        except Exception:  # noqa: BLE001 - one TV must not stop the others
            log.exception("Sync of %s failed", t)


def _mark(t, **fields):
    for k, v in fields.items():
        setattr(t, k, v)
    t.save(update_fields=list(fields))


def sync_tv(t, active=None):
    """Sync one TV; returns True if it is now up to date."""
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
        return False
    client = tvlib.client(t.host, t.token)
    try:
        return _sync(t, client, active, now)
    except tvlib.NotPaired as exc:
        _mark(t, online=True, last_seen=now, token="", error="The TV stopped allowing Frame; pair again")
        log.warning("%s unpaired: %s", t, exc)
        return False
    except tvlib.TVError as exc:
        _mark(t, online=True, last_seen=now, error=str(exc)[:250])
        log.warning("%s: %s", t, exc)
        return False
    finally:
        client.close()


def _sync(t, client, active, now):
    on_tv = client.my_photos()
    wanted = service.wanted_on(t, active)
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
    for p in wanted:
        if p.id in placed:
            continue
        cid = client.upload(service.read_image(p), matte=matte)
        if not cid:
            raise tvlib.TVError("The TV didn't accept a photo")
        Placement.objects.create(photo=p, tv=t, content_id=cid)
        placed[p.id] = cid
        on_tv.add(cid)

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

    # 4. Don't leave a photo that's no longer wanted on screen.
    if ours and client.art_mode():
        # Something we just took off is still on screen. Anything else (their
        # own photo, Samsung art) was picked on the TV; leave it.
        cur = client.current()
        if cur in deleted or (cur.startswith("MY_") and cur not in on_tv):
            client.show(placed[wanted[0].id])

    _mark(t, online=True, last_seen=now, last_sync=now, error="",
          unmanaged=len(on_tv - ours), slideshow=t.slideshow)
    return True
