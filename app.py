from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re, os

app = Flask(__name__)
CORS(app, origins="*")

ERP_BASE = "https://newerp.kluniversity.in"

# Exact URL visible in the screenshot after clicking Search
ATTEND_URL = f"{ERP_BASE}/index.php?r=studentattendance/studentdailyattendance/searchgetinput"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": ERP_BASE,
}


def parse_kl_table(html):
    """
    Parse the KL ERP attendance table.
    Exact columns from ERP: # | Coursecode | Coursedesc | Ltps | Section | Year | Semester | Fr Date | Total Conducted | Total Attended
    Same subject appears multiple times (L/S/P) — we merge them.
    """
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    student_name = ""

    # Get student name
    for sel in [".username", ".user-name", "#profile-name", ".navbar-text b",
                ".navbar-text strong", "[class*='username']"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if 2 < len(t) < 80:
                student_name = t
                break

    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue

        ths = header_row.find_all(["th", "td"])
        # Normalize: lowercase, strip spaces
        headers = [h.get_text(strip=True).lower().replace(" ", "") for h in ths]

        # Must have these two exact columns from KL ERP
        if "coursecode" not in headers or "coursedesc" not in headers:
            continue

        def col(key):
            for i, h in enumerate(headers):
                if key in h:
                    return i
            return -1

        i_code      = col("coursecode")
        i_name      = col("coursedesc")
        i_conducted = col("totalconducted") if col("totalconducted") != -1 else col("conducted")
        i_attended  = col("totalattended")  if col("totalattended")  != -1 else col("attended")

        # Fallback: if still -1, look for last two numeric-looking column headers
        if i_conducted == -1 or i_attended == -1:
            num_cols = [i for i, h in enumerate(headers) if any(k in h for k in ["total", "conduct", "attend", "present"])]
            if len(num_cols) >= 2:
                i_conducted = num_cols[-2]
                i_attended  = num_cols[-1]

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cells) < 5:
                continue

            def g(i):
                return cells[i].strip() if 0 <= i < len(cells) else ""

            def gi(i):
                v = re.sub(r"[^\d]", "", g(i))
                return int(v) if v else None

            code      = g(i_code)
            name      = g(i_name)
            conducted = gi(i_conducted)
            attended  = gi(i_attended)

            if not name or not conducted:
                continue
            if attended is None:
                attended = 0

            # Merge L/S/P rows of the same subject into one
            existing = next((c for c in courses if c["code"] == code and c["name"] == name), None)
            if existing:
                existing["conducted"] += conducted
                existing["attended"]  += attended
                existing["percentage"] = round(existing["attended"] / existing["conducted"] * 100, 1)
            else:
                courses.append({
                    "code":       code,
                    "name":       name,
                    "conducted":  conducted,
                    "attended":   attended,
                    "percentage": round(attended / conducted * 100, 1),
                })

        if courses:
            break

    return student_name, courses


@app.route("/api/fetch-attendance", methods=["POST"])
def fetch_attendance():
    body   = request.get_json(force=True, silent=True) or {}
    cookie = (body.get("cookie") or "").strip()

    if not cookie:
        return jsonify({"ok": False, "error": "No cookie provided."}), 400

    hdrs = {**HEADERS, "Cookie": cookie}

    # Just GET the attendance results page directly —
    # the user already clicked Search on ERP so the session holds the result
    try:
        resp = requests.get(ATTEND_URL, headers=hdrs, timeout=15, allow_redirects=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Network error: {e}"}), 500

    # Redirected to login = cookie expired
    if "LoginForm" in resp.text or "login" in resp.url.lower():
        return jsonify({
            "ok": False,
            "error": "Session expired or invalid. Go back to the ERP, make sure you're logged in and the attendance table is visible, then copy the cookie again."
        }), 401

    student_name, courses = parse_kl_table(resp.text)

    if not courses:
        return jsonify({
            "ok": False,
            "error": "Could not find attendance data. Make sure: (1) you are on the Attendance Register page, (2) you selected the year & semester and clicked Search so the table is visible, THEN copy the cookie."
        })

    return jsonify({"ok": True, "studentName": student_name, "courses": courses})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
