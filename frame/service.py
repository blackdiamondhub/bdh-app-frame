"""Frame's rules: which photos belong on which TV, and when guest stays end.

Family photos are on the TVs unless guests are staying. While they are, the
TVs show only that stay's guest photos; family photos are deleted from the
TVs (not just hidden, so guests can't find them in the TV's Art menu) and go
back up after the stay. A guest stay ends at midnight after its checkout
date (checkout times aren't strict), when the owners arrive, when different
guests arrive, or when an admin ends it. Guest photos are then deleted from
the hub and the TVs for good.
"""
import logging
import threading
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from bdh_sdk.settings import app_setting, as_bool
from bdh_sdk.storage import app_media

from . import images, stay
from .models import FAMILY, GUEST, GuestStay, Photo

log = logging.getLogger(__name__)

APP = "frame"
#: Most photos one stay's guests can add (each is uploaded to every TV).
GUEST_LIMIT = 60
FAMILY_LIMIT = 500


class Invalid(ValueError):
    pass


def storage():
    return app_media(APP)


def tz():
    try:
        from bdh_sdk.location import tzinfo

        return tzinfo()
    except Exception:  # noqa: BLE001
        return timezone.get_default_timezone()


def setting(key, default):
    return app_setting(APP, key, default)


def flag(key, default):
    try:
        return as_bool(setting(key, default))
    except ValueError:
        return default


def guest_uploads_on():
    return flag("guest_uploads", True)


# --- guest stays ---------------------------------------------------------------

def stay_ends(gs):
    """Midnight (lodge time) after the stay's checkout date, or None if unknown."""
    if gs.checkout is None:
        return None
    day = timezone.localtime(gs.checkout, tz()).date()
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz())


def active_stay():
    return GuestStay.objects.filter(ended_at__isnull=True).first()


_eval_lock = threading.Lock()


def evaluate(now=None):
    """Bring Frame's guest stay in line with the hub's; returns the active one.

    If the hub can't be asked, nothing changes (a stay never ends early because
    the hub was restarting).
    """
    now = now or timezone.now()
    with _eval_lock:
        active = active_stay()
        try:
            cur = stay.current()
        except stay.Unavailable as exc:
            log.info("Couldn't ask the hub who is staying: %s", exc)
            if active is not None and (ends := stay_ends(active)) is not None and now >= ends:
                end_stay(active, now, "checkout passed")
                return None
            return active
        if cur is not None and not cur.owner:
            if active is not None and active.key != cur.key:
                end_stay(active, now, "new guests arrived")
                active = None
            if active is None:
                if GuestStay.objects.filter(key=cur.key).exists():
                    return None  # an admin already ended this one
                active = GuestStay.objects.create(key=cur.key, guest=cur.guest, checkout=cur.checkout)
                log.info("Guest stay started: %s", active)
            elif (active.guest, active.checkout) != (cur.guest, cur.checkout):
                active.guest, active.checkout = cur.guest, cur.checkout
                active.save(update_fields=["guest", "checkout"])
            return active
        if active is None:
            return None
        if cur is not None and cur.owner:
            end_stay(active, now, "owners arrived")
            return None
        ends = stay_ends(active)
        if ends is None or now >= ends:
            end_stay(active, now, "checkout passed")
            return None
        return active


def end_stay(gs, now=None, why="ended"):
    """End ``gs`` and delete its guests' photos for good."""
    gs.ended_at = now or timezone.now()
    gs.save(update_fields=["ended_at"])
    n = delete_photos(Photo.objects.filter(stay=gs))
    log.info("Guest stay %s ended (%s); deleted %d guest photos", gs, why, n)
    wake_worker()


# --- photos ----------------------------------------------------------------------

def add_photo(data, kind, stay_obj=None, tv_ids=None):
    """Store an uploaded photo; ``tv_ids`` None means all TVs."""
    if kind == GUEST:
        if stay_obj is None:
            raise Invalid("Guest photos need a stay")
        if Photo.objects.filter(stay=stay_obj).count() >= GUEST_LIMIT:
            raise Invalid(f"That's the most photos for one stay ({GUEST_LIMIT}); remove some first")
    elif Photo.objects.filter(kind=FAMILY).count() >= FAMILY_LIMIT:
        raise Invalid(f"Frame holds up to {FAMILY_LIMIT} family photos")
    big, thumb, w, h = images.prepare(data)
    tvs = _tvs(tv_ids)
    st = storage()
    with transaction.atomic():
        p = Photo.objects.create(kind=kind, stay=stay_obj if kind == GUEST else None,
                                 width=w, height=h, all_tvs=tvs is None)
        if tvs is not None:
            p.tvs.set(tvs)
        _save(st, p.image_name, big)
        _save(st, p.thumb_name, thumb)
    wake_worker()
    return p


def _save(st, name, data):
    from django.core.files.base import ContentFile

    if st.exists(name):
        st.delete(name)
    st.save(name, ContentFile(data))


def _tvs(tv_ids):
    if tv_ids is None:
        return None
    from .discovery import usable_tvs

    ids = {int(i) for i in tv_ids if str(i).isdigit()}
    tvs = [t for t in usable_tvs() if t.id in ids]
    if not tvs:
        raise Invalid("Pick at least one TV")
    return tvs


def set_targets(photo, tv_ids):
    tvs = _tvs(tv_ids)
    photo.all_tvs = tvs is None
    photo.save(update_fields=["all_tvs"])
    photo.tvs.set(tvs or [])
    wake_worker()


def delete_photos(qs):
    """Delete photos and their files; the worker takes them off the TVs."""
    st = storage()
    n = 0
    for p in list(qs):
        for name in (p.image_name, p.thumb_name):
            try:
                st.delete(name)
            except OSError:
                log.warning("Couldn't delete %s", name)
        p.delete()  # placements stay (photo=None) until the worker clears the TV
        n += 1
    if n:
        wake_worker()
    return n


def read_image(photo):
    with storage().open(photo.image_name, "rb") as f:
        return f.read()


# --- what goes where ---------------------------------------------------------------

def showing(active=None):
    """The photos the TVs should show now: this stay's guest photos, else family photos."""
    if active is not None:
        return Photo.objects.filter(kind=GUEST, stay=active)
    return Photo.objects.filter(kind=FAMILY)


def wanted_on(tv, active=None):
    """Photos that belong on ``tv`` right now, oldest first."""
    from django.db.models import Q

    return list(showing(active).filter(Q(all_tvs=True) | Q(tvs=tv)).distinct().order_by("added_at"))


def slideshow_value(has_photos):
    if not has_photos:
        return "off"
    try:
        minutes = int(setting("interval", "15"))
    except (TypeError, ValueError):
        minutes = 15
    return f"{minutes}:{1 if flag('shuffle', True) else 0}"


# --- worker hook -------------------------------------------------------------------

_wake = None


def set_waker(fn):
    global _wake
    _wake = fn


def wake_worker():
    if _wake is not None:
        _wake()
