"""Веб-интерфейс metalparser (Flask).

Запуск:
    pip install -r requirements.txt
    export PARSER_PASSWORD=ваш_пароль       # пароль для входа в раздел
    python -m webapp.app                     # затем открыть http://127.0.0.1:5000

Возможности:
  * запароленный вход (логин-страница + сессия);
  * форма: путь к дампу ЕГРЮЛ, набор ОКВЭД, статус, режим обогащения, лимит;
  * фоновый прогон с прогрессом;
  * таблица результатов (название, ИНН, вид деятельности, телефоны/почты/сайты);
  * выгрузка CSV / Excel.

Переменные окружения:
  PARSER_PASSWORD  — пароль для входа (обязательно для защиты раздела);
  PARSER_USERNAME  — логин (по умолчанию 'admin');
  SECRET_KEY       — ключ подписи сессии (по умолчанию случайный на запуск);
  PORT             — порт (по умолчанию 5000).
"""
from __future__ import annotations

import functools
import json
import os
import re
import secrets
import threading
import time
import uuid

from flask import (
    Flask, render_template, request, jsonify, send_file, abort,
    session, redirect, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from metalparser.export import CsvAppender, write_excel_from_csv, read_existing_keys
from metalparser.okved import OKVED_SETS, OKVED_SET_LABELS, DEFAULT_SET, OKVED_TREE
from metalparser.pipeline import PipelineConfig, iter_run, count_companies

app = Flask(__name__)
# Работа за обратным прокси (nginx) — корректные схемы/префиксы для поддомена/пути
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Учётные данные раздела
PARSER_USERNAME = os.environ.get("PARSER_USERNAME", "admin")
PARSER_PASSWORD = os.environ.get("PARSER_PASSWORD", "")

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Сохранённые секреты (куки/ключ) — файл в data/, в git не попадает.
SECRETS_PATH = os.path.join(OUTPUT_DIR, "secrets.json")


def load_saved() -> dict:
    try:
        with open(SECRETS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_saved(**kv) -> None:
    data = load_saved()
    for k, v in kv.items():
        if v:
            data[k] = v
    with open(SECRETS_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    try:
        os.chmod(SECRETS_PATH, 0o600)  # на Windows может игнорироваться — не критично
    except Exception:  # noqa: BLE001
        pass


def resolve_cookie(form_val: str | None) -> str | None:
    return (form_val or "").strip() or load_saved().get("cookie") or os.environ.get("CHECKO_COOKIE") or None


_KEYS_FILE = os.path.join(OUTPUT_DIR, "api_keys.txt")


def resolve_api_key(form_val: str | None) -> str | None:
    from metalparser.checko import read_keys_file
    return ((form_val or "").strip() or read_keys_file(_KEYS_FILE)
            or load_saved().get("api_key") or os.environ.get("CHECKO_API_KEY") or None)


def parse_okved_codes(form) -> list[str]:
    """Коды ОКВЭД из галочек дерева + из текстового поля (через запятую/пробел)."""
    codes = [c.strip() for c in form.getlist("okved_codes") if c.strip()]
    for tok in re.split(r"[,\s;]+", form.get("okved_text", "") or ""):
        tok = tok.strip().replace(",", ".")
        if tok:
            codes.append(tok)
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

# Состояние задач в памяти (один процесс). job_id -> dict
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

# ---------- Автосбор в общую базу (data/base.csv), фоновый цикл ----------
BASE_CSV = os.path.join(OUTPUT_DIR, "base.csv")
BASE_XLSX = os.path.join(OUTPUT_DIR, "base.xlsx")
AUTO_PATH = os.path.join(OUTPUT_DIR, "auto.json")
AUTO = {"enabled": False, "interval": 24.0, "sel": None, "running": False,
        "last_run": "", "last_added": 0, "total": 0, "next_run": "", "error": ""}
_AUTO_THREAD = None


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def parse_selection(f) -> dict:
    """Выбор параметров сбора из формы (без секретов — ключи/куки берутся при запуске)."""
    return {
        "source": f.get("source", "api"),
        "okved": parse_okved_codes(f),
        "only_active": f.get("only_active", "on") == "on",
        "main_okved_only": f.get("main_okved_only") == "on",
        "delay": float(f.get("delay", "1.5") or 1.5),
        "concurrency": int(f.get("concurrency", "3") or 3),
        "browser": f.get("browser") == "on",
        "regions": [r.strip() for r in (f.get("regions") or "").replace(",", " ").split() if r.strip()],
    }


def _auto_save():
    try:
        with open(AUTO_PATH, "w", encoding="utf-8") as fh:
            json.dump({"enabled": AUTO["enabled"], "interval": AUTO["interval"],
                       "sel": AUTO["sel"]}, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _auto_load():
    try:
        with open(AUTO_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        AUTO["enabled"] = bool(d.get("enabled"))
        AUTO["interval"] = float(d.get("interval") or 24.0)
        AUTO["sel"] = d.get("sel")
        AUTO["total"] = len(read_existing_keys(BASE_CSV))
    except Exception:  # noqa: BLE001
        pass


def _progress_path(csv_path: str) -> str:
    """Сайдкар-файл с кодами ОКВЭД, уже полностью пройденными (для докачки)."""
    return os.path.splitext(csv_path)[0] + ".progress.json"


def _load_done(csv_path: str) -> set:
    try:
        with open(_progress_path(csv_path), encoding="utf-8") as fh:
            return set(json.load(fh).get("done_codes", []))
    except Exception:  # noqa: BLE001
        return set()


def _save_done(csv_path: str, done: set) -> None:
    tmp = _progress_path(csv_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"done_codes": sorted(done)}, fh, ensure_ascii=False)
    os.replace(tmp, _progress_path(csv_path))


def _auto_config(sel: dict) -> PipelineConfig:
    saved = load_saved()
    return PipelineConfig(
        source=sel.get("source", "api"), okved_set="none", extra_okved=sel.get("okved", []),
        only_active=sel.get("only_active", True), main_okved_only=sel.get("main_okved_only", False),
        api_key=resolve_api_key(None), cookie=resolve_cookie(None),
        browser=sel.get("browser", False), user_agent=saved.get("ua") or os.environ.get("CHECKO_UA"),
        delay=sel.get("delay", 1.5), concurrency=sel.get("concurrency", 3),
        regions=sel.get("regions", []),
    )


def _auto_run_once():
    sel = AUTO["sel"]
    if not sel or not sel.get("okved"):
        return
    with _LOCK:
        AUTO["running"] = True
        AUTO["error"] = ""
    n = 0
    appender = CsvAppender(BASE_CSV)
    done = _load_done(BASE_CSV)

    def on_code_done(code):
        done.add(code)
        _save_done(BASE_CSV, done)

    try:
        for c in iter_run(_auto_config(sel), skip=read_existing_keys(BASE_CSV),
                          done_codes=done, on_code_done=on_code_done):
            appender.write(c)
            n += 1
        appender.close()
        write_excel_from_csv(BASE_CSV, BASE_XLSX)
        with _LOCK:
            AUTO["last_added"] = n
            AUTO["total"] = len(read_existing_keys(BASE_CSV))
            AUTO["last_run"] = _now()
    except Exception as exc:  # noqa: BLE001
        appender.close()
        with _LOCK:
            AUTO["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _LOCK:
            AUTO["running"] = False


def _auto_loop():
    while AUTO["enabled"]:
        _auto_run_once()
        total = max(60.0, AUTO["interval"] * 3600)
        AUTO["next_run"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + total))
        slept = 0.0
        while AUTO["enabled"] and slept < total:
            time.sleep(min(30.0, total - slept))
            slept += 30.0


def _auto_ensure_thread():
    global _AUTO_THREAD
    if _AUTO_THREAD and _AUTO_THREAD.is_alive():
        return
    _AUTO_THREAD = threading.Thread(target=_auto_loop, daemon=True)
    _AUTO_THREAD.start()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if not PARSER_PASSWORD:
        # Защита не настроена — явно предупреждаем, вход не пускаем
        return render_template("login.html",
                               error="Пароль не задан: установите переменную окружения "
                                     "PARSER_PASSWORD на сервере и перезапустите.",
                               disabled=True), 503
    if request.method == "POST":
        user = (request.form.get("username") or "").strip()
        pwd = request.form.get("password") or ""
        if secrets.compare_digest(user, PARSER_USERNAME) and \
                secrets.compare_digest(pwd, PARSER_PASSWORD):
            session["auth"] = True
            return redirect(url_for("index"))
        error = "Неверный логин или пароль"
    return render_template("login.html", error=error, disabled=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _worker(job_id: str, config: PipelineConfig):
    job = JOBS[job_id]
    csv_path = os.path.join(OUTPUT_DIR, f"{job_id}.csv")
    xlsx_path = os.path.join(OUTPUT_DIR, f"{job_id}.xlsx")
    appender = CsvAppender(csv_path)   # потоковая запись — результат не теряется
    try:
        def on_progress(scanned):
            with _LOCK:
                job["scanned"] = scanned

        # пишем каждую компанию сразу и в файл, и в таблицу
        for c in iter_run(config, on_progress=on_progress):
            appender.write(c)
            with _LOCK:
                job["found"] += 1
                if len(job["rows"]) < 500:
                    job["rows"].append(c.to_row())
        appender.close()
        write_excel_from_csv(csv_path, xlsx_path)
        with _LOCK:
            job["csv"] = csv_path
            job["xlsx"] = xlsx_path
            job["status"] = "done"
            job["finished"] = time.time()
    except Exception as exc:  # noqa: BLE001
        appender.close()
        # сохраняем то, что успели собрать
        try:
            write_excel_from_csv(csv_path, xlsx_path)
        except Exception:  # noqa: BLE001
            pass
        with _LOCK:
            if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                job["csv"] = csv_path
                job["xlsx"] = xlsx_path
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"


@app.route("/")
@login_required
def index():
    saved = load_saved()
    return render_template(
        "index.html",
        tree=OKVED_TREE,
        default_set=DEFAULT_SET,
        cookie_saved=bool(saved.get("cookie") or os.environ.get("CHECKO_COOKIE")),
        key_saved=bool(saved.get("api_key") or os.environ.get("CHECKO_API_KEY")),
    )


@app.route("/save-secret", methods=["POST"])
@login_required
def save_secret():
    """Сохраняет куки и/или API-ключ в data/secrets.json (не в git)."""
    cookie = (request.form.get("cookie") or "").strip()
    api_key = (request.form.get("api_key") or "").strip()
    if not cookie and not api_key:
        return jsonify({"error": "Нечего сохранять — вставьте куки или ключ"}), 400
    save_saved(cookie=cookie, api_key=api_key)
    return jsonify({"saved": True,
                    "cookie": bool(load_saved().get("cookie")),
                    "api_key": bool(load_saved().get("api_key"))})


def _auto_status_payload():
    with _LOCK:
        return {k: AUTO[k] for k in
                ("enabled", "interval", "running", "last_run", "last_added", "total", "next_run", "error")}


@app.route("/auto/start", methods=["POST"])
@login_required
def auto_start():
    sel = parse_selection(request.form)
    if not sel["okved"]:
        return jsonify({"error": "Выберите ОКВЭД (галочки или список) для автосбора"}), 400
    AUTO["sel"] = sel
    AUTO["interval"] = float(request.form.get("interval", "24") or 24)
    AUTO["enabled"] = True
    _auto_save()
    _auto_ensure_thread()
    return jsonify(_auto_status_payload())


@app.route("/auto/stop", methods=["POST"])
@login_required
def auto_stop():
    AUTO["enabled"] = False
    _auto_save()
    return jsonify(_auto_status_payload())


@app.route("/auto/run-now", methods=["POST"])
@login_required
def auto_run_now():
    sel = parse_selection(request.form)
    if sel["okved"]:
        AUTO["sel"] = sel
        _auto_save()
    if not AUTO["sel"]:
        return jsonify({"error": "Сначала задайте ОКВЭД"}), 400
    if AUTO["running"]:
        return jsonify({"error": "Уже идёт сбор — подождите"}), 409
    threading.Thread(target=_auto_run_once, daemon=True).start()
    return jsonify({"started": True})


@app.route("/auto/status")
@login_required
def auto_status():
    return jsonify(_auto_status_payload())


@app.route("/start", methods=["POST"])
@login_required
def start():
    f = request.form
    source = f.get("source", "api")             # api | site | egrul
    egrul_path = (f.get("egrul_path") or "").strip()
    api_key = resolve_api_key(f.get("api_key"))
    cookie = resolve_cookie(f.get("cookie"))
    regions = [r.strip() for r in (f.get("regions") or "").replace(",", " ").split() if r.strip()]

    if source == "egrul":
        if not egrul_path or not os.path.exists(egrul_path):
            return jsonify({"error": "Укажите существующий путь к дампу ЕГРЮЛ"}), 400
    elif source == "api" and not api_key:
        return jsonify({"error": "Для источника API нужен ключ checko (поле «API-ключ» или env CHECKO_API_KEY)"}), 400
    elif source == "site" and not cookie and not (f.get("browser") == "on"):
        return jsonify({"error": "Для источника «Сайт» нужны куки авторизованного checko (поле «Куки» или env CHECKO_COOKIE), либо включите режим браузера с сохранённым профилем"}), 400

    okved_codes = parse_okved_codes(f)
    if not okved_codes:
        return jsonify({"error": "Выберите ОКВЭД галочками или впишите коды через запятую"}), 400

    enrich_mode = f.get("enrich", "none")  # none | api | html (для egrul)
    config = PipelineConfig(
        source=source,
        egrul_path=egrul_path,
        okved_set="none",
        extra_okved=okved_codes,
        only_active=f.get("only_active", "on") == "on",
        main_okved_only=f.get("main_okved_only") == "on",
        enrich_contacts=f.get("no_contacts") != "on",
        enrich=enrich_mode != "none",
        prefer_api=enrich_mode == "api",
        api_key=api_key,
        cookie=cookie,
        browser=(f.get("browser") == "on"),
        user_agent=load_saved().get("ua") or os.environ.get("CHECKO_UA"),
        delay=float(f.get("delay", "1.5") or 1.5),
        limit=int(f.get("limit", "0") or 0),
        concurrency=int(f.get("concurrency", "3") or 3),
        regions=regions,
    )

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "running", "found": 0, "scanned": 0, "rows": [],
        "error": "", "started": time.time(),
    }
    threading.Thread(target=_worker, args=(job_id, config), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/count", methods=["POST"])
@login_required
def count():
    """Считает число компаний по выбранным ОКВЭД без скачивания (поле ЗапВсего)."""
    f = request.form
    api_key = resolve_api_key(f.get("api_key"))
    if not api_key:
        return jsonify({"error": "Для подсчёта нужен ключ checko"}), 400
    okved_codes = parse_okved_codes(f)
    if not okved_codes:
        return jsonify({"error": "Выберите ОКВЭД галочками или впишите коды через запятую"}), 400
    regions = [r.strip() for r in (f.get("regions") or "").replace(",", " ").split() if r.strip()]
    config = PipelineConfig(
        source="api", okved_set="none", extra_okved=okved_codes,
        only_active=f.get("only_active", "on") == "on",
        api_key=api_key, regions=regions,
    )
    try:
        per, total = count_companies(config, delay=0.3)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502
    return jsonify({"per": [{"code": c, "count": n} for c, n in per], "total": total})


@app.route("/status/<job_id>")
@login_required
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        abort(404)
    with _LOCK:
        return jsonify({
            "status": job["status"],
            "found": job["found"],
            "scanned": job["scanned"],
            "error": job.get("error", ""),
            "rows": job["rows"][:500],  # в таблицу — первые 500
            "has_files": bool(job.get("csv")),
        })


@app.route("/download/<job_id>.<fmt>")
@login_required
def download(job_id: str, fmt: str):
    job = JOBS.get(job_id)
    if not job or fmt not in ("csv", "xlsx"):
        abort(404)
    path = job.get(fmt)
    if not path or not os.path.exists(path):
        abort(404)
    name = f"metal_companies_{job_id}.{fmt}"
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/files")
@login_required
def files():
    """Список готовых выгрузок (CSV/Excel) из папки data/."""
    items = []
    for fn in sorted(os.listdir(OUTPUT_DIR)):
        if fn.lower().endswith((".csv", ".xlsx")):
            fp = os.path.join(OUTPUT_DIR, fn)
            items.append({
                "name": fn,
                "size": f"{os.path.getsize(fp) / 1024:.1f} КБ",
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(fp))),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return render_template("files.html", files=items)


@app.route("/files/<path:name>")
@login_required
def files_download(name: str):
    """Безопасная отдача файла из data/ (без выхода за пределы папки)."""
    if "/" in name or "\\" in name or not name.lower().endswith((".csv", ".xlsx")):
        abort(404)
    path = os.path.normpath(os.path.join(OUTPUT_DIR, name))
    if not path.startswith(os.path.normpath(OUTPUT_DIR) + os.sep) or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=name)


# Восстановление автосбора при старте (переживает перезапуск serve.py)
_auto_load()
if AUTO["enabled"] and AUTO["sel"]:
    _auto_ensure_thread()


if __name__ == "__main__":
    # HOST=0.0.0.0 — доступ снаружи; PORT — порт; FLASK_DEBUG=1 — отладка (НЕ для публичного IP)
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
