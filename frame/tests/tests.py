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

from bdh_sdk.storage import set_state_backend
from bdh_sdk.testing import assert_plugin_importable
from frame import discovery, images, service, stay, sync, tv as tvlib, views, worker
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

    def upload(self, data):
        # The TV only ever gets exactly 16:9 at panel size, with no matte: a matted
        # non-16:9 photo crashes the real TV's art app (error 40000).
        img = Image.open(io.BytesIO(data))
        assert img.size == images.TV_SIZE, f"sent {img.size}, not the panel's 16:9"
        img.verify()
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
        return self.tv.get("art", True)

    def show(self, cid):
        self.tv["current"] = cid
        self.tv["log"].append(("show", cid))

    def set_slideshow(self, minutes, shuffle):
        self.tv["slideshow"] = (minutes, shuffle)
        self.tv["log"].append(("slideshow", minutes, shuffle))

    def slideshow(self):
        self.tv["log"].append("status")
        s = self.tv["slideshow"]
        return "off" if not s or not s[0] else f"{s[0]}:{1 if s[1] else 0}"

    def close(self):
        pass


def make_fake_tv(host, photos=("MY_F0001",), current="MY_F0001"):
    FakeTV.tvs[host] = {"photos": list(photos), "current": current, "log": [], "n": 100, "slideshow": None}
    return FakeTV.tvs[host]


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
        p = patch("frame.tv.describe", side_effect=lambda host: {"uid": "uuid-" + host, "name": "Living Room",
                                                                  "model": "QN55LS03", "frame": True,
                                                                  "on": host in FakeTV.tvs})
        p.start()
        self.addCleanup(p.stop)
        self.hub_stay = None
        p2 = patch("frame.stay.current", side_effect=lambda: self.hub_stay)
        p2.start()
        self.addCleanup(p2.stop)
        self.settings = {}
        real_setting = service.setting
        p3 = patch("frame.service.setting", side_effect=lambda k, d: self.settings.get(k, real_setting(k, d)))
        p3.start()
        self.addCleanup(p3.stop)
        sync._pair_tries.clear()
        sync._pair_status.clear()
        sync._synced.clear()
        sync._restart.clear()
        p_re = patch("frame.sync.RECHECK", 0)  # every sync visits the TV unless a test says otherwise
        p_re.start()
        self.addCleanup(p_re.stop)
        set_state_backend(None)
        self.addCleanup(set_state_backend, None)
        service._last_interval = None
        p4 = patch("frame.sync._spawn", side_effect=lambda fn, *args: fn(*args))  # pair inline
        p4.start()
        self.addCleanup(p4.stop)
        p5 = patch("frame.sync._close_db")
        p5.start()
        self.addCleanup(p5.stop)

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
        # Second sync only looks.
        fake["log"].clear()
        sync.sync_tv(t)
        self.assertEqual(fake["log"], ["list", "status"])

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
        self.tv("10.0.0.1")
        b = self.tv("10.0.0.2")
        p = service.add_photo(jpeg(), FAMILY, tv_ids=[str(b.id)])
        sync.sync_all()
        self.assertEqual(len(FakeTV.tvs["10.0.0.1"]["photos"]), 1)
        self.assertEqual(len(FakeTV.tvs["10.0.0.2"]["photos"]), 2)
        service.set_targets(p, None)
        sync.sync_all()
        self.assertEqual(len(FakeTV.tvs["10.0.0.1"]["photos"]), 2)

    def test_photo_deleted_on_the_tv_stays_off_that_tv(self):
        """Deleting one of Frame's photos with the remote must not make Frame send it back."""
        a, b = self.tv("10.0.0.1"), self.tv("10.0.0.2")
        p, q = self.family(2)
        sync.sync_all()
        cid = Placement.objects.get(tv=a, photo=p).content_id
        FakeTV.tvs[a.host]["photos"].remove(cid)  # deleted on the TV
        sync.sync_all()
        sync.sync_all()
        self.assertNotIn(cid, FakeTV.tvs[a.host]["photos"])
        self.assertEqual(len(FakeTV.tvs[a.host]["photos"]), 2)  # q + the TV's own photo
        self.assertEqual(len(FakeTV.tvs[b.host]["photos"]), 3)  # still on the other TV
        p.refresh_from_db()
        self.assertFalse(p.all_tvs)
        self.assertEqual(list(p.tvs.all()), [b])
        service.set_targets(p, None)  # put back on every TV from the app
        sync.sync_all()
        self.assertEqual(len(FakeTV.tvs[a.host]["photos"]), 3)

    def test_photos_sent_before_framing_are_sent_again(self):
        """Photos uploaded by older versions (maybe with a matte) are replaced by framed ones."""
        t = self.tv(photos=("MY_F0001", "MY_F0050"), current="MY_F0050")
        old, = self.family(1)
        Placement.objects.create(photo=old, tv=t, content_id="MY_F0050")  # framed=False
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        self.assertNotIn("MY_F0050", fake["photos"])
        pl = Placement.objects.get(tv=t)
        self.assertTrue(pl.framed)
        self.assertIn(pl.content_id, fake["photos"])
        self.assertEqual(fake["current"], pl.content_id)
        fake["log"].clear()
        sync.sync_tv(t)
        self.assertEqual(fake["log"], ["list", "status"])

    def test_reset_tv_gets_everything_back(self):
        t = self.tv()
        self.family(3)
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        fake["photos"] = ["MY_F0001"]  # every Frame photo gone at once: a reset, not a choice
        sync.sync_tv(t)
        self.assertEqual(len(fake["photos"]), 4)
        self.assertTrue(all(p.all_tvs for p in Photo.objects.all()))

    def test_new_token_from_the_tv_is_saved(self):
        """After Allow the TV can hand out a new token; using the old one makes it ask again."""
        t = self.tv()
        self.family(1)

        class Rotating(FakeTV):
            new_token = "tok-2"
        tvlib.set_factory(Rotating)
        sync.sync_tv(t)
        t.refresh_from_db()
        self.assertEqual(t.token, "tok-2")

    def test_deleted_photo_comes_off(self):
        t = self.tv()
        (p,) = self.family(1)
        sync.sync_tv(t)
        service.delete_photos(Photo.objects.filter(id=p.id))
        sync.sync_tv(t)
        self.assertEqual(FakeTV.tvs[t.host]["photos"], ["MY_F0001"])
        self.assertFalse(Placement.objects.exists())

    def test_new_photo_goes_on_screen_right_away(self):
        t = self.tv()
        self.family(1)
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        fake["current"] = "MY_F0001"  # the slideshow moved on to their own photo
        (p,) = self.family(1)
        sync.sync_tv(t)
        self.assertEqual(fake["current"], Placement.objects.get(photo=p).content_id)
        # Nothing new: the slideshow is left alone.
        fake["current"] = "MY_F0001"
        sync.sync_tv(t)
        self.assertEqual(fake["current"], "MY_F0001")

    def test_off_or_unpaired_tv(self):
        off = TV.objects.create(name="Off", host="10.9.9.9", token="tok")
        self.assertFalse(sync.sync_tv(off))
        off.refresh_from_db()
        self.assertFalse(off.online)
        make_fake_tv("10.0.0.5")
        new = TV.objects.create(name="New", host="10.0.0.5")
        self.assertFalse(sync.sync_tv(new))
        new.refresh_from_db()
        # Frame asked the TV to let it in (someone pressed Allow), and doesn't nag it again soon.
        self.assertEqual((new.token, new.error), ("tok-10.0.0.5", ""))
        self.assertFalse(sync.maybe_pair(new))

    def test_slideshow_is_sent_again_after_showing_a_new_photo(self):
        """Picking a photo can stop the TV's slideshow, so it must be restarted every time."""
        t = self.tv()
        self.family(1)
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        fake["log"].clear()
        self.family(1)
        sync.sync_tv(t)
        kinds = [e[0] if isinstance(e, tuple) else e for e in fake["log"]]
        self.assertLess(kinds.index("show"), kinds.index("slideshow"))
        self.assertEqual(fake["slideshow"], (15, True))

    def test_start_slideshow_button(self):
        """The TV sat in the Art Store with the slideshow already set and Art Mode reporting off
        (seen at Sun Peaks): the button sends the slideshow again, even on an unchanged TV."""
        t = self.tv()
        self.family(2)
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        fake["log"].clear()
        sync.sync_tv(t)
        self.assertNotIn("show", [e[0] for e in fake["log"] if isinstance(e, tuple)])  # nothing to do
        fake["art"] = False  # browsing the Art Store
        sync.restart_slideshows()
        sync.sync_tv(t)
        self.assertEqual([e for e in fake["log"] if isinstance(e, tuple)], [("slideshow", 15, True)])
        fake["log"].clear()
        sync.sync_tv(t)  # done once, not on every pass
        self.assertEqual([e for e in fake["log"] if isinstance(e, tuple)], [])

    def test_slideshow_the_tv_lost_is_restored(self):
        """A reboot (or someone in the TV's menus) turns the slideshow off; the next visit puts it back."""
        t = self.tv()
        self.family(2)
        sync.sync_tv(t)
        fake = FakeTV.tvs[t.host]
        fake["slideshow"] = None
        sync.sync_tv(t)
        self.assertEqual(fake["slideshow"], (15, True))
        fake["slideshow"] = (60, False)
        sync.sync_tv(t)
        self.assertEqual(fake["slideshow"], (15, True))

    def test_interval_change_reaches_the_tv(self):
        t = self.tv()
        self.family(2)
        sync.sync_tv(t)
        service.set_interval("3")
        sync.sync_tv(t)
        self.assertEqual(FakeTV.tvs[t.host]["slideshow"], (3, True))

    def test_unchanged_tv_is_not_visited_every_minute(self):
        """Every visit opens a connection the TV has to handle; leave it be when nothing changed."""
        t = self.tv()
        self.family(1)
        with patch("frame.sync.RECHECK", 600):
            sync.sync_tv(t)
            fake = FakeTV.tvs[t.host]
            fake["log"].clear()
            self.assertTrue(sync.sync_tv(t))
            self.assertEqual(fake["log"], [])
            self.family(1)  # something changed: visit now
            sync.sync_tv(t)
            self.assertIn("list", fake["log"])
            fake["log"].clear()
            service.set_interval("60")
            sync.sync_tv(t)
            self.assertEqual(fake["slideshow"], (60, True))
            # Turned off (or the power cut): everything is checked when it's back.
            fake["log"].clear()
            saved = FakeTV.tvs.pop(t.host)
            sync.sync_tv(t)
            FakeTV.tvs[t.host] = saved
            sync.sync_tv(t)
            self.assertIn("status", fake["log"])

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
        self.assertEqual(self.c.get(reverse("frame_image", args=[self.fam.id])).status_code, 404)
        r = self.c.get(s["photos"][0]["image"])
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/jpeg"))
        r = self.c.post(reverse("frame_photo", args=[self.fam.id]), json.dumps({"op": "delete"}),
                        content_type="application/json")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(Photo.objects.filter(id=self.fam.id).exists())
        self.assertNotIn("host", s["tvs"][0] if s["tvs"] else {})

    def test_unpaired_tv_is_not_in_the_app(self):
        living = self.tv()
        office = TV.objects.create(name="Office television", host="192.168.10.124", uid="uuid-o")
        self.assertEqual([t["id"] for t in self.state()["tvs"]], [living.id])
        with self.assertRaises(service.Invalid):
            service.add_photo(jpeg(), FAMILY, tv_ids=[office.id])
        office.token = "tok"
        office.save()
        self.assertEqual(sorted(t["id"] for t in self.state()["tvs"]), [living.id, office.id])

    def test_tvs_with_the_power_cut(self):
        """Between stays the TVs have no power: nothing answers, nothing breaks, nothing is forgotten."""
        living = self.tv()
        sync.sync_all()
        self.assertTrue(Placement.objects.filter(tv=living).exists())
        FakeTV.tvs.clear()
        with patch("frame.discovery.local_subnet", return_value=__import__("ipaddress").ip_network("192.168.11.0/30")):
            self.assertEqual(discovery.discover(), [])
        sync.sync_all()
        living.refresh_from_db()
        self.assertEqual((living.token, living.online), ("tok", False))
        self.assertTrue(Placement.objects.filter(tv=living).exists())
        s = self.state()
        self.assertEqual([(t["id"], t["online"]) for t in s["tvs"]], [(living.id, False)])
        f = io.BytesIO(jpeg())
        f.name = "x.jpg"
        self.assertEqual(self.c.post(reverse("frame_upload"), {"photo": f, "tvs": "all"}).status_code, 200)

    def test_nobody_sees_anything_without_a_stay(self):
        service.end_stay(self.gs)
        s = self.state()
        self.assertIsNone(s["role"])
        self.assertEqual(s["photos"], [])
        self.assertEqual(self.c.get(reverse("frame_thumb", args=[self.fam.id])).status_code, 404)
        self.assertEqual(self.c.get(reverse("frame_image", args=[self.fam.id])).status_code, 404)
        r = self.c.post(reverse("frame_upload"), {"photo": io.BytesIO(jpeg())})
        self.assertEqual(r.status_code, 403)

    def test_guest_link(self):
        url = reverse("frame_guest_state", args=[self.gs.link_secret])
        s = self.state(url)
        self.assertEqual([p["id"] for p in s["photos"]], [self.guest_photo.id])
        self.assertNotIn("link", s["stay"])
        self.assertEqual(self.c.get(reverse("frame_guest_thumb", args=[self.gs.link_secret, self.fam.id])).status_code, 404)
        self.assertEqual(self.c.get(reverse("frame_guest_image", args=[self.gs.link_secret, self.fam.id])).status_code, 404)
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

    def test_admin_sees_all(self):
        self.c.force_login(self.admin)
        s = self.state()
        self.assertEqual(s["role"], "admin")
        self.assertEqual({p["id"] for p in s["photos"]}, {self.fam.id, self.guest_photo.id})
        self.assertEqual(self.c.get(reverse("frame_qr") + "?for=family").status_code, 200)

    def test_non_admin_cant_end_stay(self):
        r = self.c.post(reverse("frame_stay"), json.dumps({"op": "end"}), content_type="application/json")
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


