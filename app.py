from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
import json
import shlex
import uuid

APP_DIR = Path(__file__).resolve().parent
ENV_PROJECTS_ROOT = os.environ.get("PATCHER_ROOT")
PROJECTS_ROOT_LOCKED = bool(ENV_PROJECTS_ROOT)
PROJECTS_ROOT = Path(ENV_PROJECTS_ROOT or Path.cwd()).expanduser().resolve()
MAX_PATCH_BYTES = 5 * 1024 * 1024
CONFIG_FILE = Path(__file__).with_name("patcher_config.json")
DEFAULT_CONFIG = {"pinned": [], "commands": [], "projects_root": None}

def load_config():
    try:
        data = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        data = {}
    return {"pinned": data.get("pinned", []), "commands": data.get("commands", []), "projects_root": data.get("projects_root")}

def save_config(data):
    CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

# When PATCHER_ROOT is not provided, reuse the root chosen previously in the UI.
if not PROJECTS_ROOT_LOCKED:
    _saved_root = load_config().get("projects_root")
    if _saved_root:
        _candidate = Path(_saved_root).expanduser().resolve()
        if _candidate.is_dir():
            PROJECTS_ROOT = _candidate

class PinRequest(BaseModel):
    project: str

class RootRequest(BaseModel):
    path: str

class CommandRequest(BaseModel):
    project: str
    name: str
    commands: list[str]


app = FastAPI(title="Patcher")


@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    return FileResponse(Path(__file__).with_name("favicon.svg"), media_type="image/svg+xml")


def run_git(project: Path, *args: str, input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=project,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )


def project_path(project_id: str) -> Path:
    # IDs may point to a Git repo nested inside a direct child of the projects root.
    # Keep them relative, shallow and strictly contained inside PROJECTS_ROOT.
    if not project_id or "\\" in project_id:
        raise HTTPException(400, "Proyecto inválido")
    rel = Path(project_id)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts) or len(rel.parts) > 3:
        raise HTTPException(400, "Proyecto inválido")
    p = (PROJECTS_ROOT / rel).resolve()
    try:
        p.relative_to(PROJECTS_ROOT)
    except ValueError:
        raise HTTPException(400, "Proyecto fuera de la raíz configurada")
    if not p.is_dir():
        raise HTTPException(404, "Proyecto no encontrado")
    return p


SCAN_SKIP = {".git", "node_modules", ".venv", "venv", "dist", "build", "coverage", "__pycache__"}
MAX_REPO_DEPTH = 3

def find_git_repos(folder: Path, depth: int = 0) -> list[Path]:
    """Find Git repositories below a project folder without walking dependency trees."""
    if (folder / ".git").exists():
        return [folder]
    if depth >= MAX_REPO_DEPTH:
        return []
    repos: list[Path] = []
    try:
        children = sorted(folder.iterdir(), key=lambda x: x.name.lower())
    except OSError:
        return repos
    for child in children:
        if not child.is_dir() or child.name.startswith(".") or child.name in SCAN_SKIP:
            continue
        repos.extend(find_git_repos(child, depth + 1))
    return repos


def validate_patch_paths(data: bytes) -> None:
    # Reject absolute/traversal paths appearing in git patch headers.
    text = data.decode("utf-8", errors="replace")
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            if m:
                paths.extend(m.groups())
        elif line.startswith("--- ") or line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0].strip()
            if value != "/dev/null":
                if value.startswith(("a/", "b/")):
                    value = value[2:]
                paths.append(value)
    for raw in paths:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            raise HTTPException(400, f"Ruta insegura dentro del patch: {raw}")


async def read_patch(upload: UploadFile) -> bytes:
    data = await upload.read(MAX_PATCH_BYTES + 1)
    if len(data) > MAX_PATCH_BYTES:
        raise HTTPException(413, "Patch demasiado grande (máximo 5 MB)")
    if not data.strip():
        raise HTTPException(400, "Patch vacío")
    validate_patch_paths(data)
    return data


def git_with_temp_patch(project: Path, data: bytes, *args: str):
    fd, name = tempfile.mkstemp(prefix="patcher-", suffix=".patch")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return run_git(project, "apply", *args, name)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace").strip()


@app.get("/api/projects")
def projects():
    if not PROJECTS_ROOT.is_dir():
        raise HTTPException(500, f"No existe PATCHER_ROOT: {PROJECTS_ROOT}")
    result = []
    for container in sorted(PROJECTS_ROOT.iterdir(), key=lambda x: x.name.lower()):
        # Do not expose Patcher itself when its source directory lives inside the selected root.
        if not container.is_dir() or container.name.startswith(".") or container.resolve() == APP_DIR:
            continue
        root_is_git = (container / ".git").exists()
        # A direct child of the configured root is always selectable, even before `git init`.
        result.append({
            "id": container.name, "name": container.name, "container": None,
            "display": container.name, "git": root_is_git, "nested": False,
            "url": f"http://{container.name}.test",
        })
        # Organizational folders can additionally expose their nested repositories.
        if not root_is_git:
            for repo in find_git_repos(container):
                if repo == container:
                    continue
                rel = repo.relative_to(PROJECTS_ROOT)
                result.append({
                    "id": rel.as_posix(), "name": repo.name, "container": container.name,
                    "display": " / ".join(rel.parts), "git": True, "nested": True,
                    "url": f"http://{container.name}.test",
                })
    return {"root": str(PROJECTS_ROOT), "root_locked": PROJECTS_ROOT_LOCKED, "projects": result}



