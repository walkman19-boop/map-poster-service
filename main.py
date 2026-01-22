import io
import os
import uuid
from datetime import timedelta

from flask import Flask, Response, jsonify, request
from google.cloud import storage
from PIL import Image, ImageDraw


app = Flask(__name__)

# REQUIRED
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "").strip()

# OPTIONAL
GCS_PREFIX = os.environ.get("GCS_PREFIX", "renders").strip().strip("/")

# OPTIONAL: set to true ONLY if you really configured signing credentials
SIGN_URLS = os.environ.get("SIGN_URLS", "").strip().lower() in ("1", "true", "yes")
SIGNED_URL_EXP_SECONDS = int(os.environ.get("SIGNED_URL_EXP_SECONDS", "3600"))


def require_bucket():
    if not OUTPUT_BUCKET:
        raise RuntimeError("Missing env var OUTPUT_BUCKET (set it in Cloud Run -> Variables).")


def upload_to_gcs(data: bytes, content_type: str, filename: str) -> str:
    require_bucket()

    client = storage.Client()
    bucket = client.bucket(OUTPUT_BUCKET)

    object_name = f"{GCS_PREFIX}/{filename}" if GCS_PREFIX else filename
    blob = bucket.blob(object_name)

    blob.upload_from_string(data, content_type=content_type)

    # DEFAULT (recommended): public URL (bucket must allow public read via IAM)
    if not SIGN_URLS:
        return blob.public_url

    # Signed URL mode (requires credentials that can sign)
    return blob.generate_signed_url(
        expiration=timedelta(seconds=SIGNED_URL_EXP_SECONDS),
        method="GET",
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/docs")
def docs():
    base = request.host_url.rstrip("/")
    html = f"""
    <h1>Map Poster Service</h1>
    <p>Status: OK</p>

    <h2>Endpoints</h2>
    <ul>
      <li><b>GET</b> <code>/health</code></li>
      <li><b>POST</b> <code>/render</code></li>
    </ul>

    <h2>Example JSON</h2>
    <pre>{{
  "maps_link": "https://www.google.com/maps/@54.707849,25.3968932,16z",
  "zoom": 2500,
  "output": "PNG",
  "title": "Pupoja",
  "subtitle": "Vilnius"
}}</pre>

    <h2>PowerShell test</h2>
    <pre>
$uri = "{base}/render"
$body = @{{
  maps_link = "https://www.google.com/maps/@54.707849,25.3968932,16z"
  zoom      = 2500
  output    = "PNG"
  title     = "Pupoja"
  subtitle  = "Vilnius"
}} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri $uri -ContentType "application/json" -Body $body
    </pre>

    <h2>curl.exe test (Windows)</h2>
    <pre>
curl.exe -X POST "{base}/render" -H "Content-Type: application/json" -d "{{\\"maps_link\\":\\"https://www.google.com/maps/@54.707849,25.3968932,16z\\",\\"zoom\\":2500,\\"output\\":\\"PNG\\",\\"title\\":\\"Pupoja\\",\\"subtitle\\":\\"Vilnius\\"}}"
    </pre>

    <h3>Notes</h3>
    <ul>
      <li>Atidarius <code>/render</code> naršyklėje gausi <b>Method Not Allowed</b>, nes reikia <b>POST</b>.</li>
      <li>Jei <b>SIGN_URLS=true</b> – reikės signing cred (private key). Paprasčiausia: palik false.</li>
    </ul>
    """
    return Response(html, mimetype="text/html")


@app.post("/render")
def render():
    try:
        require_bucket()
        payload = request.get_json(silent=True) or {}

        maps_link = str(payload.get("maps_link", "")).strip()
        zoom = str(payload.get("zoom", "2500")).strip()
        output = str(payload.get("output", "PNG")).strip().upper()
        title = str(payload.get("title", "")).strip()
        subtitle = str(payload.get("subtitle", "")).strip()

        if not maps_link or len(maps_link) < 8:
            return jsonify({"ok": False, "error": "maps_link is required"}), 400

        if output not in ("PNG", "PDF"):
            output = "PNG"

        # DEMO image (replace with real map rendering later)
        w, h = 1400, 1800
        img = Image.new("RGB", (w, h), (8, 10, 20))
        d = ImageDraw.Draw(img)

        for x in range(50, w, 140):
            d.line([(x, 0), (x + 220, h)], width=2)

        if title:
            d.text((80, h - 260), title.upper())
        if subtitle:
            d.text((80, h - 220), subtitle.upper())

        d.text((80, h - 170), f"ZOOM: {zoom}")
        d.text((80, h - 140), maps_link[:80] + ("..." if len(maps_link) > 80 else ""))

        buf = io.BytesIO()
        if output == "PDF":
            img.save(buf, format="PDF")
            data = buf.getvalue()
            filename = f"{uuid.uuid4().hex}.pdf"
            url = upload_to_gcs(data, "application/pdf", filename)
        else:
            img.save(buf, format="PNG")
            data = buf.getvalue()
            filename = f"{uuid.uuid4().hex}.png"
            url = upload_to_gcs(data, "image/png", filename)

        return jsonify({"ok": True, "file": filename, "url": url})

    except Exception as e:
        app.logger.exception("Exception in /render")
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