class DiscoveryTests(Base):
    """TVs are found on the network; admins exclude them in the hub's Settings, not in the app."""

    def found(self, host, uid=None, name="Office television"):
        return {"uid": uid or "uuid-" + host, "host": host, "name": name, "model": "QN65LS03", "frame": True}

    def test_new_tv_is_added_and_a_moved_tv_keeps_its_row(self):
        (t,) = discovery.remember([self.found("192.168.10.124")])
        self.assertEqual((t.uid, t.name, t.token), ("uuid-192.168.10.124", "Office television", ""))
        self.assertEqual(discovery.remember([self.found("192.168.10.150", uid=t.uid)]), [])
        t.refresh_from_db()
        self.assertEqual(t.host, "192.168.10.150")
        self.assertEqual(TV.objects.count(), 1)

    def test_tv_added_before_discovery_gets_its_uid(self):
        old = self.tv("192.168.11.138")
        self.assertEqual(discovery.remember([self.found("192.168.11.138", name="Living Room Television")]), [])
        old.refresh_from_db()
        self.assertEqual((old.uid, old.name, old.token), ("uuid-192.168.11.138", "Living Room", "tok"))

    def test_another_tv_taking_an_address(self):
        a = self.tv("10.0.0.5")
        a.uid = "uuid-a"
        a.save()
        (b,) = discovery.remember([self.found("10.0.0.5", uid="uuid-b")])
        a.refresh_from_db()
        self.assertTrue(a.host.startswith(discovery.MOVED))
        self.assertEqual(b.host, "10.0.0.5")
        sync.sync_all()  # the moved TV isn't contacted

    def test_scan_sweeps_subnets_and_known_tvs(self):
        self.tv("192.168.11.138")
        frames = {"192.168.10.124", "192.168.11.138"}
        with patch("frame.discovery._open", side_effect=lambda h: h in frames | {"192.168.10.9"}), \
                patch("frame.tv.describe", side_effect=lambda h: {"uid": "uuid-" + h, "name": h, "model": "",
                                                                  "frame": h in frames, "on": True}):
            got = discovery.scan(discovery.parse_subnets("192.168.10.0/24"))
        self.assertEqual({d["host"] for d in got}, frames)

    def test_parse_subnets(self):
        nets = discovery.parse_subnets("192.168.11.0/24, 10.0.0.7/24 nonsense 10.0.0.0/8 fe80::/64")
        self.assertEqual([str(n) for n in nets], ["192.168.11.0/24", "10.0.0.0/24"])
        self.settings["scan_subnets"] = "192.168.11.0/24"
        with patch("frame.discovery.local_subnet", return_value=__import__("ipaddress").ip_network("192.168.10.0/24")):
            self.assertEqual([str(n) for n in discovery.subnets()], ["192.168.10.0/24", "192.168.11.0/24"])

    def test_excluded_tv_is_emptied_not_paired_and_hidden(self):
        living, office = self.tv("192.168.11.138"), self.tv("192.168.10.124")
        office.uid = "uuid-office"
        office.save()
        self.family(2)
        sync.sync_all()
        self.assertEqual(len(FakeTV.tvs[office.host]["photos"]), 3)
        self.settings["skip_tvs"] = '["uuid-office"]'
        self.assertEqual([t.id for t in discovery.enabled_tvs()], [living.id])
        sync.sync_all()
        self.assertEqual(FakeTV.tvs[office.host]["photos"], ["MY_F0001"])  # only the TV's own photo
        self.assertFalse(Placement.objects.filter(tv=office).exists())
        self.assertEqual(len(FakeTV.tvs[living.host]["photos"]), 3)
        with self.assertRaises(service.Invalid):
            service.add_photo(jpeg(), FAMILY, tv_ids=[office.id])
        # An excluded TV that isn't paired is never asked to pair.
        make_fake_tv("10.0.0.9")
        TV.objects.create(name="Basement", host="10.0.0.9", uid="uuid-basement")
        self.settings["skip_tvs"] = '["uuid-office", "uuid-basement"]'
        sync.sync_all()
        self.assertEqual(TV.objects.get(uid="uuid-basement").token, "")

    def test_settings_choices(self):
        self.tv("192.168.11.138")
        TV.objects.create(name="Office television", host="192.168.10.124", uid="uuid-o")
        self.assertEqual(discovery.tv_choices(), [
            ("host:192.168.11.138", "Living Room \u00b7 192.168.11.138"),
            ("uuid-o", "Office television \u00b7 192.168.10.124 \u00b7 not paired yet"),
        ])

    def test_worker_discovers_on_its_schedule(self):
        worker._last_discover = None
        with patch("frame.discovery.discover") as d, patch("frame.sync.sync_all"):
            worker.tick()
            worker.tick()
        self.assertEqual(d.call_count, 1)


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

    def test_heic_from_an_iphone(self):
        """The upload pages accept HEIC (so iOS doesn't convert first): the hub must read it."""
        out = io.BytesIO()
        Image.new("RGB", (4032, 3024), (30, 120, 200)).save(out, "HEIF")
        big, thumb, w, h = images.prepare(out.getvalue())
        self.assertEqual((w, h), (2880, 2160))
        self.assertEqual(Image.open(io.BytesIO(big)).format, "JPEG")
        self.assertEqual(Image.open(io.BytesIO(images.for_tv(big))).size, images.TV_SIZE)

    def test_upload_pages_accept_heic(self):
        for name in ("index.html", "guest.html"):
            html = (ROOT / "templates" / "frame" / name).read_text()
            self.assertIn('accept="image/*,image/heic,image/heif,.heic,.heif"', html, name)

    def test_for_tv_frames_portrait_with_blurred_sides(self):
        out = images.for_tv(jpeg(1080, 1440, color=(200, 100, 50)))
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.size, images.TV_SIZE)
        self.assertEqual(img.format, "JPEG")
        mid, side = img.getpixel((1920, 1080)), img.getpixel((50, 1080))
        self.assertTrue(all(abs(a - b) < 12 for a, b in zip(mid, (200, 100, 50))), mid)
        self.assertLess(sum(side), sum(mid) * 0.75)  # darkened copy, not black bars or the photo
        self.assertGreater(sum(side), 60)

    def test_for_tv_fills_the_panel_with_16_9_and_small_photos(self):
        for size in ((3840, 2160), (1672, 941), (300, 200)):
            img = Image.open(io.BytesIO(images.for_tv(jpeg(*size))))
            self.assertEqual(img.size, images.TV_SIZE, size)


