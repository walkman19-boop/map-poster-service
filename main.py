import os
import time
from io import BytesIO
from typing import Tuple

import requests
from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont

# -------- Version (so you can confirm Render is running this exact file) --------
VERSION = "maptiler-full-1"

app = Flask(__name__)

# -------- Config / Env --------
USER_AGENT = os.environ.get("USER_AGENT", "map-poster-service/1.0 (contact: you@domain.com)")

MAPTILER_KEY = os.environ.get("MAPTILER_KEY", "")
MAPTILER_MAP_ID = os.environ.get("MAPTILER_MAP_ID", "streets-v2")

DEFAULT_ZOOM = int(os.environ.get("DEFAULT_ZOOM", "12"))      # 0..20 typical
DEFAULT_SIZE = int(os.environ.get("DEFAULT_SIZE", "1024"))    # output map square px (512..4096)
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "neon")       # neon | dark | light

FONT_PATH = os.environ.get("FONT_PATH", "")  # optional: put a .ttf into repo and set env


# -------- Helpers --------
def _load_font(size: int) -> ImageFont.ImageFont:
    if FONT_PATH and os.path.exists(FONT_PATH):
        return ImageFont.truetype(FONT_PATH, size=size)
    return ImageFont.load_default()


def geocode_nominatim(address: str) -> Tuple[float, float]:
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError("Address not found")
    return float(data[0]["lat"]), float(data[0]["lon"])


def fetch_map_maptiler(lat: float, lon: float, zoom: int, size_px: int) -> Image.Image:
    if not MAPTILER_KEY:
        raise ValueError("Missing MAPTILER_KEY (set it in Render Environment Variables)")

    # keep memory reasonable on free instances
    size_px = max(512, min(int(size_px), 4096))
    zoom = max(0, min(int(zoom), 20))

    # MapTiler endpoint: /maps/{mapId}/static/{lon},{lat},{zoom}/{width}x{height}@2x.png?key=...
    # Use @2x then downscale -> nicer lines.
    w = max(256, min(size_px, 2048))
    h = max(256, min(size_px, 2048))

    url = f"https://api.maptiler.com/maps/{MAPTILER_MAP_ID}/static/{lon},{lat},{zoom}/{w}x{h}@2x.png"
    r = requests.get(
        url,
        params={"key": MAPTILER_KEY, "attribution": "false"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    r.raise_for_status()

    img = Image.open(BytesIO(r.content)).convert("RGBA")
    img = img.resize((size_px, size_px), Image.LANCZOS)
    return img


def apply_theme(map_img: Image.Image, theme: str) -> Image.Image:
    theme = (theme or "").lower().strip()
    if theme not in ("neon", "dark", "light"):
        theme = DEFAULT_THEME

    img = map_img.copy()

    if theme == "light":
        return img

    # darken overlay
    alpha = 140 if theme == "dark" else 165
    overlay = Image.new("RGBA", img.size, (0, 0, 0, alpha))
    img = Image.alpha_composite(img, overlay)

    if theme == "neon":
        r, g, b, a = img.split()
        # subtle neon-ish push
        g = g.point(lambda x: min(255, int(x * 0.95)))
        b = b.point(lambda x: min(255, int(x * 1.10)))
        img = Image.merge("RGBA", (r, g, b, a))

    return img


def compose_poster(
    map_img: Image.Image,
    title: str,
    subtitle: str,
    lat: float,
    lon: float,
    theme: str,
) -> Image.Image:
    size_px = map_img.size[0]
    band_h = int(size_px * 0.20)

    theme = (theme or "").lower().strip()
    if theme not in ("neon", "dark", "light"):
        theme = DEFAULT_THEME

    if theme == "light":
        band_color = (250, 250, 250, 255)
        text_color = (20, 20, 20, 255)
        sub_color = (80, 80, 80, 255)
    else:
        band_color = (8, 8, 10, 255)
        text_color = (235, 235, 240, 255)
        sub_color = (170, 170, 180, 255)

    canvas = Image.new("RGBA", (size_px, size_px + band_h), band_color)
    canvas.paste(map_img, (0, 0), map_img)

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, size_px, size_px, size_px + band_h], fill=band_color)

    # text
    title = (title or "").strip() or "YOUR PLACE"
    subtitle = (subtitle or "").strip()

    x_pad = int(size_px * 0.06)
    y0 = size_px + int(band_h * 0.16)

    title_font = _load_font(int(band_h * 0.36))
    sub_font = _load_font(int(band_h * 0.18))
    meta_font = _load_font(int(band_h * 0.16))
    attrib_font = _load_font(int(band_h * 0.13))

    draw.text((x_pad, y0), title.upper(), font=title_font, fill=text_color)

    y = y0 + int(band_h * 0.42)
    if subtitle:
        draw.text((x_pad, y), subtitle.upper(), font=sub_font, fill=sub_color)

    coords = f"{lat:.5f}, {lon:.5f}"
    draw.text((x_pad, size_px + band_h - int(band_h * 0.30)), coords, font=meta_font, fill=sub_color)

    # attribution (as you requested)
    attrib = "© OpenStreetMap contributors • MapTiler"
    draw.text((x_pad, size_px + band_h - int(band_h * 0.14)), attrib, font=attrib_font, fill=sub_color)

    return canvas.convert("RGB")


def parse_payload(payload: dict) -> Tuple[float, float, int, int, str, str, str]:
    address = payload.get("address")
    lat = payload.get("lat")
    lon = payload.get("lon") if payload.get("lon") is not None else payload.get("lng")

    zoom = payload.get("zoom", DEFAULT_ZOOM)
    size_px = payload.get("size", DEFAULT_SIZE)
    theme = payload.get("theme", DEFAULT_THEME)
    title = payload.get("title", "")
    subtitle = payload.get("subtitle", "")

    if lat is not None and lon is not None:
        lat = float(lat)
        lon = float(lon)
    elif address:
        lat, lon = geocode_nominatim(str(address))
        # be polite to Nominatim (avoid bursts)
        time.sleep(1.0)
    else:
        raise ValueError("Provide either address OR (lat + lon/lng)")

    zoom = int(float(zoom))
    size_px = int(float(size_px))

    zoom = max(0, min(zoom, 20))
    size_px = max(512, min(size_px, 4096))

    return lat, lon, zoom, size_px, str(theme), str(title), str(subtitle)


# -------- Routes --------
@app.get("/health")
def health():
    return f"ok {VERSION}"

@app.post("/render")
def render():
    payload = request.get_json(silent=True) or {}
    try:
        lat, lon, zoom, size_px, theme, title, subtitle = parse_payload(payload)

        map_img = fetch_map_maptiler(lat=lat, lon=lon, zoom=zoom, size_px=size_px)
        map_img = apply_theme(map_img, theme)
        poster = compose_poster(map_img, title, subtitle, lat, lon, theme)

        out = BytesIO()
        poster.save(out, format="PNG", optimize=True)
        out.seek(0)

        safe_name = (title.strip() or "poster").replace(" ", "_")[:40]
        return send_file(
            out,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"{safe_name}.png",
        )

    except requests.HTTPError as e:
        # surface provider error nicely (403/401 etc.)
        return jsonify({"error": f"Map provider error: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
