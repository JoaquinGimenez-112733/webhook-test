import os
import json
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

# ================== Config ==================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")      # Webhook del canal en Discord
TOKEN = os.environ.get("TOKEN")                                   # ?token=...
HNP_URL_TEMPLATE = os.environ.get("HNP_URL_TEMPLATE")             # ej: https://app.hacknplan.com/p/{ProjectId}/gamemodel?nodeId={DesignElementId}&nodeTabId=basicinfo
NOTIF_LOCALE = os.environ.get("NOTIF_LOCALE", "es")               # "es" | "en"

app = FastAPI(title="HacknPlan → Discord bridge")

# ================== Humanización ==================
EMOJI = {"created": "➕", "updated": "✏️", "deleted": "🗑️"}
NOUN_ES = {"designelement": "Elemento de diseño", "workitem": "Tarea"}
NOUN_EN = {"designelement": "Design element", "workitem": "Work item"}

ACTION_SYNONYMS = {
    "created": {"created", "create", "added", "add", "new"},
    "updated": {"updated", "update", "changed", "change", "modified", "modify", "edit", "edited"},
    # Incluimos "archived" como borrado lógico
    "deleted": {"deleted", "delete", "removed", "remove", "archived", "archive"},
}

def normalize_action(raw: str) -> str:
    a = (raw or "").lower()
    for canon, variants in ACTION_SYNONYMS.items():
        if a in variants:
            return canon
    return a or ""

def split_event(event_type: str) -> Tuple[str, str]:
    et = (event_type or "").strip()
    for sep in (".", "_", "-"):
        if sep in et:
            k, a = et.split(sep, 1)
            return k.lower(), normalize_action(a)
    return et.lower(), ""

def format_content(event_type: str, type_name: Optional[str], element_name: Optional[str], actor: Optional[str]) -> str:
    kind_l, action = split_event(event_type)
    noun = (NOUN_ES if NOTIF_LOCALE == "es" else NOUN_EN).get(
        kind_l, "Evento" if NOTIF_LOCALE == "es" else "Event"
    )
    noun = type_name or noun
    emoji = EMOJI.get(action, "🔔")

    if NOTIF_LOCALE == "es":
        label = (
            f"Nuevo {noun}" if action == "created" else
            f"{noun} actualizado" if action == "updated" else
            f"{noun} eliminado" if action == "deleted" else
            f"{noun} ({event_type or 'Evento'})"
        )
    else:
        label = (
            f"New {noun}" if action == "created" else
            f"{noun} updated" if action == "updated" else
            f"{noun} deleted" if action == "deleted" else
            f"{noun} ({event_type or 'Event'})"
        )

    if element_name:
        label = f"{label}: {element_name}"
    if actor:
        label = f"{label} — por {actor}" if NOTIF_LOCALE == "es" else f"{label} — by {actor}"

    return f"{emoji} **{label}**"

# ================== Utils ==================
def get_in(d: Dict[str, Any], path: List[str]):
    cur = d
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur

def pick_str(*vals):
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def shorten(s: str, n: int = 900):
    s = s.strip()
    return s if len(s) <= n else (s[:n] + "…")

def compute_url(payload: dict) -> Optional[str]:
    if not HNP_URL_TEMPLATE:
        return None
    try:
        return HNP_URL_TEMPLATE.format(**payload)
    except Exception:
        return None

# ================== Extractores ==================
# Ajustado a tu payload real: User.User.Name / User.User.Username
ACTOR_PATHS: List[List[str]] = [
    ["User", "User", "Name"],
    ["User", "User", "Username"],
    ["User", "Name"],
    ["UpdatedBy", "Name"],
    ["ChangedBy", "Name"],
    ["Author", "Name"],
    ["UserName"],
    ["Username"],
]

def extract_actor(p: dict) -> Optional[str]:
    for path in ACTOR_PATHS:
        val = get_in(p, path) if len(path) > 1 else p.get(path[0])
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None

def extract_fields(p: dict):
    """Adaptado a payload PascalCase típico de HNP + fallback data.* si existiera."""
    title = pick_str(
        get_in(p, ["data", "title"]),
        get_in(p, ["data", "name"]),
        p.get("Name"), p.get("Title"), p.get("name"), p.get("title"),
    )

    desc = pick_str(
        get_in(p, ["data", "summary"]),
        get_in(p, ["data", "description"]),
        p.get("Description"), p.get("Summary"), p.get("description"), p.get("summary"),
    ) or ("Sin descripción." if NOTIF_LOCALE == "es" else "No description.")

    type_name = pick_str(
        get_in(p, ["Type", "Name"]),
        get_in(p, ["data", "type", "name"]),
        p.get("TypeName"),
    )

    project_id = p.get("ProjectId") or get_in(p, ["data", "projectId"])
    design_element_id = p.get("DesignElementId") or get_in(p, ["data", "id"])

    url = pick_str(
        get_in(p, ["data", "url"]),
        get_in(p, ["data", "webUrl"]),
        p.get("Url"), p.get("url"),
    ) or compute_url(p)

    parent_name = get_in(p, ["Parent", "Name"])

    # Flag para tratar "archivado" como borrado lógico (sin link)
    archived = str(p.get("Archived", "")).lower() in ("true", "1", "yes") or \
               str(p.get("IsArchived", "")).lower() in ("true", "1", "yes")

    return title, desc, url, type_name, project_id, design_element_id, parent_name, archived

# ================== Discord ==================
async def post_to_discord(event_type: str, payload: Dict[str, Any]):
    title, desc, url, type_name, proj_id, de_id, parent_name, archived = extract_fields(payload)
    actor = extract_actor(payload)

    # Si es borrado o archivado, no enlazamos (suele 404)
    _, action = split_event(event_type)
    if action == "deleted" or archived:
        url = None

    embed = {
        "title": title or ("Elemento de diseño" if NOTIF_LOCALE == "es" else "Design element"),
        "description": shorten(desc, 1000),
        "url": url,
        "fields": [
            {"name": "Tipo", "value": (type_name or "—"), "inline": True},
            {"name": "ProjectId", "value": str(proj_id or "—"), "inline": True},
            {"name": "DesignElementId", "value": str(de_id or "—"), "inline": True},
        ],
    }
    if parent_name:
        embed["fields"].append({"name": "Parent", "value": parent_name, "inline": True})
    if actor:
        embed["fields"].append({"name": "Responsable" if NOTIF_LOCALE=="es" else "Actor",
                                "value": actor, "inline": True})

    content = format_content(event_type, type_name, title, actor)

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(DISCORD_WEBHOOK_URL, json={"content": content, "embeds": [embed]})
        r.raise_for_status()

# ================== HTTP ==================
async def parse_request(req: Request) -> Dict[str, Any]:
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

@app.post("/hacknplan")
async def hacknplan(req: Request, bg: BackgroundTasks):
    if TOKEN and req.query_params.get("token") != TOKEN:
        return Response("unauthorized", status_code=401)

    # El tipo de evento viene en el header (oficial) o en el body (fallback)
    event_type = req.headers.get("x-hacknplan-event")
    payload = await parse_request(req)
    if not event_type:
        event_type = payload.get("Event") or payload.get("Type") or "DesignElement.Updated"

    # Log mínimo
    actor = extract_actor(payload)
    print("HNP event:", event_type, "| actor:", actor)

    if DISCORD_WEBHOOK_URL:
        bg.add_task(post_to_discord, event_type, payload)

    return Response("OK", status_code=200)

@app.get("/healthz")
def healthz():
    return {"ok": True}
