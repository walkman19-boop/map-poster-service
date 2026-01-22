import os
import re
import io
import json
import uuid
from datetime import timedelta

import requests
from flask import Flask, request, jsonify

from PIL import Image, ImageDraw, ImageFont
from google.cloud import storage

app = Flask(__name__)


# ----------------------------
# Helpers: config / parsing
# ----------------------------

def getenv_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def get_gmaps_key() -> str:
    # support both names (you previously had MAPS_API_KEY)
    key = os.getenv("GMAPS_API_KEY") or os.getenv("MAPS_API_KEY")
    if not key:
        raise RuntimeError("Missing GMAPS_API_KEY env var")
    return key


def parse_google_maps_link(maps_link: str):
    """
    Supports common formats, including:
      https://www.google.com/maps/@54.707849,25.3968932,16z
    Returns: (lat: float, lng: float, zoom: int|None)
    """
    if not maps_link or not isinstance(maps_link, str):
        raise ValueError("maps_link is required and must be a string")

    # Try @lat,lng,zoomz
    m = re.search(r"/@(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)z", maps_link)
    if m:
        lat = float(m.group(1))
        lng = float(m.group(2))
        zoom = int(float(m.group(3)))
        return lat, lng, zoom

    # Try query param q=lat,lng or ll=lat,lng (fallback, zoom unknown)
    m = re.search(r"[?&](?:q|ll)=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", maps_link)
    if m:
        lat = float(m.group(1))
        lng = float(m.group(2))
        return lat, lng, None

    raise ValueError("Could not parse lat/lng/zoom from maps_link. Expected /@lat,lng,zoomz format.")


# ----------------------------
# Google Static Maps
# ----------------------------

def fetch_static_map(lat: float, lng: float, zoom: int, size_px=(1200, 1200), scale: int = 2, maptype: str = "roadmap") -> bytes:
    """
    Fetches PNG bytes from Google Static Maps API.
    Note: max 'size' for static maps is 640x640 (free), but with scale=2 you get higher resolution.
    We'll request within limits: size <= 640x640, scale=2 gives up to 1280px effective.
    """
    key = get_gmaps_key()

    # Google Static Maps limit: size param max 640x640
    # We'll clamp requested size to 640 while keeping output quality with scale=2.
    req_w = min(640, max(1, int(size_px[0] // scale)))
    req_h = min(640, max(1, int(size_px[1] // scale)))

    params = {
        "center": f"{lat},{lng}",
        "zoom": str(int(zoom)),
        "size": f"{req_w}x{req_h}",
        "scale": str(int(scale)),
        "maptype": maptype,
        "format": "png",
        "key": key,
    }

    url = "https://maps.googleapis.com/maps/api/staticmap"
    r = requests.get(url, params=params, timeout=30)
    # If billing/API disabled or restrictions wrong, you'll see 403/400 here.
    r.raise_for_status()
    return r.content


# ----------------------------
# Poster rendering
# ----------------------------

def load_font(size: int) -> ImageFont.FreeTypeFont:
    """
    Tries a few common fonts; falls back to default.
    """
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_poster_png(map_png_bytes: bytes, title: str = "", subtitle: str = "", out_size=(1200, 1200)) -> bytes:
    """
    Makes a simple poster:
      - map as background (center-cropped to out_size)
      - translucent panel bottom
      - title/subtitle text
    """
    img = Image.open(io.BytesIO(map_png_bytes)).convert("RGBA")

    # center-crop to out_size
    target_w, target_h = out_size
    # Resize up if needed
    if img.width < target_w or img.height < target_h:
        scale = max(target_w / img.width, target_h / img.height)
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    left = (img.width - target_w) // 2
    top = (img.height - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))

    draw = ImageDraw.Draw(img, "RGBA")

    # Bottom overlay
    panel_h = int(target_h * 0.18)
    panel_y0 = target_h - panel_h
    draw.rectangle([(0, panel_y0), (target_w, target_h)], fill=(255, 255, 255, 200))

    # Text
    title = (title or "").strip()
    subtitle = (subtitle or "").strip()

    title_font = load_font(64)
    subtitle_font = load_font(36)

    padding_x = 60
    y = panel_y0 + 25

    if title:
        draw.text((padding_x, y), title, fill=(0, 0, 0, 255), font=title_font)
        y += 70

    if subtitle:
        draw.text((padding_x, y), subtitle, fill=(40, 40, 40, 255), font=subtitle_font)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def png_to_pdf_bytes(png_bytes: bytes) -> bytes:
    """
    Simple 1-page PDF with the PNG filling the page.
    Uses reportlab (installed).
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(w, h))
    c.drawImage(ImageReader(img), 0, 0, width=w, height=h)
    c.showPage()
    c.save()
    return out.getvalue()


# ----------------------------
# GCS upload + signed URL
# ----------------------------

def upload_to_gcs(data: bytes, content_type: str, object_name: str) -> str:
    bucket_name = os.getenv("OUTPUT_BUCKET")
    if not bucket_name:
        raise RuntimeError("Missing OUTPUT_BUCKET env var")

    prefix = (os.getenv("GCS_PREFIX") or "").strip().strip("/")
    if prefix:
        object_name = f"{prefix}/{object_name}"

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)

    blob.upload_from_string(data, content_type=content_type)

    sign_urls = getenv_bool("SIGN_URLS", False)
    if sign_urls:
        # requires roles/iam.serviceAccountTokenCreator on the service account
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=30),
            method="GET",
        )

    # Not public; returns gs:// URL as an internal reference
    return f"gs://{bucket_name}/{object_name}"


# ----------------------------
# Routes
# ----------------------------

@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/render")
def render():
    """
    JSON body example:
    {
      "maps_link": "https://www.google.com/maps/@54.707849,25.3968932,16z",
      "zoom": 16,
      "output": "PNG",
      "title": "Pupojai",
      "subtitle": "Vilnius"
    }
    """
    payload = request.get_json(silent=True) or {}

    maps_link = payload.get("maps_link", "")
    title = payload.get("title", "")
    subtitle = payload.get("subtitle", "")
    output = (payload.get("output") or "PNG").strip().upper()

    # zoom: from JSON or from link
    zoom_in = payload.get("zoom")
    lat, lng, zoom_from_link = parse_google_maps_link(maps_link)
    zoom = int(zoom_in) if zoom_in is not None else (zoom_from_link if zoom_from_link is not None else 16)

    # Fetch map and render
    map_png = fetch_static_map(lat, lng, zoom, size_px=(1200, 1200), scale=2, maptype="roadmap")
    poster_png = render_poster_png(map_png, title=title, subtitle=subtitle, out_size=(1200, 1200))

    filename_base = uuid.uuid4().hex

    if output == "PNG":
        url = upload_to_gcs(poster_png, "image/png", f"{filename_base}.png")
        return jsonify({"ok": True, "output": "PNG", "url": url})

    if output == "PDF":
        pdf_bytes = png_to_pdf_bytes(poster_png)
        url = upload_to_gcs(pdf_bytes, "application/pdf", f"{filename_base}.pdf")
        return jsonify({"ok": True, "output": "PDF", "url": url})

    return jsonify({"ok": False, "error": "Invalid output. Use PNG or PDF."}), 400


# For local dev
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
