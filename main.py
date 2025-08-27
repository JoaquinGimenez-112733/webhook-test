import os, json
from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
TOKEN = os.environ.get("TOKEN")  # secreto opcional via ?token=...

app = FastAPI(title="HNP → Discord bridge (compat)")

async def send_to_discord(event_type: str, payload: dict):
    title = (payload.get("data", {}) or {}).get("title") or payload.get("title") or "Design element"
    url = (payload.get("data", {}) or {}).get("url") or payload.get("url")
    desc = (payload.get("data", {}) or {}).get("summary") or payload.get("summary") or ""
    if isinstance(desc, str) and len(desc) > 400:
        desc = desc[:400] + "…"

    discord_payload = {
        "content": f"🔔 **{event_type or 'Event'}**",
        "embeds": [{"title": title, "description": desc or "—", "url": url}]
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(DISCORD_WEBHOOK_URL, json=discord_payload)

@app.post("/hacknplan")
async def hacknplan(req: Request, bg: BackgroundTasks):
    # Auth por querystring: /hacknplan?token=XXXXX
    if TOKEN:
        if req.query_params.get("token") != TOKEN:
            return Response("unauthorized", status_code=401)

    # Headers y body (tolerante a cualquier content-type)
    event_type = req.headers.get("x-hacknplan-event", "Unknown")
    body_bytes = await req.body()
    try:
        payload = json.loads(body_bytes) if body_bytes else {}
    except Exception:
        payload = {"raw": body_bytes.decode("utf-8", "ignore")}

    # Log mínimo en Render (Settings → Logs)
    print("HNP event:", event_type, "| payload keys:", list(payload.keys()))

    # Enviar a Discord en background (NO bloquea la respuesta a HNP)
    if DISCORD_WEBHOOK_URL:
        bg.add_task(send_to_discord, event_type, payload)

    # Responder rápido 200 para evitar reintentos de HNP
    return Response("OK", status_code=200)

@app.get("/healthz")
def healthz():
    return {"ok": True}
