"""
Админка ретранслятора Token Harbor + статус-страница.
Супервизор запускает relay.py как дочерний процесс (127.0.0.1:$RELAY_PORT),
проксирует /v1/* через него, ведёт учёт в status_registry.
UI (title/theme/accent/секции/тексты/футер) читается из conf.yaml на каждый запрос.
Роли — из env-переменных ADMIN_TOKEN_OWNER/ADMIN_TOKEN_ADMIN/ADMIN_TOKEN_JUNIOR.
"""

import urllib.request
import urllib.error
import html as html_escape
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
import requests
from flask import Flask, Response, jsonify, request, stream_with_context

from status_registry import (
    reset as registry_reset,
    mark_started,
    mark_recovered,
    mark_unresponsive,
    mark_clean_exit,
    mark_crashed,
    mark_permanently_failed,
    append_log,
    update_service_override,
    snapshot,
)
from status_assets import STATUS_CSS, STATUS_HTML_TEMPLATE, STATUS_JS

try:
    from waitress import serve as waitress_serve
except ImportError:
    waitress_serve = None


# ============================================================
# НАСТРОЙКИ
# ============================================================
PORT = int(os.getenv("PORT", 8080))
CONF_FILE = Path("conf.yaml")

RELAY_FILE = "relay.py"
RELAY_HOST = "127.0.0.1"
RELAY_PORT = int(os.getenv("RELAY_PORT", 8080))
RELAY_URL = f"http://{RELAY_HOST}:{RELAY_PORT}"
RELAY_READY_TIMEOUT = 20.0
RELAY_READY_WAIT_FOR_CLIENT = 15.0

SERVICE_INDEX = 0
SERVICE_KEY = "token-harbor-relay"
SERVICE_NAME = "Token Harbor Relay"
SERVICE_DESCRIPTION = "OpenAI-совместимый ретранслятор для Token Harbor"

SELF_URL = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
PING_INTERVAL = 600  # 10 минут

app = Flask(__name__)

_stop = threading.Event()
_relay_proc: subprocess.Popen | None = None
_relay_ready = threading.Event()
_supervisor_started = False


# ============================================================
# РОЛИ И ПРАВА
# ============================================================
ROLE_PERMISSIONS = {
    "owner": frozenset({"view", "view_logs", "restart_relay", "restart_all"}),
    "admin": frozenset({"view", "view_logs", "restart_relay"}),
    "junior": frozenset({"view"}),
}

ENV_TO_ROLE = {
    "ADMIN_TOKEN_OWNER": "owner",
    "ADMIN_TOKEN_ADMIN": "admin",
    "ADMIN_TOKEN_JUNIOR": "junior",
    "ADMIN_TOKEN": "owner",
}


def _load_admins() -> dict:
    result = {}
    for env_name, role in ENV_TO_ROLE.items():
        token = (os.getenv(env_name) or "").strip()
        if not token or token in result:
            continue
        result[token] = role
    return result


def _get_role() -> str | None:
    token = request.cookies.get("admin_token") or request.args.get("token")
    if not token:
        return None
    return _load_admins().get(token)


def _has_perm(perm: str) -> bool:
    role = _get_role()
    if not role:
        return False
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


# ============================================================
# UI-КОНФИГ ИЗ YAML
# ============================================================
UI_DEFAULTS = {
    "title": "Token Harbor Relay",
    "subtitle": "Админка и статус ретранслятора",
    "theme": "dark",
    "accent_color": "#f63b3b",
    "sections": {
        "status": True,
        "controls": True,
        "footer": True,
    },
    "labels": {
        "controls_heading": "⚙️ Управление",
        "restart_relay": "🔄 Перезапустить ретранслятор",
        "hard_restart": "☠️ Жёсткий рестарт",
        "login_heading": "🔐 Вход",
        "login_hint": "Введи токен доступа",
        "login_button": "Войти",
    },
    "footer": {
        "copyright": "",
        "links": [],
    },
}


