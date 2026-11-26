from bdh_sdk.plugins import INFO, Status

from .models import Photo
from . import service


def status():
    # TV setup (adding, pairing) lives on the admin TVs page, never on the tile guests see.
    stay = service.active_stay()
    if stay is not None:
        n = Photo.objects.filter(stay=stay).count()
        return Status(f"{n} guest photo{'s' * (n != 1)}" if n else "Add your photos", INFO)
    n = Photo.objects.filter(kind="family").count()
    return Status(f"{n} photo{'s' * (n != 1)}", INFO)
