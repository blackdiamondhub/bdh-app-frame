"""Turn an uploaded photo into what the TVs show (4K JPEG) and a thumbnail."""
import io

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

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
        bw, bh = bh, bw  # portrait: keep up to 2160 wide (framed for the TV in for_tv)
    scale = min(bw / w, bh / h, 1.0)
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return img


def for_tv(data):
    """The JPEG a TV is sent: exactly the panel's size (16:9), the whole photo in the
    middle and a blurred, darkened copy of it filling the sides.

    Frame TVs crop anything that isn't 16:9, and their art app crashes (error 40000)
    when a non-16:9 photo has a matte, so the TV only ever gets this, matte "none".
    """
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (TV_SIZE[0], TV_SIZE[0]))
    img = img.convert("RGB")
    W, H = TV_SIZE
    w, h = img.size
    fit = min(W / w, H / h)
    fw, fh = max(1, round(w * fit)), max(1, round(h * fit))
    if (fw, fh) == (W, H):
        canvas = img if img.size == TV_SIZE else img.resize(TV_SIZE, Image.LANCZOS)
    else:
        # Blur a small cover-crop and scale it up: cheap, and smoother than blurring 4K.
        small = ImageOps.fit(img, (W // 10, H // 10), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(6))
        small = ImageEnhance.Brightness(small).enhance(0.55)
        canvas = small.resize(TV_SIZE, Image.BICUBIC)
        photo = img if (fw, fh) == (w, h) else img.resize((fw, fh), Image.LANCZOS)
        canvas.paste(photo, ((W - fw) // 2, (H - fh) // 2))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


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
