import os, json
from fastapi import FastAPI, Request, Response, HTTPException
import httpx

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN")  # opcional

app = FastAPI(title="HNP → Discord bridge")

def want_event(event_type: str) -> bool:
    # filtrá acá lo que te interesa; ej: solo Design Model
    return event_type.startswith("DesignElement")  # Created/Updated/Deleted

async def send_to_discord(event_type: str, payload: dict):
    title = payload.get("data", {}).get("title") or payload.get("title") or "Design element"
    url = payload.get("data", {}).get("url") or payload.get("url")
    desc = payload.get("data", {}).get("summary") or payload.get("summary") or ""
    if desc and len(desc) > 400:
        desc = desc[:400] + "…"

    discord_payload = {
        "content": f"🔔 **{event_type}**",
        "embeds": [{
            "title": title,
            "description": desc or "—",
            "url": url,
        }]
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json=discord_payload)
        r.raise_for_status()

@app.post("/hacknplan")
async def hacknplan(req: Request):
    # Validación opcional por token propio
    if INTERNAL_TOKEN:
        if req.headers.get("x-internal-token") != INTERNAL_TOKEN:
            raise HTTPException(status_code=401, detail="bad token")

    event_type = req.headers.get("x-hacknplan-event", "Unknown")
    try:
        payload = await req.json()
    except Exception:
        payload = {}

    # Filtrado de eventos
    if not want_event(event_type):
        return Response(content="ignored", status_code=200)

    # Reenvío a Discord (sin romper el webhook si falla)
    try:
        await send_to_discord(event_type, payload)
    except Exception as e:
        # devolvemos 200 para que HacknPlan no reintente en loop; logueá en Render
        print("Discord error:", e)

    return Response(content="OK", status_code=200)

@app.get("/healthz")
def healthz():
    return {"ok": True}