class IntervalTests(Base):
    """Anyone using the app may change how often the TVs change photo."""

    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("stefan", password="x", is_staff=True)
        self.c = Client()

    def post(self, url, *value, **body):
        data = body or {"interval": value[0]}
        return self.c.post(url, json.dumps(data), content_type="application/json")

    def test_default_and_old_hub_setting_carried_over(self):
        self.assertEqual(service.interval(), "15")
        service._last_interval = None
        self.settings["interval"] = "60"  # set in hub Settings before b6
        self.assertEqual(service.interval(), "60")
        self.settings["interval"] = "3"
        self.assertEqual(service.interval(), "60")  # copied once; the app owns it now

    def test_guest_changes_it(self):
        self.hub_stay = cur()
        gs = service.evaluate()
        s = self.c.get(reverse("frame_state")).json()
        self.assertEqual((s["role"], s["interval"]), ("guest", "15"))
        self.assertIn(["3", "3 minutes"], s["intervals"])
        self.assertEqual(self.post(reverse("frame_interval"), "3").status_code, 200)
        self.assertEqual(service.interval(), "3")
        self.assertEqual(self.post(reverse("frame_guest_interval", args=[gs.link_secret]), "60").status_code, 200)
        self.assertEqual(service.interval(), "60")

    def test_family_link_and_admin_change_it(self):
        token = signing.dumps({"f": 1}, salt=views.FAMILY_SALT, compress=False)
        self.assertEqual(self.post(reverse("frame_guest_interval", args=[token]), "720").status_code, 200)
        self.assertEqual(service.interval(), "720")
        self.c.force_login(self.admin)
        self.assertEqual(self.post(reverse("frame_interval"), "1440").status_code, 200)
        self.assertEqual(self.c.get(reverse("frame_state")).json()["interval"], "1440")

    def test_anyone_using_the_app_can_start_a_tvs_slideshow(self):
        """Each TV has its own play button; it restarts that TV only."""
        tv = TV.objects.create(name="Living Room", host="10.0.0.9", token="tok")
        other = TV.objects.create(name="Office", host="10.0.0.10", token="tok")
        unpaired = TV.objects.create(name="Basement", host="10.0.0.11")
        url = reverse("frame_restart")
        with patch("frame.worker.poke") as poke:
            self.assertEqual(self.post(url, tv=tv.id).status_code, 403)  # wall screen
            self.hub_stay = cur()
            gs = service.evaluate()
            self.assertEqual(self.post(url, tv=tv.id).status_code, 200)
            self.assertEqual(sync._restart, {tv.id})
            self.assertEqual(self.post(reverse("frame_guest_restart", args=[gs.link_secret]), tv=other.id).status_code, 200)
            self.assertEqual(self.post(reverse("frame_guest_restart", args=["nope"]), tv=tv.id).status_code, 404)
            self.assertEqual(self.post(url, tv=unpaired.id).status_code, 404)
            self.assertEqual(self.post(url, tv=999).status_code, 404)
            for bad in ("1", True, [1]):
                self.assertEqual(self.post(url, tv=bad).status_code, 400)
        self.assertEqual(poke.call_count, 2)
        self.assertEqual(sync._restart, {tv.id, other.id})

    def test_bad_values_and_nobody_signed_in(self):
        self.c.force_login(self.admin)
        for bad in ("7", "", None, "15; drop"):
            self.assertEqual(self.post(reverse("frame_interval"), bad).status_code, 400)
        self.assertEqual(service.interval(), "15")
        self.c.logout()  # wall screen, no stay: no role
        self.assertEqual(self.post(reverse("frame_interval"), "3").status_code, 403)
        self.assertNotIn("interval", self.c.get(reverse("frame_state")).json())
        self.assertEqual(self.post(reverse("frame_guest_interval", args=["nope"]), "3").status_code, 404)


