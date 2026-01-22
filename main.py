import os
import io
import uuid
from datetime import timedelta

from flask import Flask, request, jsonify, Response
from google.cloud import storage
from PIL import Image, ImageDraw

app = Flask(__name__)

# REQUIRED: bucket name where files will be saved
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "").strip()

# OPTIONAL: folder prefix inside bucket
GCS_PREFIX = os.environ.get("GCS_PREFIX", "renders").strip().strip("/")

# OPTIONAL: if you want signed URLs instead of public bucket
# Leave empty for public bucket URL mode
SIGN_URLS = os.environ.get("SIGN_URLS", "").strip().lower() in ("1", "true", "yes")
SIGNED_URL_EXP_SECONDS = int(os.environ.get("SIGNED_URL_EXP_SECONDS", "3600"))


def _require_bucket():
    if not OUTPUT_BUCKET:
        raise RuntimeError("Missing env var OUTPUT_BUCKET (set it in Cloud Run -> Variables).")


def upload_to_gcs(data: bytes, content_type: str, filename: str) -> str:
    """
    Uploads bytes to GCS and returns a URL.

    If SIGN_URLS=true -> returns signed URL (requires service account key / proper signing setup).
    Otherwise -> returns public_url (requires bucket IAM public access).
    """
    _require_bucket()

    client = storage.Client()
    bucket = client.bucket(OUTPUT_BUCKET)

    # Put into optional folder prefix
    object_name = f"{GCS_PREFIX}/{filename}" if GCS_PREFIX else filename

    blob = bucket.blob(object_name)
    blob.upload_from_string(data, content_type=content_type)

    if SIGN_URLS:
        # Signed URLs require credentials that can sign (private key).
        return blob.generate_signed_url(
            expiration=timedelta(seconds=SIGNED_URL_EXP_SECONDS),
            method="GET",
        )

    # Public mode: bucket IAM should allow allUsers -> Storage Object Viewer
    return blob.public_url


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/docs")
def docs():
    html = f"""
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Map Poster Service</title></head>
<body style="font-family:Arial, sans-serif; line-height:1.4; padding:20px; max-width:900px;">
  <h1>Map Poster Service</h1>
  <p><b>Status:</b> OK</p>
  <p><b>POST</b> <code>/render</code> – generates a PNG and uploads to GCS.</p>

  <h2>Example payload</h2>
  <pre style="background:#f6f6f6; padding:12px; border-radius:8px;">{{
  "maps_link": "https://www.google.com/maps/@54.707849,25.3968932,16z",
  "zoom": 2500,
  "output": "PNG",
  "title": "Pupoja",
  "subtitle": "Vilnius"
}}</pre>

  <h2>PowerShell test</h2>
  <pre style="background:#f6f6f6; padding:12px; border-radius:8px;">$uri = "{request.host_url.rstrip('/')}/render"

$body = @{{
  maps_link = "https://www.google.com/maps/@54.707849,25.3968932,16z"
  zoom      = 2500
  output    = "PNG"
  title     = "Pupoja"
  subtitle  = "Vilnius"
}} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body $body</pre>

  <h2>Notes</h2>
  <ul>
    <li>If you use public URLs: bucket must allow <code>allUsers</code> → <code>Storage Object Viewer</code>.</li>
    <li>If you enable signed URLs (<code>SIGN_URLS=true</code>): service must run with credentials that can sign URLs.</li>
  </ul>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


@app.post("/render")
def render():
    try:
        _require_bucket()

        payload = request.get_json(silent=True) or {}

        maps_link = str(payload.get("maps_link", "")).strip()
        zoom = str(payload.get("zoom", "2500")).strip()
        output = str(payload.get("output", "PNG")).strip().upper()
        title = str(payload.get("title", "")).strip()
        subtitle = str(payload.get("subtitle", "")).strip()

        # basic validation
        if not maps_link or len(maps_link) < 8:
            return jsonify({"ok": False, "error": "maps_link is required"}), 400

        if output not in ("PNG", "PDF"):
            output = "PNG"

        # --- DEMO IMAGE GENERATION (replace later with real map rendering) ---
        w, h = 1400, 1800
        img = Image.new("RGB", (w, h), (8, 10, 20))
        d = ImageDraw.Draw(img)

        # simple pattern
        for x in range(50, w, 140):
            d.line([(x, 0), (x + 220, h)], width=2)

        # text
        if title:
            d.text((80, h - 260), title.upper())
        if subtitle:
            d.text((80, h - 220), subtitle.upper())

        d.text((80, h - 170), f"ZOOM: {zoom}")
        d.text((80, h - 140), maps_link[:80] + ("..." if len(maps_link) > 80 else ""))

        # Export
        if output == "PDF":
            buf = io.BytesIO()
            img.save(buf, format="PDF")
            data = buf.getvalue()
            filename = f"{uuid.uuid4().hex}.pdf"
            url = upload_to_gcs(data, "application/pdf", filename)
            return jsonify({"ok": True, "file": filename, "url": url})

        # default PNG
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        filename = f"{uuid.uuid4().hex}.png"
        url = upload_to_gcs(data, "image/png", filename)

        return jsonify({"ok": True, "file": filename, "url": url})

    except Exception as e:
        # show readable error in response + logs
        app.logger.exception("Exception in /render")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    # Local dev only. Cloud Run uses PORT env var automatically via container command.
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
