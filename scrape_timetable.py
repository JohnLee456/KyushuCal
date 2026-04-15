from __future__ import annotations

import argparse
import getpass
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from convert_table_to_ics import read_table03, write_json_sample

ENTRY_URL = "https://ku-portal.kyushu-u.ac.jp/campusweb/top.do"
TIMETABLE_URL = "https://ku-portal.kyushu-u.ac.jp/campusweb/prtlmjkr.do?clearAccessData=true&kjnmnNo=112"
SSO_JA_URL = "https://ku-portal.kyushu-u.ac.jp/eduapi/gknsso/Campusmate_ja"
OUTPUT_DIR = Path("output")
CREDENTIALS_PATH = OUTPUT_DIR / "credentials.local.json"
TIMEOUT = 25


def set_best_encoding(resp: requests.Response) -> None:
    resp.encoding = resp.apparent_encoding or resp.encoding


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_saved_credentials(path: Path) -> tuple[str, str]:
    if not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    user_id = str(data.get("user_id", "")).strip()
    password = str(data.get("password", ""))
    return user_id, password


def save_credentials(path: Path, user_id: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"user_id": user_id, "password": password}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_response_dump(resp: requests.Response, output_dir: Path, tag: str) -> None:
    debug_dir = output_dir / "debug"
    set_best_encoding(resp)
    write_text(debug_dir / f"{tag}.html", resp.text)


def save_redirect_history(resp: requests.Response, output_dir: Path, tag: str) -> None:
    debug_dir = output_dir / "debug"
    lines = [f"[{tag}] final_url={resp.url} status={resp.status_code}"]
    if resp.history:
        for i, h in enumerate(resp.history, start=1):
            lines.append(f"  hop{i}: {h.status_code} {h.url}")
    else:
        lines.append("  no redirects")
    write_text(debug_dir / f"{tag}_redirects.txt", "\n".join(lines) + "\n")


def detect_login_form(soup: BeautifulSoup):
    for form in soup.find_all("form"):
        if form.select_one("input[type='password']"):
            return form
    return soup.find("form")


def build_form_payload(form) -> dict[str, str]:
    payload: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        payload[name] = inp.get("value", "")
    return payload