class PairingTests(Base):
    """The TVs page: an admin pairs a TV and is told what went wrong."""

    def setUp(self):
        super().setUp()
        self.admin = get_user_model().objects.create_user("stefan", password="x", is_staff=True)
        self.c = Client()
        make_fake_tv("192.168.10.124")
        self.office = TV.objects.create(name="Office television", host="192.168.10.124", uid="uuid-o")

    def pair(self, pairer):
        tvlib.set_factory(FakeTV, pairer=pairer)
        self.c.force_login(self.admin)
        return self.c.post(reverse("frame_tv_pair", args=[self.office.id]))

    def office_row(self, data):
        return next(t for t in data["tvs"] if t["id"] == self.office.id)

    def test_admin_only(self):
        self.assertEqual(self.c.get(reverse("frame_tvs")).status_code, 403)
        self.assertEqual(self.c.get(reverse("frame_tvs_state")).status_code, 403)
        self.assertEqual(self.c.post(reverse("frame_tv_pair", args=[self.office.id])).status_code, 403)
        self.assertEqual(self.c.post(reverse("frame_tvs_scan")).status_code, 403)
        self.hub_stay = cur()  # a guest is no admin either
        service.evaluate()
        self.assertEqual(self.c.get(reverse("frame_tvs")).status_code, 403)
        self.assertEqual(self.office.token, "")

    def test_page_lists_tvs_with_the_steps(self):
        self.c.force_login(self.admin)
        r = self.c.get(reverse("frame_tvs"))
        self.assertContains(r, "Access Notification")
        self.assertContains(r, "Office television")
        row = self.office_row(self.c.get(reverse("frame_tvs_state")).json())
        self.assertEqual((row["paired"], row["pair"]), (False, None))

    def test_pair_now_ignores_the_half_hour_wait(self):
        sync.maybe_pair(self.office)  # the worker asked a minute ago (nobody answered)
        TV.objects.filter(id=self.office.id).update(token="")
        sync._pair_status.clear()
        r = self.pair(lambda host: "tok-new")
        self.assertEqual(r.status_code, 200)
        self.office.refresh_from_db()
        self.assertEqual(self.office.token, "tok-new")
        self.assertEqual(self.office_row(r.json())["pair"]["state"], "paired")
        self.assertEqual(self.c.post(reverse("frame_tv_pair", args=[self.office.id])).status_code, 400)

    def test_each_failure_says_what_to_do(self):
        def raises(exc):
            def f(host):
                raise exc
            return f
        for exc, state in ((tvlib.PairDenied("no"), "denied"), (tvlib.PairTimeout("slow"), "timeout"),
                           (tvlib.Standby("off"), "standby"), (tvlib.Offline("gone"), "offline"),
                           (RuntimeError("weird"), "failed")):
            sync._pair_status.clear()
            r = self.pair(raises(exc))
            self.assertEqual(self.office_row(r.json())["pair"]["state"], state, exc)
            self.office.refresh_from_db()
            self.assertEqual(self.office.token, "")

    def test_pair_errors_from_the_tv_library(self):
        class UnauthorizedError(Exception):
            pass
        self.assertIsInstance(tvlib.pair_error(UnauthorizedError("x")), tvlib.PairDenied)
        self.assertIsInstance(tvlib.pair_error(TimeoutError("timed out")), tvlib.PairTimeout)
        self.assertIsInstance(tvlib.pair_error(ConnectionRefusedError("refused")), tvlib.Offline)

    def test_pairing_asks_only_a_tv_that_is_on(self):
        with patch("frame.tv.describe", return_value={"on": False}):
            with self.assertRaises(tvlib.Standby):
                tvlib.pair("192.168.10.124")

    def test_look_now(self):
        self.c.force_login(self.admin)
        with patch("frame.worker.poke") as poke:
            self.assertTrue(self.c.post(reverse("frame_tvs_scan")).json()["looking"])
        poke.assert_called_once()
        self.assertIsNone(worker._last_discover)
        with patch("frame.discovery.discover"):
            worker._discover_if_due()
        self.assertFalse(worker.discovering())


