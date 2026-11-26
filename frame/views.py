import json
import logging

from django.core import signing
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from bdh_sdk.auth import is_admin

from . import discovery, images, service, sync
from .models import FAMILY, GUEST, GuestStay, Photo, Placement

log = logging.getLogger(__name__)

ADMIN = "admin"
#: A phone an admin handed the family-photo QR code to.
FAMILY_LINK = "family"
FAMILY_SALT = "frame.family-link"
FAMILY_LINK_AGE = 30 * 60
MAX_UPLOAD = 25 * 1024 * 1024


def _error(msg, status=400):
    return JsonResponse({"error": str(msg)}, status=status)


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --- who is asking ---------------------------------------------------------------

class Who:
    """``role`` is "admin", "guest" or None; ``stay`` the active guest stay (if any)."""

    def __init__(self, role, stay, secret=None):
        self.role, self.stay, self.secret = role, stay, secret


def _who(request):
    """Someone at a hub screen (through the hub's proxy)."""
    stay = service.active_stay()
    if is_admin(request):
        return Who(ADMIN, stay)
    if stay is not None and service.guest_uploads_on():
        return Who(GUEST, stay)
    return Who(None, stay)


def _who_link(secret):
    """A phone, from the link in a QR code: a guest stay's link (good for that
    stay only) or an admin's family-photo link (good for FAMILY_LINK_AGE)."""
    if ":" in secret:
        try:
            signing.loads(secret, salt=FAMILY_SALT, max_age=FAMILY_LINK_AGE)
        except signing.BadSignature:
            return Who(None, None, secret)
        return Who(FAMILY_LINK, service.active_stay(), secret)
    stay = GuestStay.objects.filter(link_secret=secret, ended_at__isnull=True).first()
    if stay is None or not service.guest_uploads_on():
        return Who(None, None, secret)
    return Who(GUEST, stay, secret)


def _family_link(request):
    token = signing.dumps({"f": 1}, salt=FAMILY_SALT, compress=False)
    return request.build_absolute_uri(reverse("frame_guest", args=[token]))


def _visible(who):
    """Photos ``who`` may see. Family photos are only ever for admins."""
    if who.role == ADMIN:
        return Photo.objects.all()
    if who.role == FAMILY_LINK:
        return Photo.objects.filter(kind=FAMILY)
    if who.role == GUEST:
        return Photo.objects.filter(kind=GUEST, stay=who.stay)
    return Photo.objects.none()


# --- payloads ----------------------------------------------------------------------

def _thumb_url(who, p):
    if who.secret:
        return reverse("frame_guest_thumb", args=[who.secret, p.id])
    return reverse("frame_thumb", args=[p.id])


def _photo(who, p, sent):
    return {"id": p.id, "kind": p.kind, "thumb": _thumb_url(who, p), "all_tvs": p.all_tvs,
            "tvs": [t.id for t in p.tvs.all()], "sent": sorted(sent.get(p.id, ())),
            "added": p.added_at.isoformat(), "portrait": p.height > p.width}


def _tv(t, admin):
    d = {"id": t.id, "name": t.name, "online": t.online, "paired": bool(t.token)}
    if admin:
        d.update(host=t.host, model=t.model, pairing=sync.pair_waiting(t.id), error=t.error,
                 unmanaged=t.unmanaged, last_sync=t.last_sync.isoformat() if t.last_sync else None)
    return d


def _link(request, stay):
    return request.build_absolute_uri(reverse("frame_guest", args=[stay.link_secret]))


def _sent(photos):
    """photo id -> ids of the TVs it is on."""
    out = {}
    for pid, tid in Placement.objects.filter(photo__in=photos).values_list("photo_id", "tv_id"):
        out.setdefault(pid, set()).add(tid)
    return out


def _state(request, who):
    stay = who.stay
    photos = list(_visible(who).prefetch_related("tvs"))
    sent = _sent(photos)
    out = {
        "role": who.role,
        "guest_uploads": service.guest_uploads_on(),
        "tvs": [_tv(t, who.role == ADMIN) for t in discovery.usable_tvs()] if who.role else [],
        "photos": [_photo(who, p, sent) for p in photos],
        "stay": None,
        "limit": service.GUEST_LIMIT if who.role == GUEST else service.FAMILY_LIMIT,
    }
    if stay is not None and who.role is not None:
        ends = service.stay_ends(stay)
        out["stay"] = {
            "guest": stay.guest,
            "checkout": timezone.localtime(stay.checkout, service.tz()).date().isoformat()
            if stay.checkout else None,
            "ends": ends.isoformat() if ends else None,
            "photos": Photo.objects.filter(stay=stay).count(),
        }
        if who.role == ADMIN or (who.role == GUEST and not who.secret):
            out["stay"]["link"] = _link(request, stay)
    if who.role == ADMIN:
        out["settings"] = {
            "interval": str(service.setting("interval", "15")),
            "shuffle": service.flag("shuffle", True),
            "matte": str(service.setting("matte", "none")),
        }
    return out


