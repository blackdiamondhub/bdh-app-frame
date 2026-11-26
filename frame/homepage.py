from bdh_sdk.plugins import INFO, WARN, Status

from .models import TV, Photo
from . import service


def status():
    tvs = list(TV.objects.all())
    if not tvs:
        return Status("No TVs added", WARN)
    if any(not t.token for t in tvs):
        return Status("A TV needs pairing", WARN)
    stay = service.active_stay()
    if stay is not None:
        n = Photo.objects.filter(stay=stay).count()
        return Status(f"{n} guest photo{'s' * (n != 1)}" if n else "Add your photos", INFO)
    n = Photo.objects.filter(kind="family").count()
    return Status(f"{n} photo{'s' * (n != 1)}", INFO)
