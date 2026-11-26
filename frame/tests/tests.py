import io
import json
import pathlib
import shutil
import tempfile
from datetime import datetime, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from bdh_sdk.testing import assert_plugin_importable
from frame import images, service, stay, sync, tv as tvlib, views
from frame.models import FAMILY, GUEST, TV, GuestStay, Photo, Placement

ROOT = pathlib.Path(__file__).resolve().parent.parent
UTC = dt_timezone.utc


def jpeg(w=800, h=600, color=(200, 100, 50), exif_rotate=False):
    img = Image.new("RGB", (w, h), color)
    out = io.BytesIO()
    if exif_rotate:
        ex = Image.Exif()
        ex[0x0112] = 6  # rotate 90 CW on display
        img.save(out, "JPEG", exif=ex.tobytes())
    else:
        img.save(out, "JPEG")
    return out.getvalue()


class FakeTV:
    """A Frame's art channel, shared by every client for that host."""

    tvs = {}

    def __init__(self, host, token):
        self.host = host
        self.tv = FakeTV.tvs[host]

    def my_photos(self):
        self.tv["log"].append("list")
        if self.tv.get("fail"):
            raise tvlib.TVError(self.tv["fail"])
        return set(self.tv["photos"])

    def upload(self, data, matte="none"):
        Image.open(io.BytesIO(data)).verify()
        self.tv["n"] += 1
        cid = f"MY_F{self.tv['n']:04d}"
        self.tv["photos"].append(cid)
        self.tv["log"].append(("upload", cid))
        return cid

    def delete(self, ids):
        ids = list(ids)
        self.tv["photos"] = [c for c in self.tv["photos"] if c not in ids]
        self.tv["log"].append(("delete", tuple(sorted(ids))))

    def current(self):
        return self.tv["current"]

    def art_mode(self):
        return True

    def show(self, cid):
        self.tv["current"] = cid
        self.tv["log"].append(("show", cid))

    def set_slideshow(self, minutes, shuffle):
        self.tv["slideshow"] = (minutes, shuffle)
        self.tv["log"].append(("slideshow", minutes, shuffle))

    def close(self):
        pass


def make_fake_tv(host, photos=("MY_F0001",), current="MY_F0001"):
    FakeTV.tvs[host] = {"photos": list(photos), "current": current, "log": [], "n": 100, "slideshow": None}
    return FakeTV.tvs[host]


class InlineThread:
    """Runs a background thread's work straight away (tests share one DB connection)."""

    def __init__(self, target, name=None, daemon=None):
        self.target = target

    def start(self):
        self.target()


def cur(key="k1", guest="Henderson family", checkout=None, owner=False):
    return stay.Current(key=key, guest=guest, checkout=checkout or datetime(2026, 11, 29, 11, tzinfo=UTC),
                        owner=owner)


