# Map poster service (Cloud Run)

## Health check
GET `/health`

## Render
POST `/render`

Example JSON:
```json
{
  "title": "ŽARĖNAI",
  "subtitle": "TELŠIŲ R., LT",
  "zoom": "2500",
  "maps_link": "https://maps.google.com/..."
}
```