def submit_form(
    sess: requests.Session,
    base_url: str,
    form,
    extra_fields: dict[str, str] | None = None,
) -> requests.Response:
    payload = build_form_payload(form)
    if extra_fields:
        payload.update(extra_fields)

    action = form.get("action") or base_url
    method = (form.get("method") or "post").lower()
    url = urljoin(base_url, action)

    if method == "get":
        resp = sess.get(url, params=payload, timeout=TIMEOUT, allow_redirects=True)
    else:
        resp = sess.post(url, data=payload, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    return resp


def set_credentials(payload: dict[str, str], user_id: str, password: str) -> tuple[bool, bool]:
    user_candidates = ["userId", "userid", "user", "id", "loginId", "j_username"]
    pass_candidates = ["password", "passwd", "pass", "j_password"]

    user_set = False
    pass_set = False

    for key in user_candidates:
        if key in payload:
            payload[key] = user_id
            user_set = True
            break

    for key in pass_candidates:
        if key in payload:
            payload[key] = password
            pass_set = True
            break

    if not user_set:
        for key in list(payload):
            low = key.lower()
            if "user" in low or low in {"id", "uid"}:
                payload[key] = user_id
                user_set = True
                break

    if not pass_set:
        for key in list(payload):
            low = key.lower()
            if "pass" in low or "pwd" in low:
                payload[key] = password
                pass_set = True
                break

    return user_set, pass_set


def follow_meta_refresh(
    sess: requests.Session,
    current_resp: requests.Response,
    output_dir: Path,
    max_hops: int = 3,
) -> requests.Response:
    resp = current_resp
    for i in range(1, max_hops + 1):
        set_best_encoding(resp)
        soup = BeautifulSoup(resp.text, "lxml")
        meta = soup.find("meta", attrs={"http-equiv": re.compile(r"refresh", re.I)})
        if not meta:
            break
        content = meta.get("content", "")
        m = re.search(r"url\s*=\s*(.+)", content, flags=re.I)
        if not m:
            break
        next_url = urljoin(resp.url, m.group(1).strip(" '\""))
        resp = sess.get(next_url, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        save_response_dump(resp, output_dir, f"step_meta_refresh_{i}")
        save_redirect_history(resp, output_dir, f"step_meta_refresh_{i}")
    return resp


def follow_first_iframe(
    sess: requests.Session,
    current_resp: requests.Response,
    output_dir: Path,
) -> requests.Response:
    set_best_encoding(current_resp)
    soup = BeautifulSoup(current_resp.text, "lxml")
    iframe = soup.find("iframe")
    if not iframe:
        return current_resp

    src = iframe.get("src")
    if not src:
        return current_resp

    iframe_url = urljoin(current_resp.url, src)
    iframe_resp = sess.get(iframe_url, timeout=TIMEOUT, allow_redirects=True)
    iframe_resp.raise_for_status()
    save_response_dump(iframe_resp, output_dir, "step_iframe")
    save_redirect_history(iframe_resp, output_dir, "step_iframe")
    return iframe_resp


def resolve_login_surface(
    sess: requests.Session,
    first_resp: requests.Response,
    output_dir: Path,
) -> requests.Response:
    resp = follow_meta_refresh(sess, first_resp, output_dir=output_dir)
    set_best_encoding(resp)
    soup = BeautifulSoup(resp.text, "lxml")
    if detect_login_form(soup):
        return resp

    iframe_resp = follow_first_iframe(sess, resp, output_dir=output_dir)
    set_best_encoding(iframe_resp)
    iframe_soup = BeautifulSoup(iframe_resp.text, "lxml")
    if detect_login_form(iframe_soup):
        return iframe_resp

    return resp


def find_password_form(soup: BeautifulSoup):
    for form in soup.find_all("form"):
        if form.select_one("input[type='password']"):
            return form
    return None


def advance_to_password_page(
    sess: requests.Session,
    start_resp: requests.Response,
    output_dir: Path,
    max_steps: int = 6,
) -> requests.Response:
    resp = start_resp
    for i in range(1, max_steps + 1):
        set_best_encoding(resp)
        soup = BeautifulSoup(resp.text, "lxml")
        if find_password_form(soup):
            return resp

        form = detect_login_form(soup)
        if form is None:
            break

        resp = submit_form(sess, base_url=resp.url, form=form)
        save_response_dump(resp, output_dir, f"step_sso_advance_{i}")
        save_redirect_history(resp, output_dir, f"step_sso_advance_{i}")

    return resp


def follow_hidden_autopost_chain(
    sess: requests.Session,
    start_resp: requests.Response,
    output_dir: Path,
    max_steps: int = 6,
) -> requests.Response:
    resp = start_resp
    for i in range(1, max_steps + 1):
        set_best_encoding(resp)
        # Stop once we are back in Campusmate top page after SSO callback.
        if "ku-portal.kyushu-u.ac.jp/campusweb/top.do" in resp.url:
            break
        soup = BeautifulSoup(resp.text, "lxml")
        if find_password_form(soup):
            break

        form = soup.find("form")
        if form is None:
            break

        form_name = (form.get("name") or "").lower()
        form_action = (form.get("action") or "").lower()
        # Do not submit Campusmate's top-page login form again, it can trigger logout loops.
        if form_name == "loginform" or "login.do" in form_action:
            break

        inputs = form.find_all("input")
        if not inputs:
            break

        has_password = any((inp.get("type") or "").lower() == "password" for inp in inputs)
        if has_password:
            break

        non_trivial = [
            inp for inp in inputs if (inp.get("type") or "").lower() not in {"hidden", "submit", "button"}
        ]
        has_saml = any((inp.get("name") or "").lower() in {"samlresponse", "relaystate"} for inp in inputs)
        if non_trivial and not has_saml:
            break

        resp = submit_form(sess, base_url=resp.url, form=form)
        save_response_dump(resp, output_dir, f"step_hidden_autopost_{i}")
        save_redirect_history(resp, output_dir, f"step_hidden_autopost_{i}")

    return resp


def parse_tables_and_save(html: str, output_dir: Path) -> int:
    def normalize_cell_text(text: str) -> str:
        return re.sub(r"[ \t\u3000]+", " ", text).strip()

    def build_split_term_cell_map(page_html: str) -> dict[tuple[int, int], tuple[str, str]]:
        soup = BeautifulSoup(page_html, "lxml")
        mapping: dict[tuple[int, int], tuple[str, str]] = {}
        timetable = soup.find("table", class_="jikanwari_table")
        if timetable is None:
            return mapping

        tbodies = timetable.find_all("tbody", recursive=False)
        body = max(tbodies, key=lambda b: len(b.find_all("tr", recursive=False))) if tbodies else timetable

        for tr in body.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 3:
                continue
            period_text = normalize_cell_text(tds[0].get_text(" ", strip=True))
            m = re.match(r"^\s*(\d+)\s*$", period_text)
            if not m:
                continue
            period = int(m.group(1))

            # day=1..6 (Mon..Sat), columns are 2,4,6,8,10,12.
            for day in range(1, 7):
                cell_index = day * 2
                if cell_index >= len(tds):
                    continue
                cell = tds[cell_index]
                inner = cell.find(
                    "table",
                    attrs={"style": lambda v: v and "margin: 0px" in v and "border: medium none" in v},
                )
                if inner is None:
                    continue
                row = inner.find("tr")
                if row is None:
                    continue
                inner_tds = row.find_all("td", recursive=False)
                if not inner_tds:
                    continue

                left_text = normalize_cell_text(inner_tds[0].get_text(" ", strip=True)) if len(inner_tds) >= 1 else ""
                right_text = normalize_cell_text(inner_tds[2].get_text(" ", strip=True)) if len(inner_tds) >= 3 else ""
                mapping[(period, day)] = (left_text, right_text)
        return mapping

    def rewrite_timetable_with_term_split(df: pd.DataFrame, split_map: dict[tuple[int, int], tuple[str, str]]) -> pd.DataFrame:
        out = df.copy()
        for row_idx in range(len(out)):
            row0 = str(out.iat[row_idx, 0]) if out.shape[1] > 0 else ""
            m = re.match(r"^\s*(\d+)(?:\D+)?\s*$", row0)
            if not m:
                continue
            period = int(m.group(1))

            for day in range(1, 8):
                cell_col = 2 + (day - 1) * 2
                if cell_col >= out.shape[1]:
                    continue
                if (period, day) not in split_map:
                    continue

                left_text, right_text = split_map[(period, day)]
                if left_text and right_text:
                    new_text = f"{left_text}  {right_text}"
                elif left_text:
                    new_text = f"{left_text} I"
                elif right_text:
                    new_text = f"{right_text} II"
                else:
                    new_text = ""

                out.iat[row_idx, cell_col] = new_text
        return out

    tables_dir = output_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    split_map = build_split_term_cell_map(html)

    try:
        tables = pd.read_html(html)
    except ValueError:
        tables = []

    for i, table in enumerate(tables, start=1):
        if i == 2 and split_map:
            table = rewrite_timetable_with_term_split(table, split_map)
        table.to_csv(tables_dir / f"table_{i:02d}.csv", index=False, encoding="utf-8-sig")

    # Debug helper: generate parsed course slots right after table export.
    timetable_csv = tables_dir / "table_02.csv"
    if not timetable_csv.exists():
        timetable_csv = tables_dir / "table_03.csv"
    if timetable_csv.exists():
        try:
            courses = read_table03(timetable_csv)
            write_json_sample(courses, output_dir / "calendar_events.json")
        except Exception as exc:
            print(f"[WARN] Failed to write debug calendar_events.json: {exc}")

    return len(tables)


def login_and_fetch_timetable(user_id: str, password: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = output_dir / "debug"
    html_path = output_dir / "timetable_raw.html"

    with requests.Session() as sess:
        first = sess.get(ENTRY_URL, timeout=TIMEOUT, allow_redirects=True)
        first.raise_for_status()
        save_response_dump(first, output_dir, "step_first")
        save_redirect_history(first, output_dir, "step_first")

        login_surface = resolve_login_surface(sess, first, output_dir=output_dir)
        save_response_dump(login_surface, output_dir, "step_login_surface")
        save_redirect_history(login_surface, output_dir, "step_login_surface")

        # Campusmate top page uses JS transition. Enter SSO directly.
        sso_resp = sess.get(SSO_JA_URL, timeout=TIMEOUT, allow_redirects=True)
        sso_resp.raise_for_status()
        save_response_dump(sso_resp, output_dir, "step_sso_entry")
        save_redirect_history(sso_resp, output_dir, "step_sso_entry")

        password_page = advance_to_password_page(sess, sso_resp, output_dir=output_dir)
        set_best_encoding(password_page)
        pw_soup = BeautifulSoup(password_page.text, "lxml")
        pw_form = find_password_form(pw_soup)
        if pw_form is None:
            raise RuntimeError(
                "Could not reach password form in SSO flow. "
                f"Check files under {debug_dir} (step_sso_entry*.html / step_sso_advance_*.html)."
            )

        payload = build_form_payload(pw_form)
        user_set, pass_set = set_credentials(payload, user_id=user_id, password=password)
        if not (user_set and pass_set):
            raise RuntimeError(
                "Could not detect username/password fields on SSO password form. "
                f"Check {debug_dir / 'step_sso_entry.html'}"
            )

        login_resp = submit_form(sess, base_url=password_page.url, form=pw_form, extra_fields=payload)
        save_response_dump(login_resp, output_dir, "step_after_login_submit")
        save_redirect_history(login_resp, output_dir, "step_after_login_submit")

        # SAML / hidden relay forms may still need auto-posting.
        post_chain_resp = follow_hidden_autopost_chain(sess, login_resp, output_dir=output_dir)
        save_response_dump(post_chain_resp, output_dir, "step_after_hidden_chain")
        save_redirect_history(post_chain_resp, output_dir, "step_after_hidden_chain")

        timetable_resp = sess.get(TIMETABLE_URL, timeout=TIMEOUT, allow_redirects=True)
        timetable_resp.raise_for_status()
        set_best_encoding(timetable_resp)
        html = timetable_resp.text
        html_path.write_text(html, encoding="utf-8")
        save_redirect_history(timetable_resp, output_dir, "step_final_timetable")

    table_count = parse_tables_and_save(html, output_dir=output_dir)

    print(f"[OK] Saved page HTML: {html_path}")
    print(f"[OK] Extracted tables: {table_count}")
    print(f"[OK] CSV directory: {output_dir / 'tables'}")
    if (output_dir / "calendar_events.json").exists():
        print(f"[OK] Debug JSON: {output_dir / 'calendar_events.json'}")
    print(f"[OK] Debug directory: {debug_dir}")

    lower_html = html.lower()
    if table_count == 0 or "login" in lower_html or "password" in lower_html:
        print("[WARN] Possible login failure or timetable is not in <table> tags.")
        print(f"[WARN] Inspect debug files in: {debug_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Login to Kyushu University portal and fetch timetable HTML/CSV without opening a browser."
    )
    parser.add_argument("--user-id", help="Student ID / login ID")
    parser.add_argument("--password", help="Password (avoid plain text; omit to input securely)")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="Output directory (default: output)")
    parser.add_argument(
        "--credentials-file",
        default=str(CREDENTIALS_PATH),
        help="Local credentials file path (default: output/credentials.local.json)",
    )
    parser.add_argument(
        "--save-credentials",
        action="store_true",
        help="Save the provided/entered credentials to the local credentials file.",
    )
    args = parser.parse_args()

    credentials_file = Path(args.credentials_file)
    saved_user_id, saved_password = load_saved_credentials(credentials_file)

    user_id = (args.user_id or "").strip() or saved_user_id
    password = args.password or saved_password

    if not user_id:
        user_id = input("SSO-KID: ").strip()
    if not password:
        password = getpass.getpass("SSO Password: ")

    if not user_id or not password:
        raise SystemExit("ID/password is required.")

    if args.save_credentials:
        save_credentials(credentials_file, user_id=user_id, password=password)
        print(f"[OK] Credentials saved to: {credentials_file}")

    login_and_fetch_timetable(user_id=user_id, password=password, output_dir=Path(args.output_dir))


if __name__ == "__main__":
    main()
