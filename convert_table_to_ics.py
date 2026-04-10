from __future__ import annotations

import argparse
import csv
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays

TZ = ZoneInfo("Asia/Tokyo")

MONDAY = "\u6708\u66dc\u65e5"
TUESDAY = "\u706b\u66dc\u65e5"
WEDNESDAY = "\u6c34\u66dc\u65e5"
THURSDAY = "\u6728\u66dc\u65e5"
FRIDAY = "\u91d1\u66dc\u65e5"
SATURDAY = "\u571f\u66dc\u65e5"
SUNDAY = "\u65e5\u66dc\u65e5"

WEEKDAYS = [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY]
WEEKDAY_INDEX = {MONDAY: 0, TUESDAY: 1, WEDNESDAY: 2, THURSDAY: 3, FRIDAY: 4, SATURDAY: 5, SUNDAY: 6}

PERIOD_LABEL_RE = re.compile(r"^\s*(\d+)\u6642\u9650\s*$")
YEAR_ROUND_MARK = "\u901a\u5e74"
COURSE_CODE_RE = re.compile(r"\d{8}")

# Adjust these if your department uses different period times.
PERIOD_TIMES: dict[int, tuple[str, str]] = {
    1: ("08:40", "10:10"),
    2: ("10:30", "12:00"),
    3: ("13:00", "14:30"),
    4: ("14:50", "16:20"),
    5: ("16:40", "18:10"),
    6: ("18:30", "20:00"),
    7: ("20:10", "21:40"),
}


@dataclass
class CourseSlot:
    course_code: str
    course_name: str
    teacher: str
    weekday: str
    period: int
    term: str
    raw_text: str


def normalize_spaces(text: str) -> str:
    return re.sub(r"[ \t\u3000]+", " ", text).strip()


def split_course_blocks(cell_text: str) -> list[str]:
    text = normalize_spaces(cell_text)
    if not text:
        return []
    matches = list(COURSE_CODE_RE.finditer(text))
    if not matches:
        return []

    blocks: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            blocks.append(block)
    return blocks


def parse_block(block: str, weekday: str, period: int, term: str) -> CourseSlot:
    m = COURSE_CODE_RE.match(block)
    if not m:
        raise ValueError(f"Invalid course block: {block}")
    code = m.group(0)
    rest = normalize_spaces(block[m.end() :])
    parts = rest.split(" ") if rest else []
    if len(parts) >= 2:
        teacher = parts[-1]
        course_name = " ".join(parts[:-1]).strip()
    else:
        teacher = ""
        course_name = rest
    return CourseSlot(
        course_code=code,
        course_name=course_name,
        teacher=teacher,
        weekday=weekday,
        period=period,
        term=term,
        raw_text=block,
    )


def term_for_block(block: str, block_index: int, total_blocks: int) -> str:
    text = normalize_spaces(block)
    if YEAR_ROUND_MARK in text:
        return "year"

    def has_any(patterns: list[str]) -> bool:
        return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)

    # Detect second-term first to avoid "II" being accidentally matched as "I".
    second_term_patterns = [
        "\u5f8c\u671f",  # 後期
        "\u79cb\u5b66\u671f",  # 秋学期
        "\u51ac\u5b66\u671f",  # 冬学期
        "\u79cb",  # 秋
        "\u51ac",  # 冬
        r"\b(?:Q3|Q4|3Q|4Q)\b",
        "\u2161",  # Ⅱ
        r"(?<!I)II(?!I)",
    ]
    first_term_patterns = [
        "\u524d\u671f",  # 前期
        "\u6625\u5b66\u671f",  # 春学期
        "\u590f\u5b66\u671f",  # 夏学期
        "\u6625",  # 春
        "\u590f",  # 夏
        r"\b(?:Q1|Q2|1Q|2Q)\b",
        "\u2160",  # Ⅰ
        r"(?<!I)I(?!I)",
    ]

    has_second = has_any(second_term_patterns)
    has_first = has_any(first_term_patterns)
    if has_second and not has_first:
        return "second"
    if has_first and not has_second:
        return "first"

    # Fallback: if a single slot has 2 blocks, assume first/second split by order.
    if total_blocks == 1:
        return "full"
    if block_index == 0:
        return "first"
    if block_index == 1:
        return "second"
    return f"extra_{block_index + 1}"


def read_table03(path: Path) -> list[CourseSlot]:
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            rows.append(row)

    slots: list[CourseSlot] = []
    for row in rows:
        if not row:
            continue
        period_match = PERIOD_LABEL_RE.match(row[0] if len(row) > 0 else "")
        if not period_match:
            continue
        period = int(period_match.group(1))
        if period not in PERIOD_TIMES:
            continue

        for day_idx, weekday in enumerate(WEEKDAYS):
            cell_index = 2 + day_idx * 2
            if cell_index >= len(row):
                continue
            cell_text = row[cell_index]
            blocks = split_course_blocks(cell_text)
            if not blocks:
                continue
            for i, block in enumerate(blocks):
                term = term_for_block(block, i, len(blocks))
                slots.append(parse_block(block, weekday=weekday, period=period, term=term))
    return slots


def parse_date(date_text: str) -> datetime:
    return datetime.strptime(date_text, "%Y-%m-%d")