class DeadlineTests(SimpleTestCase):
    """A TV that never answers must not hang the worker (seen: stuck mid-upload for good)."""

    def test_call_past_its_deadline_fails(self):
        import threading
        import time as t

        gone = threading.Event()

        class Sock:
            def shutdown(self, how):
                gone.set()

        class Art:
            connection = type("C", (), {"sock": Sock()})()

            def close(self):
                pass

        def stuck(art):
            if not gone.wait(5):  # samsungtvws waits like this, forever
                return "never"
            raise OSError("socket shut")

        c = tvlib.FrameClient("10.0.0.1", "tok")
        c._art = Art()
        start = t.monotonic()
        with self.assertRaisesRegex(tvlib.TVError, "stopped answering"):
            c._call(stuck, deadline=0.2)
        self.assertLess(t.monotonic() - start, 2)
        # A quick call is unaffected and the next call starts a fresh timer.
        c._art = Art()
        gone.clear()
        self.assertEqual(c._call(lambda art: "ok", deadline=0.2), "ok")
        t.sleep(0.4)
        self.assertFalse(gone.is_set())


class TileTests(Base):
    def test_tile_never_talks_about_tv_setup(self):
        from frame.homepage import status
        self.assertEqual(status().text, "0 photos")  # no TVs yet
        TV.objects.create(name="Office television", host="192.168.10.124", uid="uuid-o")  # unpaired
        self.assertNotIn("pair", status().text.lower())
        self.assertEqual(status().text, "0 photos")