# --- hub pages -----------------------------------------------------------------------

@require_GET
def index(request):
    return render(request, "frame/index.html", {})


@never_cache
@require_GET
def api_state(request):
    return JsonResponse(_state(request, _who(request)))


@never_cache
@require_GET
def qr(request):
    """QR code for the guests' link, or (``?for=family``, admins) a family-photo link."""
    from bdh_sdk.qr import qr_svg

    who = _who(request)
    if request.GET.get("for") == "family":
        if who.role != ADMIN:
            raise Http404
        url = _family_link(request)
    else:
        if who.role is None or who.stay is None:
            raise Http404
        url = _link(request, who.stay)
    resp = HttpResponse(qr_svg(url), content_type="image/svg+xml")
    resp["Cache-Control"] = "no-store"
    return resp

def _upload(request, who):
    if who.role is None:
        return _error("Uploading isn't open right now", 403)
    f = request.FILES.get("photo")
    if f is None:
        return _error("No photo")
    if f.size > MAX_UPLOAD:
        return _error("That photo is too large")
    tvs = request.POST.get("tvs", "all")
    tv_ids = None if tvs in ("", "all") else tvs.split(",")
    kind = FAMILY if who.role in (ADMIN, FAMILY_LINK) else GUEST
    try:
        p = service.add_photo(f.read(), kind, stay_obj=who.stay, tv_ids=tv_ids)
    except (service.Invalid, images.BadImage) as exc:
        return _error(exc)
    return JsonResponse({"photo": _photo(who, p, _sent([p]))})


def _edit_photo(request, who, photo_id):
    data = _body(request)
    if data is None:
        return _error("Expected a JSON object")
    p = _visible(who).filter(id=photo_id).first()
    if p is None:
        return _error("That photo is gone", 404)
    op = data.get("op")
    if op == "delete":
        service.delete_photos(Photo.objects.filter(id=p.id))
        return JsonResponse({"deleted": p.id})
    if op == "tvs":
        tvs = data.get("tvs")
        try:
            service.set_targets(p, None if tvs in (None, "all") else list(tvs))
        except (service.Invalid, TypeError) as exc:
            return _error(exc)
        return JsonResponse({"photo": _photo(who, p, _sent([p]))})
    return _error("Unknown op")


def _thumb(who, photo_id):
    p = _visible(who).filter(id=photo_id).first()
    if p is None:
        raise Http404
    try:
        with service.storage().open(p.thumb_name, "rb") as f:
            data = f.read()
    except OSError:
        raise Http404 from None
    resp = HttpResponse(data, content_type="image/jpeg")
    resp["Cache-Control"] = "private, max-age=3600"
    return resp


@require_POST
def api_upload(request):
    return _upload(request, _who(request))


@require_POST
def api_photo(request, photo_id):
    return _edit_photo(request, _who(request), photo_id)


@require_GET
def thumb(request, photo_id):
    return _thumb(_who(request), photo_id)


# --- admin: the stay ----------------------------------------------------------------

@require_POST
def api_stay(request):
    if not is_admin(request):
        return _error("Only an admin can end a guest stay", 403)
    data = _body(request)
    if data is None or data.get("op") != "end":
        return _error("Unknown op")
    stay = service.active_stay()
    if stay is None:
        return _error("No guests are staying", 404)
    service.end_stay(stay, timezone.now(), "ended by an admin")
    return JsonResponse({"ended": True})


# --- guest phones (link from the QR code) ---------------------------------------------------

@never_cache
@require_GET
def guest(request, secret):
    who = _who_link(secret)
    if who.role is None:
        return render(request, "frame/guest_over.html", {}, status=404)
    return render(request, "frame/guest.html", {"secret": secret, "family": who.role == FAMILY_LINK})


@never_cache
@require_GET
def guest_state(request, secret):
    who = _who_link(secret)
    if who.role is None:
        return _error("This link has expired", 404)
    return JsonResponse(_state(request, who))


@require_POST
def guest_upload(request, secret):
    who = _who_link(secret)
    if who.role is None:
        return _error("This link has expired", 404)
    return _upload(request, who)


@require_POST
def guest_photo(request, secret, photo_id):
    who = _who_link(secret)
    if who.role is None:
        return _error("This link has expired", 404)
    return _edit_photo(request, who, photo_id)


@require_GET
def guest_thumb(request, secret, photo_id):
    who = _who_link(secret)
    if who.role is None:
        raise Http404
    return _thumb(who, photo_id)
