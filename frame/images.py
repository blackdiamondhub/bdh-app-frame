"""Turn an uploaded photo into what the TVs show (4K JPEG) and a thumbnail."""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

#: The Frame's panel.
TV_SIZE = (3840, 2160)
THUMB_SIZE = (480, 480)
#: Refuse anything bigger than a 100 MP camera (and decompression bombs).
MAX_PIXELS = 100_000_000


class BadImage(ValueError):
    pass


def _fit(img, box):
    """Shrink ``img`` to fit inside ``box`` (portrait photos fit the panel's height)."""
    w, h = img.size
    bw, bh = box
    if w < h:
        bw, bh = bh, bw  # portrait: a Frame shows it with a portrait matte, up to 2160 wide
    scale = min(bw / w, bh / h, 1.0)
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return img


def prepare(data):
    """``(jpeg, thumb_jpeg, width, height)`` for the bytes of an uploaded photo."""
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            raise BadImage("That photo is too large")
        # JPEG can decode at 1/2, 1/4 or 1/8 size: much less memory for big photos.
        img.draft("RGB", (TV_SIZE[0], TV_SIZE[0]))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except BadImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BadImage("That isn't a photo Frame can read (use JPEG or PNG)") from exc
    big = _fit(img, TV_SIZE)
    out = io.BytesIO()
    big.save(out, "JPEG", quality=90, optimize=True)
    thumb = big.copy()
    thumb.thumbnail(THUMB_SIZE, Image.LANCZOS)
    small = io.BytesIO()
    thumb.save(small, "JPEG", quality=80)
    return out.getvalue(), small.getvalue(), big.width, big.height
