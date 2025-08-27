import os, json, textwrap
from typing import Any, Dict, List
from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
TOKEN = os.environ.get("TOKEN")  # opcional: /hacknplan?token=...

app = FastAPI(title="HNP → Discord bridge (robusto)")

def get_in(d: Dict[str, Any], path: List[str]):
    cur = d
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def pick(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def shorten(s: str, n: int = 900):
    s = s.strip()
    return s if len(s) <= n else (s[:n] + "…")

def extract_fields(payload: Dict[str, Any]):
    """
    Intentamos cubrir distintos esquemas (DesignElement/WorkItem/otros).
    """

    # Posibles títulos
    title = pick(
        get_in(payload, ["data", "title"]),
        get_in(payload, ["data", "name"]),
        get_in(payload, ["designElement", "title"]),
        get_in(payload, ["element", "title"]),
        get_in(payload, ["workItem", "title"]),
        payload.get("title"),
        payload.get("name"),
    )

    # Posibles descripciones/resúmenes
    desc = pick(
        get_in(payload, ["data", "summary"]),
        get_in(payload, ["data", "description"]),
        get_in(payload, ["data", "contentPlain"]),
        get_in(payload, ["designElement", "summary"]),
        get_in(payload, ["element", "summary"]),
        payload.get("summary"),
        payload.get("description"),
    )

    # Posibles URLs
    url = pick(
        get_in(payload, ["data", "url"]),
        get_in(payload, ["data", "webUrl"]),
        get_in(payload, ["links", "html"]),
        payload.get("url"),
    )

    return title, desc, url

async def post_to_discord(event_type: str, payload: Dict[str, Any], raw_excerpt: str):
    title, desc, url = extract_fields(payload)

    # Si no encontramos campos, mostramos un recorte del payload para depurar
    if not title and not desc:
        if not desc:
            desc = f"Raw payload excerpt:\n{raw_excerpt}"

    embed = {
        "title": title or "Evento de diseño",
        "description": shorten(desc or "—", 1000),
        "url": url
    }

    body = {
        "content": f"🔔 **{event_type or 'Event'}**",
        "embeds": [embed]
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json=body)
        r.raise_for_status()

async def parse_request(req: Request) -> Dict[str, Any]:
    """
    Soporta application/json y application/x-www-form-urlencoded con 'payload' como JSON string.
    """
    ctype = (req.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        # JSON directo
        try:
            return await req.json()
        except Exception:
            pass  # caemos a lectura cruda más abajo

    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        try:
            form = await req.form()
            # Algunos servicios mandan 'payload' (string JSON) o campos sueltos
            if "payload" in form:
                try:
                    return json.loads(form["payload"])  # string → dict
                except Exception:
                    return {"payload": str(form["payload"])}
            # Si no hay 'payload', devolvemos todo el form como dict
            return {k: form[k] for k in form.keys()}
        except Exception:
            pass

    # Fallback: leer bytes y tratar de parsear JSON; si no, guardar crudo
    raw = await req.body()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw.decode("utf-8", "ignore")}

    return {}

@app.post("/hacknplan")
async def hacknplan(req: Request, bg: BackgroundTasks):
    # Auth por query param
    if TOKEN and req.query_params.get("token") != TOKEN:
        return Response("unauthorized", status_code=401)

    # Tipo de evento por header o por cuerpo
    event_type = req.headers.get("x-hacknplan-event")
    payload = await parse_request(req)
    event_type = event_type or payload.get("event") or payload.get("type") or "Unknown"

    # Resumen del payload crudo para depuración en Discord si falta mapeo
    try:
        raw_excerpt = shorten(textwrap.indent(json.dumps(payload, ensure_ascii=False, indent=2), "  "), 900)
    except Exception:
        raw_excerpt = "—"

    # Log en Render
    print("HNP event:", event_type)
    print("Payload keys:", list(payload.keys())[:20])

    # Responder rápido a HNP y enviar a Discord en background
    if DISCORD_WEBHOOK_URL:
        bg.add_task(post_to_discord, event_type, payload, raw_excerpt)

    return Response("OK", status_code=200)

@app.get("/healthz")
def healthz():
    return {"ok": True}