def _load_ui() -> dict:
    ui = {
        "sections": dict(UI_DEFAULTS["sections"]),
        "labels": dict(UI_DEFAULTS["labels"]),
        "footer": {
            "copyright": UI_DEFAULTS["footer"]["copyright"],
            "links": list(UI_DEFAULTS["footer"]["links"]),
        },
    }
    for k in ("title", "subtitle", "theme", "accent_color"):
        ui[k] = UI_DEFAULTS[k]

    try:
        with CONF_FILE.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        print(f"[ui] conf.yaml не прочитан: {e} — беру дефолты", flush=True)
        return ui

    src = cfg.get("ui") or {}
    if not isinstance(src, dict):
        return ui

    for key in ("title", "subtitle", "theme", "accent_color"):
        if isinstance(src.get(key), str):
            ui[key] = src[key]

    if isinstance(src.get("sections"), dict):
        for k, v in src["sections"].items():
            if isinstance(v, bool):
                ui["sections"][k] = v

    if isinstance(src.get("labels"), dict):
        for k, v in src["labels"].items():
            if isinstance(v, str):
                ui["labels"][k] = v

    if isinstance(src.get("footer"), dict):
        if isinstance(src["footer"].get("copyright"), str):
            ui["footer"]["copyright"] = src["footer"]["copyright"]
        if isinstance(src["footer"].get("links"), list):
            ui["footer"]["links"] = [
                lk for lk in src["footer"]["links"]
                if isinstance(lk, dict) and lk.get("label") and lk.get("url")
            ]

    return ui


def _hex_shift(hex_color: str, delta: int) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    r = max(0, min(255, r + delta))
    g = max(0, min(255, g + delta))
    b = max(0, min(255, b + delta))
    return f"#{r:02x}{g:02x}{b:02x}"


# ============================================================
# SELF-PING
# ============================================================
def _self_ping():
    if not SELF_URL:
        print("[ping] RENDER_EXTERNAL_URL/SELF_URL не задан — self-ping выключен", flush=True)
        return
    url = SELF_URL.rstrip("/") + "/health"
    print(f"[ping] старт: {url} каждые {PING_INTERVAL}s", flush=True)
    while not _stop.is_set():
        if _stop.wait(timeout=PING_INTERVAL):
            return
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "relay-self-ping"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"[ping] {url} → {resp.status}", flush=True)
        except urllib.error.HTTPError as e:
            print(f"[ping] {url} → HTTP {e.code}", flush=True)
        except Exception as e:
            print(f"[ping] {url} → ошибка: {e}", flush=True)


# ============================================================
# СУПЕРВИЗОР relay.py
# ============================================================
def _wait_relay_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not _stop.is_set():
        try:
            r = requests.get(f"{RELAY_URL}/health", timeout=1.0)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        _stop.wait(0.3)
    return False


def _drain_relay_stdout(proc: subprocess.Popen) -> None:
    try:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\n")
            print(f"[relay] {line}", flush=True)
            append_log(SERVICE_INDEX, "stdout", line)
    except Exception as exc:
        print(f"[supervisor] ошибка чтения stdout relay: {exc}", flush=True)


