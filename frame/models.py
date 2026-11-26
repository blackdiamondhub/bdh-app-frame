import secrets
import uuid

from django.db import models

FAMILY = "family"
GUEST = "guest"


def _link_secret():
    return secrets.token_urlsafe(12)


def _photo_id():
    return uuid.uuid4().hex


class TV(models.Model):
    name = models.CharField(max_length=80)
    host = models.CharField(max_length=255, unique=True)
    model = models.CharField(max_length=80, blank=True)
    # The token the TV issued when someone pressed Allow; empty = not paired yet.
    token = models.CharField(max_length=255, blank=True)
    online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_sync = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True)
    # Photos in the TV's "My Photos" that Frame didn't put there.
    unmanaged = models.IntegerField(default=0)
    # The slideshow last set on the TV ("off" or "<minutes>:<shuffle 0|1>").
    slideshow = models.CharField(max_length=20, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self):
        return self.name


class GuestStay(models.Model):
    """A guest stay as Frame sees it: from when the hub first reports it until
    midnight after checkout, the owners arriving, or an admin ending it."""

    key = models.CharField(max_length=64, unique=True)
    guest = models.CharField(max_length=120, blank=True)
    checkout = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    # The secret in the guests' upload link (QR code). New for every stay.
    link_secret = models.CharField(max_length=32, default=_link_secret, unique=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.guest or self.key}"


class Photo(models.Model):
    id = models.CharField(primary_key=True, max_length=32, default=_photo_id, editable=False)
    kind = models.CharField(max_length=10, choices=((FAMILY, "Family"), (GUEST, "Guest")))
    stay = models.ForeignKey(GuestStay, null=True, blank=True, on_delete=models.CASCADE,
                             related_name="photos")
    width = models.IntegerField(default=0)
    height = models.IntegerField(default=0)
    all_tvs = models.BooleanField(default=True)
    tvs = models.ManyToManyField(TV, blank=True, related_name="photos")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]

    @property
    def image_name(self):
        return f"photos/{self.id}.jpg"

    @property
    def thumb_name(self):
        return f"thumbs/{self.id}.jpg"


class Placement(models.Model):
    """A photo uploaded to a TV. Outlives its photo (``photo`` becomes null) so
    the worker can still delete it from the TV."""

    photo = models.ForeignKey(Photo, null=True, on_delete=models.SET_NULL, related_name="placements")
    tv = models.ForeignKey(TV, on_delete=models.CASCADE, related_name="placements")
    content_id = models.CharField(max_length=40)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tv", "content_id"], name="frame_placement_tv_content")]