@app.get("/api/root")
def get_projects_root():
    return {"root": str(PROJECTS_ROOT), "locked": PROJECTS_ROOT_LOCKED, "source": "env" if PROJECTS_ROOT_LOCKED else ("config" if load_config().get("projects_root") else "cwd")}

@app.post("/api/root")
def set_projects_root(req: RootRequest):
    global PROJECTS_ROOT
    if PROJECTS_ROOT_LOCKED:
        raise HTTPException(409, "La raíz está definida por PATCHER_ROOT y no puede cambiarse desde la interfaz")
    raw = req.path.strip()
    if not raw:
        raise HTTPException(400, "Escribe una ruta de carpeta")
    candidate = Path(raw).expanduser().resolve()
    if not candidate.exists():
        raise HTTPException(404, "La carpeta no existe")
    if not candidate.is_dir():
        raise HTTPException(400, "La ruta no corresponde a una carpeta")
    PROJECTS_ROOT = candidate
    cfg = load_config()
    cfg["projects_root"] = str(candidate)
    # Pins refer to paths relative to the previous root, so clear them when changing roots.
    cfg["pinned"] = []
    save_config(cfg)
    return {"ok": True, "root": str(PROJECTS_ROOT), "locked": False}

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/pins/toggle")
def toggle_pin(req: PinRequest):
    project_path(req.project)
    cfg = load_config()
    if req.project in cfg["pinned"]:
        cfg["pinned"].remove(req.project)
        pinned = False
    else:
        cfg["pinned"].append(req.project)
        pinned = True
    save_config(cfg)
    return {"ok": True, "pinned": pinned, "config": cfg}

@app.post("/api/commands")
def add_command(req: CommandRequest):
    name = req.name.strip()
    commands = [c.strip() for c in req.commands if c.strip()]
    if not name or not commands:
        raise HTTPException(400, "Nombre y al menos un comando son obligatorios")
    cfg = load_config()
    project_path(req.project)
    item = {"id": uuid.uuid4().hex[:10], "project": req.project, "name": name, "commands": commands}
    cfg["commands"].append(item)
    save_config(cfg)
    return item

@app.put("/api/commands/{command_id}")
def update_command(command_id: str, req: CommandRequest):
    cfg = load_config()
    name = req.name.strip()
    commands = [c.strip() for c in req.commands if c.strip()]
    if not name or not commands:
        raise HTTPException(400, "Nombre y comandos son obligatorios")
    item = next((x for x in cfg["commands"] if x.get("id") == command_id and x.get("project") == req.project), None)
    if not item:
        raise HTTPException(404, "Comando no encontrado")
    item["name"] = name
    item["commands"] = commands
    save_config(cfg)
    return item


@app.delete("/api/commands/{command_id}")
def delete_command(command_id: str):
    cfg = load_config()
    before = len(cfg["commands"])
    cfg["commands"] = [x for x in cfg["commands"] if x.get("id") != command_id]
    if len(cfg["commands"]) == before:
        raise HTTPException(404, "Comando no encontrado")
    save_config(cfg)
    return {"ok": True}

@app.post("/api/commands/{command_id}/run")
def execute_command(command_id: str, req: PinRequest):
    p = project_path(req.project)
    cfg = load_config()
    item = next((x for x in cfg["commands"] if x.get("id") == command_id and x.get("project") == req.project), None)
    if not item:
        raise HTTPException(404, "Comando no encontrado")
    outputs = []
    for command in item["commands"]:
        # Local developer tool: commands are explicitly created by the local user.
        result = subprocess.run(command, cwd=p, shell=True, executable="/bin/zsh", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, text=True)
        outputs.append({"command": command, "code": result.returncode, "output": result.stdout[-20000:]})
        if result.returncode != 0:
            break
    return {"ok": all(x["code"] == 0 for x in outputs), "name": item["name"], "results": outputs}

@app.post("/api/check")
async def check(project: str = Form(...), patch: UploadFile = File(...)):
    p = project_path(project)
    data = await read_patch(patch)

    check_result = git_with_temp_patch(p, data, "--check")
    ok = check_result.returncode == 0

    stat = git_with_temp_patch(p, data, "--stat")
    numstat = git_with_temp_patch(p, data, "--numstat")

    files = []
    if numstat.returncode == 0:
        for line in decode(numstat.stdout).splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                add, delete, filename = parts
                files.append({
                    "file": filename,
                    "added": None if add == "-" else int(add),
                    "deleted": None if delete == "-" else int(delete),
                })

    return {
        "ok": ok,
        "message": "Se puede aplicar" if ok else "No se puede aplicar",
        "error": decode(check_result.stderr) or decode(check_result.stdout) if not ok else "",
        "stat": decode(stat.stdout),
        "files": files,
        "preview": data.decode("utf-8", errors="replace")[:120000],
    }