def _supervise_relay():
    global _relay_proc
    restarts = 0
    while not _stop.is_set():
        try:
            proc = subprocess.Popen(
                [sys.executable, RELAY_FILE],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            print(f"[supervisor] не смог запустить {RELAY_FILE}: {exc}", flush=True)
            mark_permanently_failed(SERVICE_INDEX, 1)
            return

        _relay_proc = proc
        _relay_ready.clear()
        mark_started(SERVICE_INDEX)
        print(f"[supervisor] {RELAY_FILE} запущен (PID {proc.pid})", flush=True)

        threading.Thread(target=_drain_relay_stdout, args=(proc,), daemon=True).start()

        if _wait_relay_ready(RELAY_READY_TIMEOUT):
            _relay_ready.set()
            mark_recovered(SERVICE_INDEX)
            print("[supervisor] relay готов принимать запросы", flush=True)
        else:
            print("[supervisor] relay не поднялся за отведённое время", flush=True)
            mark_unresponsive(SERVICE_INDEX)

        proc.wait()
        _relay_proc = None
        _relay_ready.clear()

        if _stop.is_set():
            return

        code = proc.returncode
        if code == 0:
            mark_clean_exit(SERVICE_INDEX)
            return

        restarts += 1
        mark_crashed(SERVICE_INDEX, code, will_restart=True)
        print(f"[supervisor] relay упал (код {code}), рестарт #{restarts}", flush=True)

        if _stop.wait(3.0):
            return


def _ensure_supervisor_started() -> None:
    global _supervisor_started
    if _supervisor_started:
        return
    _supervisor_started = True
    threading.Thread(target=_supervise_relay, daemon=True).start()


# ============================================================
# HTML
# ============================================================
def _build_admin_html() -> str:
    ui = _load_ui()
    l = ui["labels"]
    sections = dict(ui["sections"])

    if not (_has_perm("restart_relay") or _has_perm("restart_all")):
        sections["controls"] = False

    role = _get_role() or "?"
    role_badge = f"""
      <div style="background:var(--light-secondary);padding:10px 16px;border-radius:8px;
                  margin-bottom:20px;font-size:0.9em;color:var(--gray);display:flex;
                  justify-content:space-between;align-items:center;gap:12px;">
        <span>Роль: <b style="color:var(--primary-color)">{html_escape.escape(role)}</b></span>
        <button type="button"
                onclick="fetch('/logout',{{method:'POST'}}).then(()=>location.reload())"
                style="padding:6px 14px;border-radius:6px;border:none;background:var(--danger-color);color:white;cursor:pointer;font-weight:600;font-size:0.85em;">
          🚪 Выйти
        </button>
      </div>
    """

    parts = []
    if sections.get("controls"):
        parts.append(f"""
        <h2 style="font-size:1.5em;margin-bottom:20px;font-weight:700;">{html_escape.escape(l['controls_heading'])}</h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:30px;">
          <button type="button" onclick="if(confirm('Перезапустить ретранслятор?'))adminPost('/restart-relay')"
                  style="padding:10px 20px;border-radius:8px;border:none;background:var(--primary-color);color:white;cursor:pointer;font-weight:600;">
            {html_escape.escape(l['restart_relay'])}
          </button>
          <button type="button" onclick="if(confirm('Жёсткий рестарт? Render поднимет контейнер заново.'))adminPost('/restart-all')"
                  style="padding:10px 20px;border-radius:8px;border:none;background:var(--danger-color);color:white;cursor:pointer;font-weight:600;">
            {html_escape.escape(l['hard_restart'])}
          </button>
        </div>
        """)

    admin_section = ""
    if parts or role_badge:
        admin_section = (
            '<section class="admin-section" '
            'style="margin-top:50px;padding-top:30px;border-top:2px solid var(--border-color);">'
            + role_badge
            + "".join(parts)
            + '<script>async function adminPost(url){const r=await fetch(url,{method:"POST"});'
              'const j=await r.json();alert(JSON.stringify(j));}</script>'
            + "</section>"
        )

    html = STATUS_HTML_TEMPLATE

    accent = ui["accent_color"]
    accent_dark = _hex_shift(accent, -16)

    theme = ui["theme"] if ui["theme"] in ("dark", "light", "auto") else "dark"
    theme_attr = "" if theme == "auto" else f' data-theme="{theme}"'
    color_scheme = "light dark" if theme == "auto" else theme

    override_css = f":root {{ --primary-color: {accent}; --primary-dark: {accent_dark}; }}"

    html = html.replace(
        '<link rel="stylesheet" href="__STAYPRESENT_CSS_URL__" />',
        f"<style>{STATUS_CSS}</style><style>{override_css}</style>",
    )
    html = html.replace(
        '<script src="__STAYPRESENT_JS_URL__"></script>',
        f"<script>{STATUS_JS}</script>",
    )
    html = html.replace(
        '<link rel="icon" href="__STAYPRESENT_FAVICON_URL__" id="favicon">',
        '<link rel="icon" href="data:," id="favicon">',
    )

    html = html.replace("__STAYPRESENT_TITLE__", html_escape.escape(ui["title"]))
    html = html.replace("__STAYPRESENT_DESCRIPTION__", html_escape.escape(ui["subtitle"]))
    html = html.replace("__STAYPRESENT_COLOR_SCHEME__", color_scheme)
    html = html.replace("__STAYPRESENT_THEME_ATTR__", theme_attr)
    html = html.replace("__STAYPRESENT_OG_TAGS_BLOCK__", "")
    html = html.replace("__STAYPRESENT_FAVICON_SCRIPT__", "")

    if sections.get("footer"):
        copyright_text = (ui["footer"].get("copyright") or "").strip()
        copyright_block = f'<p>{html_escape.escape(copyright_text)}</p>' if copyright_text else ""
        links = ui["footer"].get("links") or []
        if links:
            link_html = " | ".join(
                f'<a href="{html_escape.escape(lk["url"], quote=True)}">'
                f'{html_escape.escape(lk["label"])}</a>'
                for lk in links
            )
            links_block = f'<div class="footer-links">{link_html}</div>'
        else:
            links_block = ""
    else:
        copyright_block = ""
        links_block = ""
        html = html.replace('<div class="powered-by">', '<div class="powered-by" hidden>')

    html = html.replace("__STAYPRESENT_COPYRIGHT_BLOCK__", copyright_block)
    html = html.replace("__STAYPRESENT_FOOTER_LINKS_BLOCK__", links_block)

    if not sections.get("status"):
        hide_css = (
            "<style>"
            "body > .container > header,"
            "body > .container > .status-summary,"
            "body > .container > .services,"
            "body > .container > .incidents,"
            "body > .container > .metrics"
            "{display:none !important;}"
            "</style>"
        )
        html = html.replace("</head>", hide_css + "</head>")

    if admin_section:
        html = html.replace("      <footer>", admin_section + "\n      <footer>", 1)

    return html


def _login_page(err: str = "") -> str:
    ui = _load_ui()
    l = ui["labels"]
    accent = ui["accent_color"]
    theme = ui["theme"] if ui["theme"] in ("dark", "light", "auto") else "dark"

    if theme == "light":
        bg, card, line, text, muted, input_bg = "#f8fafc", "#ffffff", "#e2e8f0", "#0f172a", "#64748b", "#f1f5f9"
    else:
        bg, card, line, text, muted, input_bg = "#0b1220", "#16202f", "#2d3b4e", "#e6edf3", "#94a3b8", "#0b1220"

    e = f'<p style="color:#f87171;margin:0 0 1rem">{html_escape.escape(err)}</p>' if err else ""

    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape.escape(ui['title'])}</title>
