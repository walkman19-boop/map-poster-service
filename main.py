import os
import io
import uuid
import re
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify
from google.cloud import storage
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "renders").strip("/")
GMAPS_API_KEY = os.environ.get("GMAPS_API_KEY", "")


# ---------- helpers ----------

def parse_maps_link(maps_link: str):
    """
    Accepts links like:
    https://www.google.com/maps/@54.707849,25.3968932,16z
    Returns (lat, lng, zoom_int)
    """
    if not maps_link:
        return None

    m = re.search(r"/@(-?\d+\.\d+),(-?\d+\.\d+),(\d+)(?:\.\d+)?z", maps_link)
    if not m:
        return None

    lat = float(m.group(1))
    lng = float(m.group(2))
    zoom = int(m.group(3))
    return lat, lng, zoom


def upload_to_gcs(data: bytes, content_type: str, filename: str) -> str:
    if not OUTPUT_BUCKET:
        raise RuntimeError("Missing OUTPUT_BUCKET env var")

    client = storage.Client()
    bucket = client.bucket(OUTPUT_BUCKET)

    object_name = f"{GCS_PREFIX}/{filename}" if GCS_PREFIX else filename
    blob = bucket.blob(object_name)
    blob.upload_from_string(data, content_type=content_type)

    # simplest flow: make it public
    blob.make_public()
    return blob.public_url


def fetch_static_map(lat: float, lng: float, zoom: int, size_px=(1200, 1200), scale=2, maptype="roadmap"):
    if not GMAPS_API_KEY:
        raise RuntimeError("Missing GMAPS_API_KEY env var")

    w, h = size_px
    # NOTE: Google Static Maps max size is limited; scale=2 helps quality.
    url = "https://maps.googleapis.com/maps/api/staticmap"
    params = {
        "center": f"{lat},{lng}",
        "zoom": str(zoom),
        "size": f"{w}x{h}",
        "scale": str(scale),
        "maptype": maptype,
        "key": GMAPS_API_KEY,
        # optional: minimal UI
        "style": [
            "feature:poi|visibility:off",
            "feature:transit|visibility:off",
        ],
    }

    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Static Maps API failed: {r.status_code} {r.text[:200]}")
    return r.content


def draw_labels(img: Image.Image, title: str, subtitle: str):
    # bottom-left label block
    draw = ImageDraw.Draw(img)

    # Try to use default font; you can later add a real font file to repo for better typography
    try:
        font_title = ImageFont.truetype("DejaVuSans.ttf", 44)
        font_sub = ImageFont.truetype("DejaVuSans.ttf", 28)
    except:
        font_title = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    pad = 40
    x = pad
    y = img.height - pad - 120

    # semi-transparent background box
    box_w = 520
    box_h = 120
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    o = ImageDraw.Draw(overlay)
    o.rectangle([x - 18, y - 18, x - 18 + box_w, y - 18 + box_h], fill=(0, 0, 0, 140))
    img_rgba = img.convert("RGBA")
    img_rgba = Image.alpha_composite(img_rgba, overlay)

    draw = ImageDraw.Draw(img_rgba)
    draw.text((x, y), (title or "").upper(), fill=(255, 255, 255, 255), font=font_title)
    draw.text((x, y + 58), (subtitle or "").upper(), fill=(255, 255, 255, 220), font=font_sub)
    return img_rgba.convert("RGB")


# ---------- routes ----------

@app.get("/health")
def health():
    return {"ok": True}


@app.post("/render")
def render():
    payload = request.get_json(silent=True) or {}

    maps_link = (payload.get("maps_link") or "").strip()
    if not maps_link:
        return jsonify({"ok": False, "error": "maps_link is required"}), 400

    parsed = parse_maps_link(maps_link)
    if not parsed:
        return jsonify({
            "ok": False,
            "error": "Unsupported maps_link format. Expected something like https://www.google.com/maps/@LAT,LNG,ZOOMz"
        }), 400

    lat, lng, zoom_from_link = parsed

    # allow overriding zoom
    zoom = int(payload.get("zoom") or zoom_from_link)

    output = (payload.get("output") or "PNG").upper()
    title = (payload.get("title") or "").strip()
    subtitle = (payload.get("subtitle") or "").strip()

    # 1) get real map image
    map_png = fetch_static_map(lat, lng, zoom, size_px=(1200, 1200), scale=2, maptype="roadmap")
    img = Image.open(io.BytesIO(map_png)).convert("RGB")

    # 2) add labels
    img = draw_labels(img, title, subtitle)

    # 3) export
    filename_base = uuid.uuid4().hex
    if output == "PNG":
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        url = upload_to_gcs(data, "image/png", f"{filename_base}.png")
        return jsonify({"ok": True, "file": f"{filename_base}.png", "url": url})

    return jsonify({"ok": False, "error": "Only PNG supported for now (set output: PNG)"}), 400
