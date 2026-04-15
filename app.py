from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

from convert_table_to_ics import (
    build_calendar_events,
    build_ics_text,
    parse_date,
    read_table03,
    write_json_sample,
)
from scrape_timetable import login_and_fetch_timetable

app = Flask(__name__)

JOBS_DIR = Path("/tmp/output/jobs") if os.getenv("VERCEL") else Path("output/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def find_timetable_csv(tables_dir: Path) -> Path:
    preferred = [tables_dir / "table_02.csv", tables_dir / "table_03.csv"]
    for path in preferred:
        if path.exists():
            return path
    raise RuntimeError("未找到 table_02.csv / table_03.csv，请检查抓取结果。")


def parse_form_dates(
    first_semester_start: str,
    first_semester_end: str,
    second_semester_start: str,
    second_semester_end: str,
) -> tuple[datetime, datetime, datetime, datetime]:
    first_start = parse_date(first_semester_start)
    first_end = parse_date(first_semester_end)
    second_start = parse_date(second_semester_start)
    second_end = parse_date(second_semester_end)

    if first_end < first_start:
        raise ValueError("第一学期结束时间不能早于开始时间。")
    if second_end < second_start:
        raise ValueError("第二学期结束时间不能早于开始时间。")

    return first_start, first_end, second_start, second_end


def generate_job(
    user_id: str,
    password: str,
    first_semester_start: str,
    first_semester_end: str,
    second_semester_start: str,
    second_semester_end: str,
) -> tuple[str, int]:
    first_start, first_end, second_start, second_end = parse_form_dates(
        first_semester_start,
        first_semester_end,
        second_semester_start,
        second_semester_end,
    )

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    login_and_fetch_timetable(user_id, password, job_dir)

    timetable_csv = find_timetable_csv(job_dir / "tables")
    courses = read_table03(timetable_csv)
    write_json_sample(courses, job_dir / "calendar_events.json")
    events, event_count = build_calendar_events(
        courses,
        first_start=first_start,
        first_end=first_end,
        second_start=second_start,
        second_end=second_end,
    )
    (job_dir / "kyushu_timetable.ics").write_text(build_ics_text(events), encoding="utf-8")
    return job_id, event_count


@app.get("/")
def index():
    today = datetime.now().date()
    first_start = datetime(today.year, 4, 10).date()
    first_end = first_start + timedelta(weeks=8) - timedelta(days=1)
    second_start = first_end + timedelta(days=1)
    second_end = second_start + timedelta(weeks=8) - timedelta(days=1)

    defaults = {
        "first_semester_start": first_start.isoformat(),
        "first_semester_end": first_end.isoformat(),
        "second_semester_start": second_start.isoformat(),
        "second_semester_end": second_end.isoformat(),
    }
    return render_template("index.html", defaults=defaults, error="")


@app.post("/generate")
def generate():
    user_id = (request.form.get("user_id") or "").strip()
    password = request.form.get("password") or ""
    first_semester_start = request.form.get("first_semester_start") or ""
    first_semester_end = request.form.get("first_semester_end") or ""
    second_semester_start = request.form.get("second_semester_start") or ""
    second_semester_end = request.form.get("second_semester_end") or ""

    defaults = {
        "user_id": user_id,
        "first_semester_start": first_semester_start,
        "first_semester_end": first_semester_end,
        "second_semester_start": second_semester_start,
        "second_semester_end": second_semester_end,
    }

    if not user_id or not password:
        return render_template("index.html", error="请先输入 SSO-KID 和密码。", defaults=defaults), 400

    try:
        job_id, event_count = generate_job(
            user_id=user_id,
            password=password,
            first_semester_start=first_semester_start,
            first_semester_end=first_semester_end,
            second_semester_start=second_semester_start,
            second_semester_end=second_semester_end,
        )
    except ValueError as exc:
        return render_template("index.html", error=str(exc), defaults=defaults), 400
    except Exception as exc:
        return render_template("index.html", error=f"处理失败: {exc}", defaults=defaults), 500

    return redirect(url_for("result", job_id=job_id, event_count=event_count))


@app.post("/api/generate")
def api_generate():
    user_id = (request.form.get("user_id") or "").strip()
    password = request.form.get("password") or ""
    first_semester_start = request.form.get("first_semester_start") or ""
    first_semester_end = request.form.get("first_semester_end") or ""
    second_semester_start = request.form.get("second_semester_start") or ""
    second_semester_end = request.form.get("second_semester_end") or ""

    if not user_id or not password:
        return jsonify({"ok": False, "message": "请先输入学号和密码。"}), 400

    try:
        job_id, event_count = generate_job(
            user_id=user_id,
            password=password,
            first_semester_start=first_semester_start,
            first_semester_end=first_semester_end,
            second_semester_start=second_semester_start,
            second_semester_end=second_semester_end,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "message": f"处理失败: {exc}"}), 500

    ics_http_url = url_for("download_ics", job_id=job_id, _external=True)
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "event_count": event_count,
            "result_url": url_for("result", job_id=job_id, event_count=event_count, _external=True),
            "ics_download_url": ics_http_url,
            "ics_webcal_url": ics_http_url.replace("https://", "webcal://").replace("http://", "webcal://"),
            "json_url": url_for("download_json", job_id=job_id, _external=True),
        }
    )


@app.get("/result/<job_id>")
def result(job_id: str):
    job_dir = JOBS_DIR / job_id
    ics_path = job_dir / "kyushu_timetable.ics"
    if not ics_path.exists():
        return "Not Found", 404

    ics_http_url = url_for("download_ics", job_id=job_id, _external=True)
    ics_webcal_url = ics_http_url.replace("https://", "webcal://").replace("http://", "webcal://")

    return render_template(
        "result.html",
        job_id=job_id,
        event_count=request.args.get("event_count", "0"),
        ics_http_url=ics_http_url,
        ics_webcal_url=ics_webcal_url,
        json_url=url_for("download_json", job_id=job_id, _external=True),
    )


@app.get("/calendar/<job_id>/kyushu_timetable.ics")
def download_ics(job_id: str):
    path = JOBS_DIR / job_id / "kyushu_timetable.ics"
    if not path.exists():
        return "Not Found", 404

    return send_file(path, mimetype="text/calendar", as_attachment=False, download_name="kyushu_timetable.ics")


@app.get("/calendar/<job_id>/calendar_events.json")
def download_json(job_id: str):
    path = JOBS_DIR / job_id / "calendar_events.json"
    if not path.exists():
        return "Not Found", 404

    return send_file(path, mimetype="application/json", as_attachment=True, download_name="calendar_events.json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
