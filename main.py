import os
import json
import textwrap
from typing import Any, Dict, List, Tuple

from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

# ====== Config vía variables de entorno ======
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")  # Webhook de tu canal de Discord
TOKEN = os.environ.get("TOKEN")  # opcional: auth por query param ?token=...
HNP_URL_TEMPLATE = os.environ.get("HNP_URL_TEMPLATE")  # ej: https://app.hacknplan.com/p/{ProjectId}/gamemodel?nodeId={DesignElementId}&nodeTabId=basicinfo
NOTIF_LOCALE = os.environ.get("NOTIF_LOCALE", "es")  # "es" o "en"

app = FastAPI(title="HacknPlan → Discord bridge")

# ====== Diccionarios para humanizar el evento ======
EMOJI = {"created": "➕", "updated": "✏️", "deleted": "🗑️"}
NOUN_MAP_ES = {"designelement": "Elemento de diseño", "workitem": "Tarea"}
NOUN_MAP_EN = {"designelement": "Design element", "workitem": "Work item"}

# Sinónimos/variantes de acción que podrían venir desde HNP
ACTION_SYNONYMS = {
    "created": {"created", "create", "added", "add", "new"},
    "updated": {"updated", "update", "changed", "change", "modified", "modify", "edit", "edited"},
    "deleted": {"deleted", "delete", "removed", "remove", "archived", "archive"},
}

def normalize_action(action_raw: str) -> str:
    a = (action_raw or "").lower()
    for canonical, variants in ACTION_SYNONYMS.items():
        if a in variants:
            return canonical
    return a or ""

def split_and_normalize_event(event_type: str) -> Tuple[str, str]:
    """Devuelve (kind_lower, action_canonical) a partir de 'DesignElement.Deleted', 'WorkItem.Removed', etc."""
    et = (event_type or "").strip()
    if "." in et:
        kind, action = et.split(".", 1)
    else:
        kind, action = et, ""
    return (kind.lower(), normalize_action(action))

def format_event(event_type: str, type_name: str | None, locale: str = "es", element_name: str | None = None) -> str:
    kind_l, action_norm = split_and_normalize_event(event_type)
    noun_map = NOUN_MAP_ES if locale == "es" else NOUN_MAP_EN
    base_noun = noun_map.get(kind_l, ("Evento" if locale == "es" else "Event"))
    noun = type_name or base_noun
    emoji = EMOJI.get(action_norm, "🔔")

    if locale == "es":
        if action_norm == "created":
            label = f"Nuevo {noun}"
        elif action_norm == "deleted":
            label = f"{noun} eliminado"
        elif action_norm == "updated":
            label = f"{noun} actualizado"
        else:
            label = f"{noun} ({event_type or 'Evento'})"
    else:
        if action_norm == "created":
            label = f"New {noun}"
        elif action_norm == "deleted":
            label = f"{noun} deleted"
        elif action_norm == "updated":
            label = f"{noun} updated"
        else:
            label = f"{noun} ({event_type or 'Event'})"

    if element_name:
        label = f"{label}: {element_name}"

    return f"{emoji} **{label}**"


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
    cubriendo tanto esquemas 'data.*' como el PascalCase de HacknPlan.
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
    parent_name = get_in(payload, ["Parent", "Name"])  # puede venir null

    return title, desc, url, type_name, design_element_id, project_id, parent_name


async def post_to_discord(event_type: str, payload: Dict[str, Any], raw_excerpt: str):
    title, desc, url, type_name, de_id, proj_id, parent_name = extract_fields(payload)

    # Fallback de descripción
    if not (isinstance(desc, str) and desc.strip()):
        desc = "Sin descripción."

    # Si es un borrado, no enlazamos URL (suele 404)
    _, action_norm = split_and_normalize_event(event_type)
    if action_norm == "deleted":
        url = None

    embed = {
        "title": title or "Elemento de diseño",
        "description": shorten(desc, 1000),
        "url": url,
        "fields": [
            {"name": "Tipo", "value": (type_name or "—"), "inline": True},
            {"name": "DesignElementId", "value": str(de_id or "—"), "inline": True},
            {"name": "ProjectId", "value": str(proj_id or "—"), "inline": True},
        ],
    }

    if parent_name:
        embed["fields"].append({"name": "Parent", "value": parent_name, "inline": True})

    content = format_event(event_type, type_name, NOTIF_LOCALE, element_name=title)

    body = {"content": content, "embeds": [embed]}

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
@app.api_route("/hacknplan", methods=["POST", "DELETE"])
async def hacknplan(req: Request, bg: BackgroundTasks):
    # Auth simple por query param: /hacknplan?token=XXXXX
    if TOKEN and req.query_params.get("token") != TOKEN:
        return Response("unauthorized", status_code=401)

    # Tipo de evento por header o por cuerpo
    event_type = req.headers.get("x-hacknplan-event")
    payload = await parse_request(req)
    event_type = event_type or payload.get("event") or payload.get("type") or "Unknown"

    # Resumen crudo del payload (para logs)
    try:
        raw_excerpt = shorten(
            textwrap.indent(json.dumps(payload, ensure_ascii=False, indent=2), "  "),
            900,
        )
    except Exception:
        raw_excerpt = "—"

    # Logs en Render
    print("HNP method:", req.method)
    print("HNP event:", event_type)
    print("Payload keys:", list(payload.keys())[:20])

    # Responder rápido y enviar a Discord en background para evitar timeouts
    if DISCORD_WEBHOOK_URL:
        bg.add_task(post_to_discord, event_type, payload, raw_excerpt)

    return Response("OK", status_code=200)


@app.get("/healthz")
def healthz():
    return {"ok": True}
