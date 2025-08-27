import os
import json
import textwrap
from typing import Any, Dict, List

from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

# ====== Config vía variables de entorno ======
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")  # Webhook de tu canal de Discord
TOKEN = os.environ.get("TOKEN")  # opcional: auth por query param ?token=...
HNP_URL_TEMPLATE = os.environ.get("HNP_URL_TEMPLATE")  # opcional: plantilla para linkear el elemento


app = FastAPI(title="HacknPlan → Discord bridge")


# ====== Helpers ======
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


def compute_url(payload: dict):
    """Construye una URL a partir de HNP_URL_TEMPLATE y el payload si está configurada."""
    if not HNP_URL_TEMPLATE:
        return None
    try:
        # Permite usar {ProjectId}, {DesignElementId}, etc. directamente
        return HNP_URL_TEMPLATE.format(**payload)
    except Exception:
        # Si faltan claves, no rompemos
        return None


def extract_fields(payload: dict):
    """
    Extrae campos de título, descripción, url y metadatos,
    cubriendo tanto esquemas 'data.*' como PascalCase de HacknPlan.
    """
    title = (
        get_in(payload, ["data", "title"])
        or get_in(payload, ["data", "name"])
        or payload.get("Name")
        or payload.get("Title")
        or payload.get("name")
        or payload.get("title")
    )

    desc = (
        get_in(payload, ["data", "summary"])
        or get_in(payload, ["data", "description"])
        or payload.get("Description")
        or payload.get("Summary")
        or payload.get("description")
        or payload.get("summary")
    )

    url = (
        get_in(payload, ["data", "url"])
        or get_in(payload, ["data", "webUrl"])
        or payload.get("Url")
        or payload.get("url")
        or compute_url(payload)
    )

    type_name = (
        get_in(payload, ["Type", "Name"])
        or get_in(payload, ["data", "type", "name"])
        or payload.get("TypeName")
    )

    design_element_id = payload.get("DesignElementId") or get_in(payload, ["data", "id"])
    project_id = payload.get("ProjectId") or get_in(payload, ["data", "projectId"])

    return title, desc, url, type_name, design_element_id, project_id


async def post_to_discord(event_type: str, payload: Dict[str, Any], raw_excerpt: str):
    title, desc, url, type_name, de_id, proj_id = extract_fields(payload)

    if not title and not desc:
        desc = f"Raw payload excerpt:\n{raw_excerpt}"

    embed = {
        "title": title or "Elemento de diseño",
        "description": shorten((desc or "—"), 1000),
        "url": url,
        "fields": [
            {"name": "Tipo", "value": (type_name or "—"), "inline": True},
            {"name": "DesignElementId", "value": str(de_id or "—"), "inline": True},
            {"name": "ProjectId", "value": str(proj_id or "—"), "inline": True},
        ],
    }

    body = {
        "content": f"🔔 **{event_type or 'Event'}**",
        "embeds": [embed],
    }

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json=body)
        r.raise_for_status()


async def parse_request(req: Request) -> Dict[str, Any]:
    """
    Soporta:
      - application/json (payload JSON directo)
      - application/x-www-form-urlencoded o multipart/form-data con 'payload' (string JSON)
      - fallback: cuerpo crudo en 'raw'
    """
    ctype = (req.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            return await req.json()
        except Exception:
            pass

    if "application/x-www-form-urlencoded" in ctype or "multipart/form-data" in ctype:
        try:
            form = await req.form()
            if "payload" in form:
                try:
                    return json.loads(form["payload"])
                except Exception:
                    return {"payload": str(form["payload"])}
            return {k: form[k] for k in form.keys()}
        except Exception:
            pass

    raw = await req.body()
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw.decode("utf-8", "ignore")}

    return {}


# ====== Endpoints ======
@app.post("/hacknplan")
async def hacknplan(req: Request, bg: BackgroundTasks):
    # Auth simple por query param: /hacknplan?token=XXXXX
    if TOKEN and req.query_params.get("token") != TOKEN:
        return Response("unauthorized", status_code=401)

    # Tipo de evento por header o por cuerpo
    event_type = req.headers.get("x-hacknplan-event")
    payload = await parse_request(req)
    event_type = event_type or payload.get("event") or payload.get("type") or "Unknown"

    # Resumen crudo del payload para depurar (se usa solo si no hay campos mapeados)
    try:
        raw_excerpt = shorten(
            textwrap.indent(json.dumps(payload, ensure_ascii=False, indent=2), "  "),
            900,
        )
    except Exception:
        raw_excerpt = "—"

    # Logs en Render (Panel → Logs)
    print("HNP event:", event_type)
    print("Payload keys:", list(payload.keys())[:20])

    # Responder rápido y enviar a Discord en background para evitar timeouts
    if DISCORD_WEBHOOK_URL:
        bg.add_task(post_to_discord, event_type, payload, raw_excerpt)

    return Response("OK", status_code=200)


@app.get("/healthz")
def healthz():
    return {"ok": True}
