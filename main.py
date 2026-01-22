import os
import math
import time
from io import BytesIO
from typing import Optional, Tuple

import requests
from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)

# ---------- Config ----------
USER_AGENT = os.environ.get("USER_AGENT", "map-poster-service/1.0 (contact: you@domain.com)")
DEFAULT_SIZE = int(os.environ.get("DEFAULT_SIZE", "2048"))  # output px (square)
DEFAULT_THEME = os.environ.get("DEFAULT_THEME", "dark")     # dark | neon | light
DEFAULT_ZOOM = int(os.environ.get("DEFAULT_ZOOM", "12"))    # OSM tile zoom 0..19

# Uses OSM static map endpoint for MVP testing.
# For production: use a paid provider (MapTiler/Mapbox/Stadia/etc.) or your own tiles.
STATIC_MAP_URL = os.environ.get("STATIC_MAP_URL", "https://staticmap.openstreetmap.de/staticmap.php")

# Optional font path (put font file into repo and set env FONT_PATH)
FONT_PATH = os.environ.get("FONT_PATH", "")


# ---------- Helpers ----------
def _load_font(size: int) -> ImageFont.ImageFont:
    if FONT_PATH and os.path.exists(FONT_PATH):
        return ImageFont.truetype(FONT_PATH, size=size)
    # fallback
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


def fetch_static_map(lat: float, lon: float, zoom: int, size_px: int) -> Image.Image:
    # staticmap.openstreetmap.de expects width/height <= ~2048. We'll clamp.
    w = max(256, min(size_px, 2048))
    h = max(256, min(size_px, 2048))

    r = requests.get(
        STATIC_MAP_URL,
        params={
            "center": f"{lat},{lon}",
            "zoom": str(zoom),
            "size": f"{w}x{h}",
            "maptype": "mapnik",
            # no marker by default
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    r.raise_for_status()
    img = Image.open(BytesIO(r.content)).convert("RGBA")
    # upscale to requested size (for poster composition) if needed
    if (w, h) != (size_px, size_px):
        img = img.resize((size_px, size_px), Image.LANCZOS)
    return img


def apply_theme(map_img: Image.Image, theme: str) -> Image.Image:
    theme = (theme or "").lower().strip()
    if theme not in ("dark", "neon", "light"):
        theme = DEFAULT_THEME

    img = map_img.copy()

    if theme == "light":
        # very light touch
        return img

    # dark / neon: darken background & boost contrast
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 130 if theme == "dark" else 160))
    img = Image.alpha_composite(img, overlay)

    if theme == "neon":
        # add subtle color shift to mimic neon vibe
        r, g, b, a = img.split()
        # boost blues/pinks slightly
        g = g.point(lambda x: min(255, int(x * 0.95)))
        b = b.point(lambda x: min(255, int(x * 1.10)))
        img = Image.merge("RGBA", (r, g, b, a))

    return img


def format_coords(lat: float, lon: float) -> str:
    # simple, readable
    return f"{lat:.5f}, {lon:.5f}"


def compose_poster(
    lat: float,
    lon: float,
    zoom: int,
    title: str,
    subtitle: str,
    theme: str,
    size_px: int,
) -> Image.Image:
    # Poster canvas: map square + bottom text band
    band_h = int(size_px * 0.20)
    canvas = Image.new("RGBA", (size_px, size_px + band_h), (10, 10, 10, 255) if theme in ("dark", "neon") else (250, 250, 250, 255))

    # map
    map_img = fetch_static_map(lat, lon, zoom, size_px)
    map_img = apply_theme(map_img, theme)
    canvas.paste(map_img, (0, 0), map_img)

    # text band
    draw = ImageDraw.Draw(canvas)
    band_y0 = size_px

    if theme == "light":
        band_color = (250, 250, 250, 255)
        text_color = (20, 20, 20, 255)
        sub_color = (80, 80, 80, 255)
    else:
        band_color = (8, 8, 10, 255) if theme == "dark" else (6, 6, 12, 255)
        text_color = (235, 235, 240, 255)
        sub_color = (170, 170, 180, 255)

    draw.rectangle([0, band_y0, size_px, size_px + band_h], fill=band_color)

    # fonts
    title_font = _load_font(int(band_h * 0.36))
    sub_font = _load_font(int(band_h * 0.18))
    coord_font = _load_font(int(band_h * 0.16))

    # text
    title = (title or "").strip() or "YOUR PLACE"
    subtitle = (subtitle or "").strip()

    x_pad = int(size_px * 0.06)
    y = band_y0 + int(band_h * 0.18)

    draw.text((x_pad, y), title.upper(), font=title_font, fill=text_color)
    y += int(band_h * 0.42)

    if subtitle:
        draw.text((x_pad, y), subtitle.upper(), font=sub_font, fill=sub_color)
        y += int(band_h * 0.22)

    coords = format_coords(lat, lon)
    draw.text((x_pad, band_y0 + band_h - int(band_h * 0.28)), coords, font=coord_font, fill=sub_color)

    return canvas.convert("RGB")


def parse_payload(payload: dict) -> Tuple[float, float, int, str, str, str, int]:
    # Accept:
    # - lat/lng (or lon)
    # - address
    # - zoom (tile zoom 0..19)
    # - theme
    # - size
    lat = payload.get("lat")
    lng = payload.get("lng")
    lon = payload.get("lon")
    address = payload.get("address")

    zoom = payload.get("zoom", DEFAULT_ZOOM)
    theme = payload.get("theme", DEFAULT_THEME)
    size_px = int(payload.get("size", DEFAULT_SIZE))

    title = payload.get("title", "")
    subtitle = payload.get("subtitle", "")

    if lat is not None and (lng is not None or lon is not None):
        lat = float(lat)
        lng = float(lng if lng is not None else lon)
    elif address:
        lat, lng = geocode_nominatim(str(address))
        # be friendly to Nominatim
        time.sleep(1.0)
    else:
        raise ValueError("Provide either (lat + lng) OR address")

    # zoom clamp
    try:
        zoom = int(float(zoom))
    except Exception:
        zoom = DEFAULT_ZOOM
    zoom = max(0, min(19, zoom))

    # size clamp (Render memory)
    size_px = max(512, min(size_px, 4096))

    return lat, lng, zoom, str(title), str(subtitle), str(theme), size_px


# ---------- Routes ----------
@app.get("/health")
def health():
    return "ok"

@app.post("/render")
def render():
    payload = request.get_json(silent=True) or {}
    try:
        lat, lng, zoom, title, subtitle, theme, size_px = parse_payload(payload)
        img = compose_poster(
            lat=lat,
            lon=lng,
            zoom=zoom,
            title=title,
            subtitle=subtitle,
            theme=theme,
            size_px=size_px,
        )

        out = BytesIO()
        img.save(out, format="PNG", optimize=True)
        out.seek(0)

        filename = (title.strip() or "poster").replace(" ", "_")[:40]
        return send_file(
            out,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"{filename}.png",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
