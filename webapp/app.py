"""Веб-интерфейс metalparser (Flask).

Запуск:
    pip install -r requirements.txt
    python -m webapp.app        # затем открыть http://127.0.0.1:5000

Возможности:
  * форма: путь к дампу ЕГРЮЛ, набор ОКВЭД, статус, режим обогащения, лимит;
  * фоновый прогон с прогрессом;
  * таблица результатов (название, ИНН, вид деятельности, телефоны/почты/сайты);
  * выгрузка CSV / Excel.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

from flask import Flask, render_template, request, jsonify, send_file, abort

from metalparser.export import write_csv, write_excel
from metalparser.okved import OKVED_SETS, OKVED_SET_LABELS, DEFAULT_SET
from metalparser.pipeline import PipelineConfig, run

app = Flask(__name__)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Состояние задач в памяти (один процесс). job_id -> dict
JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _worker(job_id: str, config: PipelineConfig):
    job = JOBS[job_id]
    try:
        def on_company(c):
            with _LOCK:
                job["found"] += 1
                job["rows"].append(c.to_row())

        def on_progress(scanned):
            with _LOCK:
                job["scanned"] = scanned

        companies = run(config, on_company=on_company, on_progress=on_progress)

        csv_path = os.path.join(OUTPUT_DIR, f"{job_id}.csv")
        xlsx_path = os.path.join(OUTPUT_DIR, f"{job_id}.xlsx")
        write_csv(companies, csv_path)
        write_excel(companies, xlsx_path)
        with _LOCK:
            job["csv"] = csv_path
            job["xlsx"] = xlsx_path
            job["status"] = "done"
            job["finished"] = time.time()
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            job["status"] = "error"
            job["error"] = f"{type(exc).__name__}: {exc}"


@app.route("/")
def index():
    return render_template(
        "index.html",
        okved_sets={k: OKVED_SET_LABELS.get(k, k) for k in sorted(OKVED_SETS)},
        default_set=DEFAULT_SET,
    )


@app.route("/start", methods=["POST"])
def start():
    f = request.form
    egrul_path = (f.get("egrul_path") or "").strip()
    if not egrul_path or not os.path.exists(egrul_path):
        return jsonify({"error": "Укажите существующий путь к дампу ЕГРЮЛ"}), 400

    enrich_mode = f.get("enrich", "none")  # none | api | html
    config = PipelineConfig(
        egrul_path=egrul_path,
        okved_set=f.get("okved_set", DEFAULT_SET),
        only_active=f.get("only_active", "on") == "on",
        enrich=enrich_mode != "none",
        prefer_api=enrich_mode == "api",
        api_key=(f.get("api_key") or "").strip() or None,
        delay=float(f.get("delay", "1.5") or 1.5),
        limit=int(f.get("limit", "0") or 0),
    )

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "status": "running", "found": 0, "scanned": 0, "rows": [],
        "error": "", "started": time.time(),
    }
    threading.Thread(target=_worker, args=(job_id, config), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
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
def download(job_id: str, fmt: str):
    job = JOBS.get(job_id)
    if not job or fmt not in ("csv", "xlsx"):
        abort(404)
    path = job.get(fmt)
    if not path or not os.path.exists(path):
        abort(404)
    name = f"metal_companies_{job_id}.{fmt}"
    return send_file(path, as_attachment=True, download_name=name)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
