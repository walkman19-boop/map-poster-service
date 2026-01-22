import os
import io
import uuid
from flask import Flask, request, jsonify
from google.cloud import storage
from PIL import Image, ImageDraw

app = Flask(__name__)

OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")
GCS_PREFIX = os.environ.get("GCS_PREFIX", "renders")


def upload_to_gcs(data: bytes, content_type: str, filename: str) -> str:
    bucket_name = os.environ["OUTPUT_BUCKET"]
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    blob = bucket.blob(filename)
    blob.upload_from_string(data, content_type=content_type)

    blob.make_public()
    return blob.public_url



@app.get("/health")
def health():
    return {"ok": True}


@app.post("/render")
def render():
    payload = request.get_json(silent=True) or {}

    title = payload.get("title", "ŽARĖNAI")
    subtitle = payload.get("subtitle", "TELŠIŲ R., LT")
    zoom = str(payload.get("zoom", "2500"))
    maps = str(payload.get("maps_link", "https://maps.google.com"))

    w, h = 1400, 1800
    img = Image.new("RGB", (w, h), (8, 10, 20))
    d = ImageDraw.Draw(img)

    # simple neon-ish "roads" pattern
    for x in range(50, w, 140):
        d.line([(x, 0), (x + 220, h)], fill=(0, 220, 255), width=2)

    # text
    d.text((80, h - 260), title.upper(), fill=(0, 220, 255))
    d.text((80, h - 210), subtitle.upper(), fill=(0, 220, 255))
    d.text((80, h - 170), f"ZOOM: {zoom}", fill=(120, 160, 180))
    d.text((80, h - 140), maps[:60] + ("..." if len(maps) > 60 else ""), fill=(120, 160, 180))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    filename = f"{uuid.uuid4().hex}.png"
    url = upload_to_gcs(png_bytes, "image/png", filename)

    return jsonify({"ok": True, "file": filename, "url": url})