class TokenTests(SimpleTestCase):
    def test_client_notices_a_new_token(self):
        class Art:
            token = "tok"

            def get_artmode(self):
                self.token = "tok-new"  # what samsungtvws does on ms.channel.connect
                return "on"

        c = tvlib.FrameClient("10.0.0.1", "tok")
        c._art = Art()
        self.assertTrue(c.art_mode())
        self.assertEqual(c.new_token, "tok-new")
        c2 = tvlib.FrameClient("10.0.0.1", "tok")
        c2._art = type("Same", (), {"token": "tok", "get_artmode": lambda self: "on"})()
        c2.art_mode()
        self.assertIsNone(c2.new_token)


class PluginTests(SimpleTestCase):
    def test_plugin_importable(self):
        assert_plugin_importable("frame.plugin")

    def test_manifest(self):
        from frame.plugin import spec
        self.assertEqual((spec.network, spec.lifecycle), ("lan", "always"))
        self.assertIn("stays.read", spec.permissions)
        self.assertTrue((ROOT / "static" / spec.tiles[0].icon).exists())
        self.assertEqual(reverse("frame_state"), "/frame/api/state/")

    def test_settings_manage_tvs(self):
        from frame.plugin import spec
        by_key = {x.key: x for x in spec.settings}
        self.assertEqual(by_key["skip_tvs"].choices_from, "frame.discovery.tv_choices")
        self.assertIn("scan_subnets", by_key)
        self.assertNotIn("interval", by_key)  # set in the app, by anyone using it
        self.assertEqual([(pg.title, pg.url_name) for pg in spec.settings_pages], [("TVs", "frame_tvs")])
        js = (ROOT / "static" / "frame" / "frame.js").read_text()
        self.assertNotIn("add-tv", js)  # TVs are managed in Settings, not in the app

    def test_hub_page_is_dark_like_the_other_apps(self):
        # Bootstrap's body is white; the page must set the shared dark backdrop itself.
        self.assertIn("{% block body_class %}fr-page{% endblock %}",
                      (ROOT / "templates" / "frame" / "index.html").read_text())
        css = (ROOT / "static" / "frame" / "frame.css").read_text()
        self.assertRegex(css, r"body\.fr-page\s*\{\s*background: radial-gradient\([^)]*#1d3350")

    def test_templates_escape_and_never_inline_family_photos(self):
        js = (ROOT / "static" / "frame" / "guest.js").read_text()
        self.assertNotIn("family\"", js.replace("family ", ""))  # phone page gets photos only from the API