def hhmm_to_parts(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def next_weekday_on_or_after(start_date: datetime, weekday_index: int) -> datetime:
    return start_date + timedelta(days=(weekday_index - start_date.weekday()) % 7)


def add_weekly_events(
    events: list[dict[str, str]],
    course: CourseSlot,
    date_start: datetime,
    date_end: datetime,
    excluded_dates: set[date] | None = None,
) -> int:
    if course.weekday not in WEEKDAY_INDEX:
        return 0
    weekday_idx = WEEKDAY_INDEX[course.weekday]
    first_day = next_weekday_on_or_after(date_start, weekday_idx)
    start_h, start_m = hhmm_to_parts(PERIOD_TIMES[course.period][0])
    end_h, end_m = hhmm_to_parts(PERIOD_TIMES[course.period][1])

    count = 0
    current = first_day
    while current.date() <= date_end.date():
        if excluded_dates and current.date() in excluded_dates:
            current += timedelta(days=7)
            continue
        dt_start = current.replace(hour=start_h, minute=start_m, second=0, microsecond=0, tzinfo=TZ)
        dt_end = current.replace(hour=end_h, minute=end_m, second=0, microsecond=0, tzinfo=TZ)
        events.append(
            {
                "uid": f"{uuid.uuid4()}@kyushuclass",
                "summary": course.course_name,
                "dtstart": dt_start.strftime("%Y%m%dT%H%M%S"),
                "dtend": dt_end.strftime("%Y%m%dT%H%M%S"),
                "description": f"Code: {course.course_code}\\nTeacher: {course.teacher}\\nTerm: {course.term}",
                "category": "KyushuClass",
            }
        )
        count += 1
        current += timedelta(days=7)
    return count


def collect_japan_holiday_dates(date_ranges: list[tuple[datetime, datetime]]) -> set[date]:
    if not date_ranges:
        return set()

    start_year = min(start.date().year for start, _ in date_ranges)
    end_year = max(end.date().year for _, end in date_ranges)
    jp_holidays = holidays.country_holidays("JP", years=range(start_year, end_year + 1))

    holiday_dates: set[date] = set()
    for start, end in date_ranges:
        current = start.date()
        while current <= end.date():
            if current in jp_holidays:
                holiday_dates.add(current)
            current += timedelta(days=1)
    return holiday_dates


def build_calendar_events(
    courses: list[CourseSlot],
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
    exclude_japan_holidays: bool = True,
) -> tuple[list[dict[str, str]], int]:
    events: list[dict[str, str]] = []
    event_count = 0
    excluded_dates = (
        collect_japan_holiday_dates([(first_start, first_end), (second_start, second_end)])
        if exclude_japan_holidays
        else set()
    )
    for c in courses:
        if c.term in {"first", "full", "year"}:
            event_count += add_weekly_events(events, c, first_start, first_end, excluded_dates=excluded_dates)
        if c.term in {"second", "full", "year"}:
            event_count += add_weekly_events(events, c, second_start, second_end, excluded_dates=excluded_dates)
    return events, event_count


def ics_escape(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;").replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text


def build_ics_text(events: list[dict[str, str]]) -> str:
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//KyushuClass//Table03 to iCloud Calendar//",
        "CALSCALE:GREGORIAN",
        "X-WR-CALNAME:Kyushu Timetable",
    ]
    for ev in events:
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{ev['uid']}",
                f"DTSTAMP:{now_utc}",
                f"SUMMARY:{ics_escape(ev['summary'])}",
                f"DTSTART;TZID=Asia/Tokyo:{ev['dtstart']}",
                f"DTEND;TZID=Asia/Tokyo:{ev['dtend']}",
                f"DESCRIPTION:{ics_escape(ev['description'])}",
                f"CATEGORIES:{ics_escape(ev['category'])}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_json_sample(courses: list[CourseSlot], json_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "notes": {
            "term_rule": "same slot with 2 courses -> first=first semester, second=second semester",
            "weekday_columns": WEEKDAYS,
            "period_times": PERIOD_TIMES,
        },
        "courses": [asdict(c) for c in courses],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Kyushu table_03.csv to JSON + iCloud-importable ICS.")
    parser.add_argument("--input-csv", default="output/tables/table_03.csv")
    parser.add_argument("--output-json", default="output/calendar_events.json")
    parser.add_argument("--output-ics", default="output/kyushu_timetable.ics")
    parser.add_argument("--first-semester-start", default="2026-04-13")
    parser.add_argument("--first-semester-end", default="2026-08-07")
    parser.add_argument("--second-semester-start", default="2026-10-01")
    parser.add_argument("--second-semester-end", default="2027-02-05")
    parser.add_argument(
        "--keep-japan-holidays",
        action="store_true",
        help="Keep classes on Japanese public holidays (default behavior removes them).",
    )
    args = parser.parse_args()

    courses = read_table03(Path(args.input_csv))
    write_json_sample(courses, Path(args.output_json))

    first_start = parse_date(args.first_semester_start)
    first_end = parse_date(args.first_semester_end)
    second_start = parse_date(args.second_semester_start)
    second_end = parse_date(args.second_semester_end)

    if first_end < first_start:
        raise SystemExit("first semester end date must be on or after first semester start date.")
    if second_end < second_start:
        raise SystemExit("second semester end date must be on or after second semester start date.")

    events, event_count = build_calendar_events(
        courses,
        first_start=first_start,
        first_end=first_end,
        second_start=second_start,
        second_end=second_end,
        exclude_japan_holidays=not args.keep_japan_holidays,
    )

    output_ics = Path(args.output_ics)
    output_ics.parent.mkdir(parents=True, exist_ok=True)
    output_ics.write_text(build_ics_text(events), encoding="utf-8")

    print(f"[OK] Parsed slots: {len(courses)}")
    print(f"[OK] JSON sample: {args.output_json}")
    print(f"[OK] ICS events: {event_count}")
    print(f"[OK] ICS file: {args.output_ics}")


if __name__ == "__main__":
    main()