<style>
  body{{background:{bg};color:{text};font-family:-apple-system,sans-serif;
        display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;}}
  .box{{background:{card};padding:2rem;border-radius:12px;border:1px solid {line};width:360px;}}
  h1{{color:{accent};margin:0 0 0.5rem;font-size:1.4rem;}}
  p{{color:{muted};margin:0 0 1rem;font-size:.9rem;}}
  input{{width:100%;padding:10px 14px;margin:0 0 1rem;border:1px solid {line};border-radius:8px;
         background:{input_bg};color:{text};box-sizing:border-box;font-size:1rem;}}
  button{{width:100%;padding:10px;background:{accent};color:white;border:none;border-radius:8px;
          cursor:pointer;font-weight:600;font-size:1rem;}}
  button:hover{{opacity:.9;}}
</style></head><body>
<div class="box">
  <h1>{html_escape.escape(l['login_heading'])}</h1>
  <p>{html_escape.escape(l['login_hint'])}</p>
  {e}
  <form method="POST" action="/login">
    <input type="password" name="token" placeholder="ADMIN_TOKEN" autocomplete="off">
    <button type="submit">{html_escape.escape(l['login_button'])}</button>
  </form>
</div>
</body></html>"""


# ============================================================
# РОУТЫ
# ============================================================
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "relay_ready": _relay_ready.is_set(),
    })


@app.route("/")
def index():
    _ensure_supervisor_started()
    if not _load_admins():
        return Response(
            "<h1 style='font-family:sans-serif;text-align:center;margin-top:4rem'>"
            "Ни одного ADMIN_TOKEN_* не задано в env</h1>",
            status=503,
        )
    if not _has_perm("view"):
        return Response(_login_page(), mimetype="text/html")
    return Response(_build_admin_html(), mimetype="text/html")


@app.route("/login", methods=["POST"])
def login():
    token = (request.form.get("token") or "").strip()
    admins = _load_admins()
    if token and token in admins:
        resp = Response(status=302)
        resp.headers["Location"] = "/"
        resp.set_cookie(
            "admin_token", token,
            httponly=True, samesite="Strict", secure=True,
        )
        return resp
    return Response(_login_page("Неверный токен"), status=403, mimetype="text/html")


@app.route("/logout", methods=["POST"])
def logout():
    resp = Response(status=302)
    resp.headers["Location"] = "/"
    resp.delete_cookie("admin_token")
    return resp


@app.route("/api/status.json")
def api_status_json():
    if not _has_perm("view"):
        return jsonify({"error": "forbidden"}), 403

    timezone = "auto"
    time_format = "24h"
    try:
        with CONF_FILE.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ui_cfg = cfg.get("ui") or {}
        if isinstance(ui_cfg, dict):
            if isinstance(ui_cfg.get("timezone"), str):
                timezone = ui_cfg["timezone"]
            if isinstance(ui_cfg.get("time_format"), str):
                time_format = ui_cfg["time_format"]
    except (FileNotFoundError, yaml.YAMLError):
        pass

    is_elevated = _has_perm("view_logs")
    data = snapshot(admin=is_elevated, timezone=timezone, time_format=time_format)
    data["admin"] = is_elevated
    data["admin_available"] = False
    data["poll_seconds"] = 10
    return jsonify(data)


@app.route("/v1/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy_v1(subpath):
    _ensure_supervisor_started()

    if not _relay_ready.is_set():
        if not _relay_ready.wait(timeout=RELAY_READY_WAIT_FOR_CLIENT):
            return jsonify({"error": "relay not ready, try again shortly"}), 503

    target = f"{RELAY_URL}/v1/{subpath}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection")
    }

    try:
        upstream = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=request.get_data(),
            stream=True,
            timeout=(10, 300),
        )
    except requests.RequestException as exc:
        mark_unresponsive(SERVICE_INDEX)
        return jsonify({"error": "relay unreachable", "detail": str(exc)}), 502

    mark_started(SERVICE_INDEX)
    mark_recovered(SERVICE_INDEX)

    excluded = {"content-encoding", "transfer-encoding", "connection", "content-length"}
    resp_headers = [
        (k, v) for k, v in upstream.raw.headers.items()
        if k.lower() not in excluded
    ]

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=resp_headers,
        direct_passthrough=True,
    )


@app.route("/restart-relay", methods=["POST"])
def restart_relay_route():
    if not _has_perm("restart_relay"):
        return jsonify({"error": "forbidden"}), 403
    proc = _relay_proc
    if proc is None:
        return jsonify({"status": "relay not running"})
    try:
        proc.terminate()
    except OSError:
        pass
    return jsonify({"status": "relay termination signaled, supervisor will restart"})


@app.route("/restart-all", methods=["POST"])
def restart_all_route():
    if not _has_perm("restart_all"):
        return jsonify({"error": "forbidden"}), 403

    def _hard_exit():
        proc = _relay_proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
        os._exit(0)

    threading.Timer(1.0, _hard_exit).start()
    return jsonify({"status": "dying"})


# ============================================================
# ЗАПУСК
# ============================================================
def _shutdown(signum, frame):
    print(f"[main] сигнал {signum}, останавливаюсь", flush=True)
    _stop.set()
    proc = _relay_proc
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    registry_reset(
        [{"file": SERVICE_KEY, "module": None, "args": [], "env": {}, "status": None}],
        display_names=[SERVICE_NAME],
        track_web_server=False,
        bot_status=[True],
    )
    update_service_override(SERVICE_KEY, SERVICE_NAME, SERVICE_DESCRIPTION)

    _ensure_supervisor_started()
    threading.Thread(target=_self_ping, daemon=True).start()

    if waitress_serve is not None:
        print(f"[main] waitress на порту {PORT}", flush=True)
        waitress_serve(
            app,
            host="0.0.0.0",
            port=PORT,
            threads=2,
            channel_timeout=300,
            cleanup_interval=30,
        )
    else:
        print(f"[main] flask dev-server на порту {PORT}", flush=True)
        app.run(host="0.0.0.0", port=PORT, threaded=True)