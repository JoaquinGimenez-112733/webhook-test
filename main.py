import os
import json
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, Request, Response, BackgroundTasks
import httpx

# ================== Config ==================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")      # Webhook del canal en Discord
TOKEN = os.environ.get("TOKEN")                                   # ?token=...
HNP_URL_TEMPLATE = os.environ.get("HNP_URL_TEMPLATE")             # DesignModel: https://app.hacknplan.com/p/{ProjectId}/gamemodel?nodeId={DesignElementId}&nodeTabId=basicinfo
HNP_BOARD_URL_TEMPLATE = os.environ.get("HNP_BOARD_URL_TEMPLATE") # Boards:     https://app.hacknplan.com/p/{ProjectId}/kanban?categoryId={CategoryId}&boardId={BoardId}
NOTIF_LOCALE = os.environ.get("NOTIF_LOCALE", "es")               # "es" | "en"

app = FastAPI(title="HacknPlan → Discord bridge")

# ================== Humanización ==================
EMOJI = {"created": "➕", "updated": "✏️", "deleted": "🗑️"}
NOUN_ES = {"designelement": "Elemento de diseño", "workitem": "Tarea"}
NOUN_EN = {"designelement": "Design element", "workitem": "Work item"}

ACTION_SYNONYMS = {
    "created": {"created", "create", "added", "add", "new"},
    "updated": {"updated", "update", "changed", "change", "modified", "modify", "edit", "edited"},
    "deleted": {"deleted", "delete", "removed", "remove", "archived", "archive"},
}

# Mapeo de StageId de WorkItem -> (etiqueta, emoji)
WORK_STAGE_MAP = {
    1: ("Planificada", "📝"),
    2: ("En progreso", "⏳"),
    3: ("Testeandose", "🧪"),
    4: ("Completada", "✅"),
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
def get_in(d: Union[Dict[str, Any], List[Any]], path: List[Union[str, int]]):
    cur: Any = d
    for k in path:
        if isinstance(cur, dict) and isinstance(k, str) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list) and isinstance(k, int) and 0 <= k < len(cur):
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

def compute_design_url(payload: dict) -> Optional[str]:
    if not HNP_URL_TEMPLATE:
        return None
    try:
        return HNP_URL_TEMPLATE.format(**payload)
    except Exception:
        return None

def compute_board_url(payload: dict) -> Optional[str]:
    if not HNP_BOARD_URL_TEMPLATE:
        return None
    try:
        ctx = {
            "ProjectId": payload.get("ProjectId"),
            "BoardId": get_in(payload, ["Board", "BoardId"]) or payload.get("BoardId"),
            "CategoryId": get_in(payload, ["Category", "CategoryId"]) or payload.get("CategoryId") or 0,
        }
        return HNP_BOARD_URL_TEMPLATE.format(**ctx)
    except Exception:
        return None

# ================== Extractores ==================
ACTOR_PATHS: List[List[Union[str, int]]] = [
    # DesignElement payloads
    ["User", "User", "Name"],
    ["User", "User", "Username"],
    ["User", "Name"],
    ["User", "Username"],
    ["UpdatedBy", "Name"],
    ["ChangedBy", "Name"],
    ["Author", "Name"],
    # WorkItem payloads
    ["AssignedUsers", 0, "User", "Name"],
    ["AssignedUsers", 0, "User", "Username"],
]

def extract_actor(p: dict) -> Optional[str]:
    for path in ACTOR_PATHS:
        val = get_in(p, path)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None

def extract_stage_info(p: dict) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    sid = get_in(p, ["Stage", "StageId"])
    if isinstance(sid, int) and sid in WORK_STAGE_MAP:
        label, emoji = WORK_STAGE_MAP[sid]
        return sid, label, emoji
    return None, None, None