class NoNativeDialogTests(SimpleTestCase):
    # The hub shows the app in a sandboxed iframe without allow-modals, so
    # confirm()/alert()/prompt() are silently ignored there (bulk delete did nothing).
    def test_static_js_has_no_native_dialogs(self):
        import re
        pattern = re.compile(r"(?<![\w.$])(?:(?:window|self|top|parent)\.)?(confirm|alert|prompt)\s*\(")
        static = pathlib.Path(__file__).resolve().parents[1] / "static" / "frame"
        found = []
        for path in static.glob("*.js"):
            text = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(encoding="utf-8"), flags=re.S)
            found += ["%s: %s()" % (path.name, m.group(1)) for m in pattern.finditer(text)]
        self.assertEqual(found, [])

    def test_every_button_action_is_handled(self):
        """A button whose data-act/data-* nothing reads does nothing when tapped."""
        import re
        static = pathlib.Path(__file__).resolve().parents[1] / "static" / "frame"
        texts = {p.name: p.read_text(encoding="utf-8") for p in static.glob("*.js")}
        alljs = "\n".join(texts.values())
        missing = []
        for name, text in texts.items():
            for v in set(re.findall(r'data-act="([\w-]+)"', text)):
                if not re.search(r'case "%s"|act === "%s"' % (v, v), text):
                    missing.append("%s: data-act=%s" % (name, v))
            for a in set(re.findall(r"data-([a-z][a-z-]*)[=\s>\"']", text)) - {"act"}:
                camel = re.sub(r"-(\w)", lambda m: m.group(1).upper(), a)
                if not re.search(r"\.%s\b|\[data-%s[\]=]|dataset\.%s\b" % (camel, a, camel), alljs):
                    missing.append("%s: data-%s" % (name, a))
        self.assertEqual(missing, [])