@app.post("/api/apply")
async def apply_patch(project: str = Form(...), patch: UploadFile = File(...)):
    p = project_path(project)
    data = await read_patch(patch)

    check_result = git_with_temp_patch(p, data, "--check")
    if check_result.returncode != 0:
        raise HTTPException(409, decode(check_result.stderr) or "El patch ya no se puede aplicar")

    result = git_with_temp_patch(p, data)
    if result.returncode != 0:
        raise HTTPException(500, decode(result.stderr) or "Falló git apply")

    return {"ok": True, "message": "Patch aplicado correctamente"}


@app.post("/api/reverse-check")
async def reverse_check(project: str = Form(...), patch: UploadFile = File(...)):
    p = project_path(project)
    data = await read_patch(patch)
    result = git_with_temp_patch(p, data, "--reverse", "--check")
    return {"ok": result.returncode == 0, "error": decode(result.stderr)}


@app.post("/api/reverse")
async def reverse(project: str = Form(...), patch: UploadFile = File(...)):
    p = project_path(project)
    data = await read_patch(patch)
    check = git_with_temp_patch(p, data, "--reverse", "--check")
    if check.returncode != 0:
        raise HTTPException(409, decode(check.stderr) or "No se puede deshacer este patch")
    result = git_with_temp_patch(p, data, "--reverse")
    if result.returncode != 0:
        raise HTTPException(500, decode(result.stderr) or "No se pudo deshacer")
    return {"ok": True, "message": "Patch deshecho"}


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Patcher</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{color-scheme:dark;--bg:#0c0d10;--panel:#13151a;--line:#272a31;--muted:#9299a8;--text:#f3f5f7;--good:#5ed69a;--bad:#ff7777}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
button,select,input,textarea{font:inherit}.app{max-width:1100px;margin:0 auto;padding:28px}.top{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}.brand{font-size:20px;font-weight:750}.rootWrap{display:flex;align-items:center;gap:8px;max-width:72%}.root{color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.rootChange{padding:5px 8px;font-size:12px;background:transparent;color:#78aef4}.rootLock{font-size:11px;color:var(--muted)}
.projectHub{max-width:680px;margin:4px auto 22px;position:relative}.searchShell{position:relative}.search{width:100%;height:52px;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:14px;padding:0 46px 0 44px;font-size:15px;outline:none;transition:.15s}.search:focus,.searchShell.open .search{border-color:#6ea8e8;box-shadow:0 0 0 2px rgba(110,168,232,.12)}.searchIcon{position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none;font-size:18px}.clearProject{position:absolute;right:10px;top:50%;transform:translateY(-50%);width:32px;height:32px;padding:0;border:0;background:transparent;color:var(--muted);font-size:20px;display:none}.clearProject.show{display:block}.projectMenu{position:absolute;left:0;right:0;top:58px;z-index:20;background:#11141a;border:1px solid var(--line);border-radius:12px;box-shadow:0 18px 45px rgba(0,0,0,.38);overflow:hidden;max-height:330px;overflow-y:auto}.projectResult{width:100%;border:0;border-bottom:1px solid #20242b;border-radius:0;background:transparent;padding:11px 13px;display:grid;grid-template-columns:1fr auto auto;gap:12px;align-items:center;text-align:left}.projectResult:last-child{border-bottom:0}.projectResult:hover,.projectResult.active{background:#1a2230}.projectResult .projectName{font-weight:650}.projectResult .projectUrl{font-size:12px;color:var(--muted)}.projectResult .star{font-size:17px;color:#c9a94f}.quick{display:flex;justify-content:center;gap:6px;flex-wrap:wrap;margin-top:9px}.quick button{padding:5px 9px;border-radius:7px;font-size:12px;color:#c9ced8;background:#15181e}.quick button.active{border-color:#596171;background:#20252e}.quick .star{color:#d9b94e;margin-right:4px}.commands{margin-top:22px;border-top:1px solid var(--line);padding-top:18px}.cmdHead{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.cmdHead strong{font-size:16px}.cmdList{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.cmdCard{position:relative;min-height:72px;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:12px;display:grid;grid-template-columns:38px minmax(0,1fr) 28px;gap:10px;align-items:center;transition:.15s}.cmdCard:hover{border-color:#363b46;background:#16191f}.cmdCard.running{grid-column:span 2;border-color:#4f8fe8;box-shadow:0 0 0 1px rgba(79,143,232,.2)}.cmdRun{width:38px;height:38px;border:0;border-radius:10px;padding:0;display:grid;place-items:center;background:#173b2b;color:#5ed69a;font-size:17px}.cmdCard.running .cmdRun{background:#172a43;color:#75adf3}.cmdCard.ok .cmdRun{background:#173b2b;color:var(--good)}.cmdCard.bad .cmdRun{background:#421d22;color:var(--bad)}.cmdInfo{min-width:0}.cmdName{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.cmdSub{font:11px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}.cmdState{font-size:12px;margin-top:4px}.cmdState.ok{color:var(--good)}.cmdState.bad{color:var(--bad)}.cmdState.running{color:#75adf3}.cmdDetail{color:#78aef4;cursor:pointer}.cmdDetail:hover{text-decoration:underline}.cmdMore{width:28px;height:28px;padding:0;border:0;background:transparent;color:var(--muted);font-size:18px}.cmdMore:hover{background:#20242c;color:var(--text)}.cmdMenu{position:absolute;right:10px;top:48px;z-index:12;min-width:130px;padding:5px;background:#1a1e26;border:1px solid #343945;border-radius:9px;box-shadow:0 12px 30px rgba(0,0,0,.4)}.cmdMenu button{width:100%;border:0;background:transparent;text-align:left;padding:7px 9px}.cmdMenu button:hover{background:#252a34}.cmdMenu .danger{color:var(--bad)}.cmdOutput{grid-column:1/-1;margin:2px 0 0;max-height:230px;overflow:auto;background:#090a0c;border:1px solid var(--line);border-radius:8px;padding:10px;font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}.cmdForm{margin-top:12px;padding:14px;background:var(--panel);border:1px solid var(--line);border-radius:10px}.cmdForm input,.cmdForm textarea{width:100%;background:#0d0f13;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:9px;margin-bottom:8px}.cmdForm textarea{min-height:85px;resize:vertical}.danger{color:var(--bad)}select{min-width:240px;background:var(--panel);border:1px solid var(--line);color:var(--text);border-radius:9px;padding:9px 12px}.site{color:var(--muted);text-decoration:none}.site:hover{color:var(--text)}
.drop{border:1px dashed #414652;border-radius:14px;min-height:220px;display:grid;place-items:center;text-align:center;background:var(--panel);transition:.15s;cursor:pointer}.drop.drag{border-color:#888;background:#181b21}.drop strong{display:block;font-size:17px;margin-bottom:5px}.drop span{color:var(--muted)}
.card{margin-top:14px;border-top:1px solid var(--line);padding-top:16px}.hidden{display:none}.status{display:flex;align-items:center;justify-content:space-between;gap:15px}.ok{color:var(--good)}.bad{color:var(--bad)}.name{font-weight:650}.summary{color:var(--muted);margin-top:5px}.files{margin:16px 0}.file{display:grid;grid-template-columns:1fr 70px 70px;padding:7px 0;border-bottom:1px solid #1d2026}.add{color:var(--good);text-align:right}.del{color:var(--bad);text-align:right}.actions{display:flex;gap:8px;margin-top:14px}button{border:1px solid var(--line);background:#1a1d23;color:var(--text);padding:9px 13px;border-radius:8px;cursor:pointer}button.primary{background:#f2f3f5;color:#111;border-color:#f2f3f5}button:disabled{opacity:.4;cursor:not-allowed}
.diffViewer{margin-top:14px;max-height:620px;overflow:auto;background:#090a0c;border:1px solid var(--line);border-radius:10px}.diffFile{border-bottom:1px solid var(--line)}.diffFile:last-child{border-bottom:0}.diffHead{position:sticky;top:0;z-index:2;display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:#151820;border-bottom:1px solid var(--line);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.diffHead button{padding:4px 8px}.diffBody{font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}.diffLine{display:grid;grid-template-columns:48px 48px 22px minmax(0,1fr);min-height:20px}.diffLine .ln{color:#626a78;text-align:right;padding:1px 8px;border-right:1px solid #20242b;user-select:none}.diffLine .mark{padding-left:7px;user-select:none}.diffLine .code{white-space:pre;overflow:visible;padding:1px 10px}.diffLine.added{background:rgba(46,160,67,.16)}.diffLine.added .mark,.diffLine.added .code{color:#a8e6b7}.diffLine.deleted{background:rgba(248,81,73,.15)}.diffLine.deleted .mark,.diffLine.deleted .code{color:#ffb0aa}.diffLine.hunk{background:#111b2b;color:#79a7e8}.diffLine.meta{color:#8b949e;background:#0d0f13}.diffEmpty{padding:20px;color:var(--muted)}pre{max-height:430px;overflow:auto;background:#090a0c;border:1px solid var(--line);border-radius:10px;padding:14px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}.error{white-space:pre-wrap;color:var(--bad);background:#170e10;border:1px solid #472329;padding:10px;border-radius:8px;margin-top:12px}
.modalBackdrop{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.68);display:grid;place-items:center;padding:18px}.modal{width:min(560px,100%);background:#15181e;border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 24px 70px rgba(0,0,0,.55)}.modal h2{font-size:17px;margin:0 0 6px}.modal p{color:var(--muted);margin:0 0 14px}.modal input{width:100%;background:#0d0f13;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:11px 12px}.modalError{color:var(--bad);font-size:12px;margin-top:8px}.modalBackdrop.hidden{display:none}@media(max-width:650px){.cmdList{grid-template-columns:1fr}.cmdCard.running{grid-column:span 1}.app{padding:16px}.root{display:none}.projectHub{max-width:none}.projectResult{grid-template-columns:1fr auto}.projectResult .projectUrl{display:none}.file{grid-template-columns:1fr 50px 50px}}
</style>
</head>
<body><main class="app">
<div class="top"><div class="brand">Patcher</div><div class="rootWrap"><div id="root" class="root"></div><span id="rootLock" class="rootLock hidden">PATCHER_ROOT</span><button id="rootChange" class="rootChange hidden">Cambiar</button></div></div>
<div class="projectHub"><div id="searchShell" class="searchShell"><span class="searchIcon">⌕</span><input id="projectSearch" class="search" autocomplete="off" placeholder="Buscar proyecto…"><button id="clearProject" class="clearProject" title="Limpiar selección">×</button><div id="projectMenu" class="projectMenu hidden"></div></div><div id="quick" class="quick"></div></div>
<div id="drop" class="drop"><div><strong>Arrastra un .patch aquí</strong><span>o haz clic para seleccionarlo</span><input id="file" type="file" accept=".patch,.diff,text/x-diff" hidden></div></div>
<section id="card" class="card hidden">
 <div class="status"><div><div id="filename" class="name"></div><div id="message"></div></div><div id="totals" class="summary"></div></div>
 <div id="error" class="error hidden"></div><div id="files" class="files"></div>
 <div class="actions"><button id="previewBtn">Ver cambios</button><button id="applyBtn" class="primary">Aplicar patch</button><button id="undoBtn" class="hidden">Deshacer</button></div>
 <div id="preview" class="diffViewer hidden"></div>
</section>
<section class="commands"><div class="cmdHead"><strong>Comandos rápidos</strong><button id="newCmd">+ Nuevo comando</button></div><div id="cmdList" class="cmdList"></div><div id="cmdForm" class="cmdForm hidden"><input id="cmdName" placeholder="Nombre, ej. Reiniciar servidor"><textarea id="cmdText" placeholder="Un comando por línea&#10;docker compose down&#10;docker compose up -d"></textarea><div class="actions"><button id="saveCmd" class="primary">Guardar</button><button id="cancelCmd">Cancelar</button></div></div></section>
</main>
<div id="rootModal" class="modalBackdrop hidden"><div class="modal"><h2>Carpeta de proyectos</h2><p>Selecciona la carpeta raíz que contiene los proyectos que quieres administrar.</p><input id="rootInput" autocomplete="off" placeholder="/ruta/a/mis/proyectos"><div id="rootError" class="modalError hidden"></div><div class="actions" style="justify-content:flex-end"><button id="cancelRoot">Cancelar</button><button id="saveRoot" class="primary">Usar esta carpeta</button></div></div></div>
<script>
const $=s=>document.querySelector(s); let currentFile=null,currentCheck=null,allProjects=[],config={pinned:[],commands:[]},commandState={},editingCommandId=null,openCommandMenu=null;
async function loadProjects(){const [pr,cr]=await Promise.all([fetch('/api/projects'),fetch('/api/config')]);const d=await pr.json();if(!pr.ok)throw new Error(d.detail||'No se pudieron cargar los proyectos');config=await cr.json();allProjects=d.projects;$('#root').textContent=d.root;$('#root').title=d.root;$('#rootChange').classList.toggle('hidden',!!d.root_locked);$('#rootLock').classList.toggle('hidden',!d.root_locked);renderQuick();renderCommands()}
let selectedProject='';
function currentProject(){return allProjects.find(p=>p.id===selectedProject)||null}
function projectLabel(p){return p.display||p.name}
function renderProjectMenu(){const input=$('#projectSearch'),q=input.value.toLowerCase().trim();const list=allProjects.filter(p=>!q||p.name.toLowerCase().includes(q)||(p.container||'').toLowerCase().includes(q)||(p.display||'').toLowerCase().includes(q)||p.url.toLowerCase().includes(q)).slice(0,40);const menu=$('#projectMenu');menu.innerHTML=list.length?list.map(p=>`<button class="projectResult ${selectedProject===p.id?'active':''}" data-project="${esc(p.id)}"><span class="projectName">${p.nested?'<span style="color:var(--muted);font-weight:400">'+esc(p.container)+' / </span>':''}${esc(p.name)}</span><span class="projectUrl">${p.git?esc(p.url.replace('http://','')):'Sin Git · '+esc(p.url.replace('http://',''))}</span><span class="star" data-pin="${esc(p.id)}" title="${config.pinned.includes(p.id)?'Quitar de fijados':'Fijar proyecto'}">${config.pinned.includes(p.id)?'★':'☆'}</span></button>`).join(''):`<div class="summary" style="padding:14px">No hay proyectos que coincidan.</div>`;menu.classList.remove('hidden');$('#searchShell').classList.add('open');menu.querySelectorAll('[data-project]').forEach(row=>row.onclick=e=>{if(e.target.closest('[data-pin]'))return;selectProject(row.dataset.project)});menu.querySelectorAll('[data-pin]').forEach(star=>star.onclick=async e=>{e.stopPropagation();await togglePin(star.dataset.pin);renderProjectMenu()})}
function closeProjectMenu(){ $('#projectMenu').classList.add('hidden');$('#searchShell').classList.remove('open') }
function selectProject(id){const p=allProjects.find(x=>x.id===id);if(!p)return;selectedProject=id;$('#projectSearch').value=projectLabel(p);$('#clearProject').classList.add('show');closeProjectMenu();renderQuick();renderCommands();$('#terminal').classList.add('hidden');if(currentFile)checkFile(currentFile)}
function clearProject(){selectedProject='';$('#projectSearch').value='';$('#clearProject').classList.remove('show');renderQuick();renderCommands();$('#terminal').classList.add('hidden');$('#projectSearch').focus();renderProjectMenu()}
function renderQuick(){const map=new Map(allProjects.map(p=>[p.id,p]));$('#quick').innerHTML=config.pinned.map(id=>map.get(id)).filter(Boolean).map(p=>`<button data-project="${esc(p.id)}" class="${selectedProject===p.id?'active':''}"><span class="star">★</span>${esc(p.display||p.name)}</button>`).join('');$('#quick').querySelectorAll('button').forEach(b=>b.onclick=()=>selectProject(b.dataset.project))}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function togglePin(project){if(!project)return;const r=await fetch('/api/pins/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project})});const d=await r.json();if(r.ok){config=d.config;renderQuick()}}
$('#projectSearch').addEventListener('focus',()=>{const p=currentProject();if(p){$('#projectSearch').value='';}renderProjectMenu()});
$('#projectSearch').addEventListener('input',()=>{if(selectedProject)selectedProject='';$('#clearProject').classList.toggle('show',!!$('#projectSearch').value);renderQuick();renderCommands();renderProjectMenu()});
$('#projectSearch').addEventListener('keydown',e=>{if(e.key==='Escape'){const p=currentProject();if(p)$('#projectSearch').value=projectLabel(p);closeProjectMenu();$('#projectSearch').blur()}});
$('#clearProject').onclick=e=>{e.preventDefault();clearProject()};
document.addEventListener('click',e=>{if(!e.target.closest('.projectHub')){const p=currentProject();if(p)$('#projectSearch').value=projectLabel(p);closeProjectMenu()}});
const drop=$('#drop'),input=$('#file');drop.onclick=()=>input.click();input.onchange=()=>input.files[0]&&checkFile(input.files[0]);
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.add('drag')}));['dragleave','drop'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.remove('drag')}));drop.addEventListener('drop',e=>{const f=e.dataTransfer.files[0];if(f)checkFile(f)});
function form(file){const f=new FormData();f.append('project',selectedProject);f.append('patch',file,file.name);return f}
async function checkFile(file){if(!selectedProject){$('#projectSearch').focus();renderProjectMenu();return}currentFile=file;$('#card').classList.remove('hidden');$('#filename').textContent=file.name;$('#message').className='';$('#message').textContent='Validando…';$('#files').innerHTML='';$('#error').classList.add('hidden');$('#applyBtn').disabled=true;$('#undoBtn').classList.add('hidden');
 try{const r=await fetch('/api/check',{method:'POST',body:form(file)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Error');currentCheck=d;render(d)}catch(e){render({ok:false,message:'Error',error:e.message,files:[],preview:''})}}
function render(d){$('#message').textContent=d.ok?'✓ '+d.message:'✕ '+d.message;$('#message').className=d.ok?'ok':'bad';$('#applyBtn').disabled=!d.ok;let a=0,z=0;for(const f of d.files||[]){a+=f.added||0;z+=f.deleted||0}$('#totals').textContent=`${(d.files||[]).length} archivo(s) · +${a} −${z}`;$('#files').innerHTML=(d.files||[]).map(f=>`<div class="file"><span>${esc(f.file)}</span><span class="add">+${f.added??'bin'}</span><span class="del">−${f.deleted??'bin'}</span></div>`).join('');renderDiff(d.preview||'');if(d.error){$('#error').textContent=d.error;$('#error').classList.remove('hidden')}else $('#error').classList.add('hidden')}
$('#previewBtn').onclick=()=>{const v=$('#preview');v.classList.toggle('hidden');$('#previewBtn').textContent=v.classList.contains('hidden')?'Ver cambios':'Ocultar cambios'};
$('#applyBtn').onclick=async()=>{if(!currentFile)return;$('#applyBtn').disabled=true;try{const r=await fetch('/api/apply',{method:'POST',body:form(currentFile)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'No se pudo aplicar');$('#message').textContent='✓ Patch aplicado';$('#message').className='ok';$('#undoBtn').classList.remove('hidden')}catch(e){$('#error').textContent=e.message;$('#error').classList.remove('hidden');$('#applyBtn').disabled=false}};
$('#undoBtn').onclick=async()=>{if(!currentFile)return;$('#undoBtn').disabled=true;try{const r=await fetch('/api/reverse',{method:'POST',body:form(currentFile)});const d=await r.json();if(!r.ok)throw new Error(d.detail||'No se pudo deshacer');$('#message').textContent='↶ Patch deshecho';$('#message').className='ok';$('#undoBtn').classList.add('hidden');$('#applyBtn').disabled=false}catch(e){$('#error').textContent=e.message;$('#error').classList.remove('hidden')}finally{$('#undoBtn').disabled=false}};

function renderDiff(text){
 const root=$('#preview'); if(!text.trim()){root.innerHTML='<div class="diffEmpty">No hay diff para mostrar.</div>';return}
 const lines=text.split('\n'); let files=[],file=null,oldNo=null,newNo=null;
 const pushFile=name=>{file={name:name||'Cambios',lines:[]};files.push(file);oldNo=null;newNo=null};
 for(const raw of lines){
   if(raw.startsWith('diff --git ')){const m=raw.match(/^diff --git a\/(.*?) b\/(.*)$/);pushFile(m?m[2]:raw.replace('diff --git ',''));file.lines.push({type:'meta',raw});continue}
   if(!file)pushFile('Patch');
   if(raw.startsWith('@@')){const m=raw.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);if(m){oldNo=+m[1];newNo=+m[2]}file.lines.push({type:'hunk',raw});continue}
   if(raw.startsWith('+++')||raw.startsWith('---')||raw.startsWith('index ')||raw.startsWith('new file')||raw.startsWith('deleted file')){file.lines.push({type:'meta',raw});continue}
   if(raw.startsWith('+')){file.lines.push({type:'added',old:'',neu:newNo??'',mark:'+',code:raw.slice(1)});if(newNo!=null)newNo++;continue}
   if(raw.startsWith('-')){file.lines.push({type:'deleted',old:oldNo??'',neu:'',mark:'−',code:raw.slice(1)});if(oldNo!=null)oldNo++;continue}
   if(raw.startsWith(' ')){file.lines.push({type:'context',old:oldNo??'',neu:newNo??'',mark:' ',code:raw.slice(1)});if(oldNo!=null)oldNo++;if(newNo!=null)newNo++;continue}
   file.lines.push({type:'meta',raw});
 }
 const lineHtml=l=>l.raw!==undefined?`<div class="diffLine ${l.type}"><span class="ln"></span><span class="ln"></span><span class="mark"></span><span class="code">${esc(l.raw)}</span></div>`:`<div class="diffLine ${l.type}"><span class="ln">${l.old}</span><span class="ln">${l.neu}</span><span class="mark">${l.mark}</span><span class="code">${esc(l.code)}</span></div>`;
 root.innerHTML=files.map((f,i)=>`<section class="diffFile"><div class="diffHead"><span>${esc(f.name)}</span><button data-fold="${i}">−</button></div><div class="diffBody" data-body="${i}">${f.lines.map(lineHtml).join('')}</div></section>`).join('');
 root.querySelectorAll('[data-fold]').forEach(b=>b.onclick=()=>{const body=root.querySelector(`[data-body="${b.dataset.fold}"]`);body.classList.toggle('hidden');b.textContent=body.classList.contains('hidden')?'+':'−'});
}
function commandPreview(c){return (c.commands||[]).join(' && ')}
function commandElapsed(st){return st?.elapsed?` · ${st.elapsed}s`:''}
function renderCommands(){
 const project=selectedProject, commands=(config.commands||[]).filter(c=>c.project===project), list=$('#cmdList');
 if(!project){list.innerHTML='<span class="summary">Selecciona un proyecto para ver sus comandos.</span>';return}
 if(!commands.length){list.innerHTML='<span class="summary">No hay comandos todavía.</span>';return}
 list.innerHTML=commands.map(c=>{const st=commandState[c.id]||{},running=st.state==='running',done=st.state==='ok'||st.state==='bad';let state='';
   if(running)state='<div class="cmdState running">● Ejecutando…</div>';
   else if(st.state==='ok')state=`<div class="cmdState ok">✓ Listo${commandElapsed(st)} · <span class="cmdDetail" data-detail="${esc(c.id)}">Ver detalle</span></div>`;
   else if(st.state==='bad')state=`<div class="cmdState bad">✕ Error${commandElapsed(st)} · <span class="cmdDetail" data-detail="${esc(c.id)}">Ver detalle</span></div>`;
   const output=running?`<pre class="cmdOutput">${esc(st.output||'Preparando…')}</pre>`:'';
   const menu=openCommandMenu===c.id?`<div class="cmdMenu"><button data-edit="${esc(c.id)}">Editar</button><button data-duplicate="${esc(c.id)}">Duplicar</button><button data-del="${esc(c.id)}" class="danger">Eliminar</button></div>`:'';
   return `<article class="cmdCard ${esc(st.state||'')}" data-card="${esc(c.id)}"><button class="cmdRun" data-run="${esc(c.id)}" ${running?'disabled':''}>${running?'◌':st.state==='ok'?'✓':st.state==='bad'?'!':'▶'}</button><div class="cmdInfo"><div class="cmdName">${esc(c.name)}</div><div class="cmdSub">${esc(commandPreview(c))}</div>${state}</div><button class="cmdMore" data-more="${esc(c.id)}" title="Opciones">•••</button>${menu}${output}</article>`}).join('');
 list.querySelectorAll('[data-run]').forEach(b=>b.onclick=e=>{e.stopPropagation();runCommand(b.dataset.run)});
 list.querySelectorAll('[data-more]').forEach(b=>b.onclick=e=>{e.stopPropagation();openCommandMenu=openCommandMenu===b.dataset.more?null:b.dataset.more;renderCommands()});
 list.querySelectorAll('[data-detail]').forEach(b=>b.onclick=e=>{e.stopPropagation();showCommandDetail(b.dataset.detail)});
 list.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editCommand(b.dataset.edit));
 list.querySelectorAll('[data-duplicate]').forEach(b=>b.onclick=()=>duplicateCommand(b.dataset.duplicate));
 list.querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>deleteCommand(b.dataset.del));
}
document.addEventListener('click',e=>{if(openCommandMenu&&!e.target.closest('.cmdMenu')&&!e.target.closest('.cmdMore')){openCommandMenu=null;renderCommands()}});
function openCommandForm(c=null){editingCommandId=c?.id||null;$('#cmdName').value=c?.name||'';$('#cmdText').value=(c?.commands||[]).join('\n');$('#saveCmd').textContent=c?'Guardar cambios':'Guardar';$('#cmdForm').classList.remove('hidden');$('#cmdName').focus()}
$('#newCmd').onclick=()=>openCommandForm();
$('#cancelCmd').onclick=()=>{editingCommandId=null;$('#cmdForm').classList.add('hidden')};
$('#saveCmd').onclick=async()=>{const name=$('#cmdName').value.trim(),commands=$('#cmdText').value.split('\n').map(x=>x.trim()).filter(Boolean);if(!name||!commands.length)return alert('Escribe nombre y al menos un comando');const id=editingCommandId,url=id?'/api/commands/'+id:'/api/commands',method=id?'PUT':'POST';const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({project:selectedProject,name,commands})});const d=await r.json();if(!r.ok)return alert(d.detail||'Error');if(id){const i=config.commands.findIndex(x=>x.id===id);if(i>=0)config.commands[i]=d}else config.commands.push(d);editingCommandId=null;$('#cmdForm').classList.add('hidden');renderCommands()};
function editCommand(id){const c=config.commands.find(x=>x.id===id);openCommandMenu=null;if(c)openCommandForm(c)}
async function duplicateCommand(id){const c=config.commands.find(x=>x.id===id);if(!c)return;const r=await fetch('/api/commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project:selectedProject,name:c.name+' copia',commands:c.commands})});const d=await r.json();if(r.ok){config.commands.push(d);openCommandMenu=null;renderCommands()}}
async function deleteCommand(id){const r=await fetch('/api/commands/'+id,{method:'DELETE'});if(r.ok){config.commands=config.commands.filter(x=>x.id!==id);delete commandState[id];openCommandMenu=null;renderCommands()}}
function showCommandDetail(id){const st=commandState[id];if(!st?.output)return;const card=$(`[data-card="${id}"]`);if(!card)return;let out=card.querySelector('.cmdOutput');if(out){out.remove();return}out=document.createElement('pre');out.className='cmdOutput';out.textContent=st.output;card.appendChild(out);out.scrollTop=out.scrollHeight}
async function runCommand(id){const project=selectedProject;if(!project)return;const c=config.commands.find(x=>x.id===id&&x.project===project);if(!c)return;const started=Date.now();commandState[id]={state:'running',output:`$ ${c.commands.join('\n$ ')}`};renderCommands();try{const r=await fetch('/api/commands/'+id+'/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({project})});const d=await r.json();const output=r.ok?d.results.map(x=>`$ ${x.command}\n${x.output}\n[exit ${x.code}]`).join('\n'):(d.detail||'Error al ejecutar el comando');commandState[id]={state:r.ok&&d.ok?'ok':'bad',output,elapsed:Math.max(1,Math.round((Date.now()-started)/1000))}}catch(e){commandState[id]={state:'bad',output:'No se pudo ejecutar el comando: '+e.message,elapsed:Math.max(1,Math.round((Date.now()-started)/1000))}}renderCommands()}

function openRootModal(){if(!$('#rootChange').classList.contains('hidden')){$('#rootInput').value=$('#root').textContent;$('#rootError').classList.add('hidden');$('#rootModal').classList.remove('hidden');setTimeout(()=>{$('#rootInput').focus();$('#rootInput').select()},0)}}
function closeRootModal(){$('#rootModal').classList.add('hidden')}
$('#rootChange').onclick=openRootModal;$('#cancelRoot').onclick=closeRootModal;$('#rootModal').addEventListener('click',e=>{if(e.target===$('#rootModal'))closeRootModal()});$('#rootInput').addEventListener('keydown',e=>{if(e.key==='Enter')$('#saveRoot').click();if(e.key==='Escape')closeRootModal()});
$('#saveRoot').onclick=async()=>{const path=$('#rootInput').value.trim();const btn=$('#saveRoot');btn.disabled=true;$('#rootError').classList.add('hidden');try{const r=await fetch('/api/root',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'No se pudo cambiar la carpeta');selectedProject='';currentFile=null;$('#projectSearch').value='';$('#clearProject').classList.remove('show');$('#card').classList.add('hidden');closeRootModal();await loadProjects()}catch(e){$('#rootError').textContent=e.message;$('#rootError').classList.remove('hidden')}finally{btn.disabled=false}};
loadProjects().catch(e=>{document.body.innerHTML='<pre>'+esc(e.message)+'</pre>'});
</script></body></html>'''