class Base(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        o = override_settings(MEDIA_ROOT=self.media)
        o.enable()
        self.addCleanup(o.disable)
        FakeTV.tvs = {}
        tvlib.set_factory(FakeTV, pairer=lambda host: "tok-" + host)
        self.addCleanup(tvlib.set_factory)
        p = patch("frame.tv.describe", side_effect=lambda host: {"name": "Living Room", "model": "QN55LS03",
                                                                  "frame": True, "on": host in FakeTV.tvs})
        p.start()
        self.addCleanup(p.stop)
        self.hub_stay = None
        p2 = patch("frame.stay.current", side_effect=lambda: self.hub_stay)
        p2.start()
        self.addCleanup(p2.stop)

    def tv(self, host="192.168.11.138", **kw):
        make_fake_tv(host, **kw)
        return TV.objects.create(name="Living Room", host=host, token="tok")

    def family(self, n=1, **kw):
        return [service.add_photo(jpeg(), FAMILY, **kw) for _ in range(n)]


class StayTests(Base):
    def test_guest_arrival_and_midnight_after_checkout(self):
        self.hub_stay = cur()
        gs = service.evaluate(now=datetime(2026, 11, 26, 18, tzinfo=UTC))
        self.assertEqual((gs.key, gs.guest), ("k1", "Henderson family"))
        service.add_photo(jpeg(), GUEST, stay_obj=gs)
        # The hub drops the stay at checkout; Frame keeps it until midnight (lodge time, UTC-7).
        self.hub_stay = None
        with patch("frame.service.tz", return_value=timezone.get_fixed_timezone(-7 * 60)):
            self.assertIsNotNone(service.evaluate(now=datetime(2026, 11, 30, 6, 59, tzinfo=UTC)))
            self.assertIsNone(service.evaluate(now=datetime(2026, 11, 30, 7, 0, tzinfo=UTC)))
        self.assertFalse(Photo.objects.filter(kind=GUEST).exists())
        self.assertEqual(list(service.storage().listdir("photos")[1]), [])

    def test_owners_arriving_end_the_stay(self):
        self.hub_stay = cur()
        gs = service.evaluate()
        service.add_photo(jpeg(), GUEST, stay_obj=gs)
        self.hub_stay = cur(key="o1", owner=True)
        self.assertIsNone(service.evaluate())
        self.assertFalse(Photo.objects.exists())

    def test_back_to_back_stays_purge_the_first(self):
        self.hub_stay = cur()
        first = service.evaluate()
        service.add_photo(jpeg(), GUEST, stay_obj=first)
        self.hub_stay = cur(key="k2", guest="Smiths")
        second = service.evaluate()
        self.assertNotEqual(first.id, second.id)
        self.assertFalse(Photo.objects.filter(stay=first).exists())
        self.assertNotEqual(first.link_secret, second.link_secret)

    def test_admin_ended_stay_stays_ended(self):
        self.hub_stay = cur()
        service.end_stay(service.evaluate())
        self.assertIsNone(service.evaluate())
        self.assertEqual(GuestStay.objects.count(), 1)

    def test_hub_unreachable_keeps_the_stay(self):
        self.hub_stay = cur()
        service.evaluate()
        with patch("frame.stay.current", side_effect=stay.Unavailable("down")):
            self.assertIsNotNone(service.evaluate(now=datetime(2026, 11, 27, tzinfo=UTC)))
            # ...but not past midnight after checkout.
            self.assertIsNone(service.evaluate(now=datetime(2026, 12, 1, tzinfo=UTC)))


class SyncTests(Base):
    def test_family_photos_go_up_and_never_touch_unmanaged(self):
        t = self.tv()
        self.family(2)
        self.assertTrue(sync.sync_tv(t))
        fake = FakeTV.tvs[t.host]
        self.assertIn("MY_F0001", fake["photos"])
        self.assertEqual(len(fake["photos"]), 3)
        t.refresh_from_db()
        self.assertEqual((t.unmanaged, t.error), (1, ""))
        self.assertEqual(fake["slideshow"], (15, True))
        # Second sync is a no-op.
        fake["log"].clear()
        sync.sync_tv(t)
        self.assertEqual(fake["log"], ["list"])

    def test_guests_arriving_delete_family_photos_before_uploading(self):
        t = self.tv()
        self.family(2)
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        fake["current"] = Placement.objects.first().content_id
        family_ids = set(Placement.objects.values_list("content_id", flat=True))
        self.hub_stay = cur()
        gs = service.evaluate()
        service.add_photo(jpeg(), GUEST, stay_obj=gs)
        fake["log"].clear()
        sync.sync_tv(t, gs)
        kinds = [e[0] for e in fake["log"] if isinstance(e, tuple)]
        self.assertEqual(kinds[0], "delete")
        self.assertEqual(set(fake["log"][1][1]), family_ids)
        self.assertLess(kinds.index("delete"), kinds.index("upload"))
        self.assertFalse(family_ids & set(fake["photos"]))
        self.assertIn("MY_F0001", fake["photos"])
        self.assertNotIn(fake["current"], family_ids)

    def test_no_guest_photos_means_slideshow_off_and_no_family(self):
        t = self.tv()
        self.family(1)
        sync.sync_tv(t)
        self.hub_stay = cur()
        gs = service.evaluate()
        sync.sync_tv(t, gs)
        fake = FakeTV.tvs[t.host]
        self.assertEqual(fake["photos"], ["MY_F0001"])
        self.assertEqual(fake["slideshow"], (0, False))

    def test_targets(self):
        a, b = self.tv("10.0.0.1"), self.tv("10.0.0.2")
        p = service.add_photo(jpeg(), FAMILY, tv_ids=[str(b.id)])
        sync.sync_all()
        self.assertEqual(len(FakeTV.tvs["10.0.0.1"]["photos"]), 1)
        self.assertEqual(len(FakeTV.tvs["10.0.0.2"]["photos"]), 2)
        service.set_targets(p, None)
        sync.sync_all()
        self.assertEqual(len(FakeTV.tvs["10.0.0.1"]["photos"]), 2)

    def test_deleted_photo_comes_off(self):
        t = self.tv()
        (p,) = self.family(1)
        sync.sync_tv(t)
        service.delete_photos(Photo.objects.filter(id=p.id))
        sync.sync_tv(t)
        self.assertEqual(FakeTV.tvs[t.host]["photos"], ["MY_F0001"])
        self.assertFalse(Placement.objects.exists())

    def test_off_or_unpaired_tv(self):
        off = TV.objects.create(name="Off", host="10.9.9.9", token="tok")
        self.assertFalse(sync.sync_tv(off))
        off.refresh_from_db()
        self.assertFalse(off.online)
        make_fake_tv("10.0.0.5")
        new = TV.objects.create(name="New", host="10.0.0.5")
        self.assertFalse(sync.sync_tv(new))
        new.refresh_from_db()
        self.assertEqual(new.error, "Not paired")

    def test_tv_error_is_recorded(self):
        t = self.tv()
        FakeTV.tvs[t.host]["fail"] = "socket closed"
        self.assertFalse(sync.sync_tv(t))
        t.refresh_from_db()
        self.assertEqual(t.error, "socket closed")


class PrivacyTests(Base):
    """Family photos are for admins only; guests see only their own stay's photos."""

    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("stefan", password="x", is_staff=True)
        self.c = Client()
        (self.fam,) = self.family(1)
        self.hub_stay = cur()
        self.gs = service.evaluate()
        self.guest_photo = service.add_photo(jpeg(), GUEST, stay_obj=self.gs)

    def state(self, url=None):
        return self.c.get(url or reverse("frame_state")).json()

    def test_hub_guest_never_sees_family(self):
        s = self.state()
        self.assertEqual(s["role"], "guest")
        self.assertEqual([p["id"] for p in s["photos"]], [self.guest_photo.id])
        self.assertEqual(self.c.get(reverse("frame_thumb", args=[self.fam.id])).status_code, 404)
        self.assertEqual(self.c.get(reverse("frame_thumb", args=[self.guest_photo.id])).status_code, 200)
        r = self.c.post(reverse("frame_photo", args=[self.fam.id]), json.dumps({"op": "delete"}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Photo.objects.filter(id=self.fam.id).exists())
        self.assertNotIn("host", s["tvs"][0] if s["tvs"] else {})

    def test_nobody_sees_anything_without_a_stay(self):
        service.end_stay(self.gs)
        s = self.state()
        self.assertIsNone(s["role"])
        self.assertEqual(s["photos"], [])
        self.assertEqual(self.c.get(reverse("frame_thumb", args=[self.fam.id])).status_code, 404)
        r = self.c.post(reverse("frame_upload"), {"photo": io.BytesIO(jpeg())})
        self.assertEqual(r.status_code, 403)

    def test_guest_link(self):
        url = reverse("frame_guest_state", args=[self.gs.link_secret])
        s = self.state(url)
        self.assertEqual([p["id"] for p in s["photos"]], [self.guest_photo.id])
        self.assertNotIn("link", s["stay"])
        self.assertEqual(self.c.get(reverse("frame_guest_thumb", args=[self.gs.link_secret, self.fam.id])).status_code, 404)
        f = io.BytesIO(jpeg())
        f.name = "x.jpg"
        r = self.c.post(reverse("frame_guest_upload", args=[self.gs.link_secret]), {"photo": f, "tvs": "all"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Photo.objects.get(id=r.json()["photo"]["id"]).kind, GUEST)

    def test_link_dies_with_the_stay(self):
        secret = self.gs.link_secret
        service.end_stay(self.gs)
        r = self.c.get(reverse("frame_guest", args=[secret]))
        self.assertEqual(r.status_code, 404)
        self.assertContains(r, "Thanks for staying", status_code=404)
        self.assertEqual(self.c.get(reverse("frame_guest_state", args=[secret])).status_code, 404)
        self.assertEqual(self.c.get(reverse("frame_guest", args=["nope"])).status_code, 404)

    def test_guest_uploads_off(self):
        with patch("frame.service.guest_uploads_on", return_value=False):
            self.assertIsNone(self.state()["role"])
            self.assertEqual(self.c.get(reverse("frame_guest_state", args=[self.gs.link_secret])).status_code, 404)

    def test_admin_sees_all_and_manages_tvs(self):
        self.c.force_login(self.admin)
        s = self.state()
        self.assertEqual(s["role"], "admin")
        self.assertEqual({p["id"] for p in s["photos"]}, {self.fam.id, self.guest_photo.id})
        make_fake_tv("192.168.11.138")
        with patch("frame.views.threading.Thread", InlineThread), patch("frame.views._close_db"):
            r = self.c.post(reverse("frame_tvs"), json.dumps({"op": "add", "host": "192.168.11.138"}),
                            content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(TV.objects.get(host="192.168.11.138").token, "tok-192.168.11.138")
        self.assertEqual(self.c.get(reverse("frame_qr") + "?for=family").status_code, 200)

    def test_non_admin_cant_touch_tvs_or_stay(self):
        for url, body in ((reverse("frame_tvs"), {"op": "add", "host": "1.2.3.4"}),
                          (reverse("frame_stay"), {"op": "end"})):
            r = self.c.post(url, json.dumps(body), content_type="application/json")
            self.assertEqual(r.status_code, 403)
        self.assertEqual(self.c.get(reverse("frame_qr") + "?for=family").status_code, 404)

    def test_family_link(self):
        token = signing.dumps({"f": 1}, salt=views.FAMILY_SALT, compress=False)
        s = self.state(reverse("frame_guest_state", args=[token]))
        self.assertEqual([p["id"] for p in s["photos"]], [self.fam.id])
        with patch("django.core.signing.time.time", return_value=__import__("time").time() + views.FAMILY_LINK_AGE + 5):
            self.assertEqual(self.c.get(reverse("frame_guest_state", args=[token])).status_code, 404)
        forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
        self.assertEqual(self.c.get(reverse("frame_guest_state", args=[forged])).status_code, 404)

    def test_admin_ends_stay(self):
        self.c.force_login(self.admin)
        r = self.c.post(reverse("frame_stay"), json.dumps({"op": "end"}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Photo.objects.filter(kind=GUEST).exists())


class ImageTests(SimpleTestCase):
    def test_big_landscape_shrinks_to_4k(self):
        big, thumb, w, h = images.prepare(jpeg(6000, 4000))
        self.assertEqual((w, h), (3240, 2160))
        self.assertEqual(Image.open(io.BytesIO(big)).size, (3240, 2160))
        self.assertLessEqual(max(Image.open(io.BytesIO(thumb)).size), 480)

    def test_portrait_and_exif(self):
        _, _, w, h = images.prepare(jpeg(4000, 3000, exif_rotate=True))
        self.assertEqual((w, h), (2160, 2880))

    def test_small_png_kept(self):
        out = io.BytesIO()
        Image.new("RGBA", (300, 200)).save(out, "PNG")
        self.assertEqual(images.prepare(out.getvalue())[2:], (300, 200))

    def test_garbage(self):
        with self.assertRaises(images.BadImage):
            images.prepare(b"not a photo")


class PluginTests(SimpleTestCase):
    def test_plugin_importable(self):
        assert_plugin_importable("frame.plugin")

    def test_manifest(self):
        from frame.plugin import spec
        self.assertEqual((spec.network, spec.lifecycle), ("lan", "always"))
        self.assertIn("stays.read", spec.permissions)
        self.assertTrue((ROOT / "static" / spec.tiles[0].icon).exists())
        self.assertEqual(reverse("frame_state"), "/frame/api/state/")

    def test_templates_escape_and_never_inline_family_photos(self):
        js = (ROOT / "static" / "frame" / "guest.js").read_text()
        self.assertNotIn("family\"", js.replace("family ", ""))  # phone page gets photos only from the API