def extract_fields(p: dict):
    """Campos comunes y específicos (DesignElement/WorkItem)."""
    title = pick_str(
        get_in(p, ["data", "title"]),
        get_in(p, ["data", "name"]),
        p.get("Title"), p.get("Name"),
        p.get("title"), p.get("name"),
    )

    desc = pick_str(
        get_in(p, ["data", "summary"]),
        get_in(p, ["data", "description"]),
        p.get("Description"), p.get("Summary"),
        p.get("description"), p.get("summary"),
    ) or ("Sin descripción." if NOTIF_LOCALE == "es" else "No description.")

    type_name = pick_str(
        get_in(p, ["Type", "Name"]),
        get_in(p, ["data", "type", "name"]),
        p.get("TypeName"),
    )

    project_id = p.get("ProjectId") or get_in(p, ["data", "projectId"])
    design_element_id = p.get("DesignElementId") or get_in(p, ["data", "id"])
    work_item_id = p.get("WorkItemId")

    # URLs
    design_url = pick_str(
        get_in(p, ["data", "url"]),
        get_in(p, ["data", "webUrl"]),
        p.get("Url"), p.get("url"),
    ) or compute_design_url(p)

    board_url = compute_board_url(p)

    parent_name = get_in(p, ["Parent", "Name"])
    stage_id, stage_label, stage_emoji = extract_stage_info(p)

    archived = str(p.get("Archived", "")).lower() in ("true", "1", "yes") or \
               str(p.get("IsArchived", "")).lower() in ("true", "1", "yes")

    return {
        "title": title,
        "desc": desc,
        "type_name": type_name,
        "project_id": project_id,
        "design_element_id": design_element_id,
        "work_item_id": work_item_id,
        "design_url": design_url,
        "board_url": board_url,
        "parent_name": parent_name,
        "stage_id": stage_id,
        "stage_label": stage_label,
        "stage_emoji": stage_emoji,
        "archived": archived,
    }

# ================== Discord ==================
async def post_to_discord(event_type: str, payload: Dict[str, Any]):
    f = extract_fields(payload)
    actor = extract_actor(payload)

    kind_l, action = split_event(event_type)

    # URL del embed:
    # - DesignElement: usar design_url (salvo deleted/archived → None)
    # - WorkItem: usar board_url (la board existe aunque se cambie/elimine la tarea)
    url = None
    if kind_l == "designelement":
        url = None if action == "deleted" or f["archived"] else f["design_url"]
    elif kind_l == "workitem":
        url = f["board_url"]  # siempre que haya board info

    # Contenido principal humanizado
    content = format_content(event_type, f["type_name"], f["title"], actor)

    # Si es WorkItem y tenemos Stage -> agregar al contenido: e.g. "🧪 **Testeandose** 🧪"
    if kind_l == "workitem" and f["stage_label"] and f["stage_emoji"]:
        content = f"{content} — {f['stage_emoji']} **{f['stage_label']}** {f['stage_emoji']}"

    # Armar embed
    embed_title = f["title"] or ("Elemento de diseño" if NOTIF_LOCALE == "es" else "Design element")
    embed = {
        "title": embed_title,
        "description": shorten(f["desc"], 1000),
        "url": url,
        "fields": []
    }

    # Campos comunes
    if f["type_name"]:
        embed["fields"].append({"name": "Tipo", "value": f["type_name"], "inline": True})
    if f["project_id"] is not None:
        embed["fields"].append({"name": "ProjectId", "value": str(f["project_id"]), "inline": True})

    # IDs y específicos
    if kind_l == "designelement" and f["design_element_id"] is not None:
        embed["fields"].append({"name": "DesignElementId", "value": str(f["design_element_id"]), "inline": True})
    if kind_l == "workitem":
        if f["work_item_id"] is not None:
            embed["fields"].append({"name": "WorkItemId", "value": str(f["work_item_id"]), "inline": True})
        # Mostrar Board info (ID y link visible en descripción/URL del embed)
        board_id = get_in(payload, ["Board", "BoardId"]) or payload.get("BoardId")
        cat_id = get_in(payload, ["Category", "CategoryId"]) or payload.get("CategoryId")
        if board_id is not None:
            embed["fields"].append({"name": "BoardId", "value": str(board_id), "inline": True})
        if cat_id is not None:
            embed["fields"].append({"name": "CategoryId", "value": str(cat_id), "inline": True})

    # Stage si es WorkItem
    if kind_l == "workitem" and f["stage_label"]:
        embed["fields"].append({
            "name": "Stage",
            "value": f"{f['stage_emoji']} {f['stage_label']} ({f['stage_id']})",
            "inline": True
        })

    if actor:
        embed["fields"].append({"name": "Responsable" if NOTIF_LOCALE == "es" else "Actor",
                                "value": actor, "inline": True})

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

    # Tipo de evento (header oficial o fallback)
    event_type = req.headers.get("x-hacknplan-event")
    payload = await parse_request(req)
    if not event_type:
        event_type = "WorkItem.Updated" if "WorkItemId" in payload else "DesignElement.Updated"

    # Log mínimo
    print("HNP event:", event_type, "| keys:", list(payload.keys())[:12])

    if DISCORD_WEBHOOK_URL:
        bg.add_task(post_to_discord, event_type, payload)

    return Response("OK", status_code=200)

@app.get("/healthz")
def healthz():
    return {"ok": True}
