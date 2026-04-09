from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from convert_table_to_ics import (
    build_calendar_events,
    build_ics_text,
    parse_date,
    read_table03,
    write_json_sample,
)
from scrape_timetable import login_and_fetch_timetable

app = Flask(__name__)

JOBS_DIR = Path("output/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)


def parse_form_dates(form) -> tuple[datetime, datetime, datetime, datetime]:
    first_start = parse_date(form["first_semester_start"])
    first_end = parse_date(form["first_semester_end"])
    second_start = parse_date(form["second_semester_start"])
    second_end = parse_date(form["second_semester_end"])
    if first_end < first_start:
        raise ValueError("第一学期结束时间不能早于开始时间。")
    if second_end < second_start:
        raise ValueError("第二学期结束时间不能早于开始时间。")
    return first_start, first_end, second_start, second_end


@app.get("/")
def index():
    today = datetime.now().date()
    return render_template(
        "index.html",
        defaults={
            "first_semester_start": f"{today.year}-04-10",
            "first_semester_end": f"{today.year}-08-07",
            "second_semester_start": f"{today.year}-10-01",
            "second_semester_end": f"{today.year + 1}-02-05",
        },
    )


@app.post("/generate")
def generate():
    user_id = (request.form.get("user_id") or "").strip()
    password = request.form.get("password") or ""
    defaults = {
        "user_id": user_id,
        "first_semester_start": request.form.get("first_semester_start", ""),
        "first_semester_end": request.form.get("first_semester_end", ""),
        "second_semester_start": request.form.get("second_semester_start", ""),
        "second_semester_end": request.form.get("second_semester_end", ""),
    }
    if not user_id or not password:
        return render_template("index.html", error="请先输入 SSO-KID 和密码。", defaults=defaults), 400

    try:
        first_start, first_end, second_start, second_end = parse_form_dates(request.form)
    except Exception as exc:
        return render_template("index.html", error=str(exc), defaults=defaults), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 1) scrape timetable html/tables
        login_and_fetch_timetable(user_id=user_id, password=password, output_dir=job_dir)

        # 2) convert table_03.csv -> JSON + ICS
        table03 = job_dir / "tables" / "table_03.csv"
        if not table03.exists():
            raise RuntimeError("未找到 table_03.csv，请检查抓取结果。")

        courses = read_table03(table03)
        write_json_sample(courses, job_dir / "calendar_events.json")
        events, event_count = build_calendar_events(
            courses,
            first_start=first_start,
            first_end=first_end,
            second_start=second_start,
            second_end=second_end,
        )
        (job_dir / "kyushu_timetable.ics").write_text(build_ics_text(events), encoding="utf-8")

    except Exception as exc:
        return render_template("index.html", error=f"处理失败：{exc}", defaults=defaults), 500

    return redirect(url_for("result", job_id=job_id, event_count=event_count))


@app.get("/result/<job_id>")
def result(job_id: str):
    job_dir = JOBS_DIR / job_id
    ics_path = job_dir / "kyushu_timetable.ics"
    json_path = job_dir / "calendar_events.json"
    if not ics_path.exists():
        abort(404)

    ics_http_url = url_for("download_ics", job_id=job_id, _external=True)
    ics_webcal_url = ics_http_url.replace("https://", "webcal://").replace("http://", "webcal://")
    return render_template(
        "result.html",
        job_id=job_id,
        event_count=request.args.get("event_count", "0"),
        ics_http_url=ics_http_url,
        ics_webcal_url=ics_webcal_url,
        json_url=url_for("download_json", job_id=job_id),
    )


@app.get("/calendar/<job_id>/kyushu_timetable.ics")
def download_ics(job_id: str):
    path = JOBS_DIR / job_id / "kyushu_timetable.ics"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="text/calendar", as_attachment=False, download_name="kyushu_timetable.ics")


@app.get("/calendar/<job_id>/calendar_events.json")
def download_json(job_id: str):
    path = JOBS_DIR / job_id / "calendar_events.json"
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="application/json", as_attachment=True, download_name="calendar_events.json")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
