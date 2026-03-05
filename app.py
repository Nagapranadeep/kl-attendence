from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re, os

app = Flask(__name__)
CORS(app, origins="*")

ERP_BASE = "https://newerp.kluniversity.in"
ATTENDANCE_URL = f"{ERP_BASE}/index.php?r=student/student-attendanceregister"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": ERP_BASE,
}


def parse_attendance(html):
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    student_name = ""

    # Try common selectors for student name
    for sel in [".user-name", "#profile-name", ".username", "[class*='student-name']", "span.name", ".navbar-text"]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t and len(t) > 2:
                student_name = t
                break

    # Also try finding name in any element with text that looks like a name
    if not student_name:
        for tag in soup.find_all(["span", "div", "td", "p"]):
            t = tag.get_text(strip=True)
            if re.match(r'^[A-Z][a-z]+ [A-Z]', t) and len(t) < 60:
                student_name = t
                break

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        joined  = " ".join(headers)

        has_subject = any(k in joined for k in ["course", "subject", "name", "code"])
        has_numbers = any(k in joined for k in ["conduct", "attend", "held", "present", "percent"])
        if not has_subject and not has_numbers:
            continue

        col = {"code": -1, "name": -1, "conducted": -1, "attended": -1}
        for i, h in enumerate(headers):
            if "code" in h and col["code"] == -1:                                             col["code"] = i
            if any(k in h for k in ["course name","subject name","name","description","title"]) and col["name"] == -1: col["name"] = i
            if any(k in h for k in ["conduct","held","total class"]):                          col["conducted"] = i
            if any(k in h for k in ["attend","present"]) and col["attended"] == -1:           col["attended"] = i

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td","th"])]
            if len(cells) < 3:
                continue

            def sint(idx):
                if idx == -1 or idx >= len(cells): return None
                v = re.sub(r"[^\d]", "", cells[idx])
                return int(v) if v else None

            def sstr(idx):
                if idx == -1 or idx >= len(cells): return ""
                return cells[idx].strip()

            code      = sstr(col["code"])
            name      = sstr(col["name"])
            conducted = sint(col["conducted"])
            attended  = sint(col["attended"])

            if not conducted:
                nums = [int(re.sub(r"[^\d]","",c)) for c in cells if re.sub(r"[^\d]","",c) and 0 < int(re.sub(r"[^\d]","",c)) < 1000]
                if len(nums) >= 2:
                    conducted, attended = nums[0], nums[1]

            if not conducted or conducted == 0: continue
            if attended is None: attended = 0
            if not name and not code:
                for c in cells:
                    if c and not re.match(r"^[\d\s.%()]+$", c) and len(c) > 2:
                        name = c; break
            if not name and not code: continue

            pct = round((attended / conducted) * 100, 1)
            courses.append({
                "code": code or "—",
                "name": name or code,
                "conducted": conducted,
                "attended": attended,
                "percentage": pct,
            })

        if courses:
            break

    return student_name, courses


@app.route("/api/fetch-attendance", methods=["POST"])
def fetch_attendance():
    """
    Fetch attendance using the user's browser session cookie.
    Body: { "cookie": "PHPSESSID=abc123; _csrf=xyz..." }
    """
    body   = request.get_json(force=True, silent=True) or {}
    cookie = (body.get("cookie") or "").strip()

    if not cookie:
        return jsonify({"ok": False, "error": "No cookie provided."}), 400

    headers = {**HEADERS, "Cookie": cookie}

    try:
        resp = requests.get(ATTENDANCE_URL, headers=headers, timeout=15, allow_redirects=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Request failed: {str(e)}"}), 500

    # If redirected back to login, cookie is invalid/expired
    if "login" in resp.url.lower() or "LoginForm" in resp.text:
        return jsonify({"ok": False, "error": "Session expired or invalid. Please copy your cookie again from the ERP."}), 401

    soup = BeautifulSoup(resp.text, "html.parser")

    # Try to auto-submit search form to get attendance data
    form = soup.find("form", method=re.compile("post", re.I))
    if form and not any("attendance" in str(td).lower() for td in soup.find_all("td")):
        csrf_inp = form.find("input", {"name": "_csrf"}) or soup.find("input", {"name": "_csrf"})
        data = {}
        if csrf_inp: data["_csrf"] = csrf_inp.get("value","")
        for inp in form.find_all("input"):
            n, v, t = inp.get("name",""), inp.get("value",""), inp.get("type","text").lower()
            if n and t not in ("submit","button","reset"): data[n] = v
        for sel in form.find_all("select"):
            n = sel.get("name","")
            if not n: continue
            opts = [o for o in sel.find_all("option") if o.get("value","").strip() not in ("","0")]
            if opts: data[n] = opts[0]["value"]
        action = form.get("action","") or ATTENDANCE_URL
        if not action.startswith("http"): action = ERP_BASE + "/" + action.lstrip("/")
        resp2 = requests.post(action, data=data, headers={**headers, "Referer": ATTENDANCE_URL}, timeout=15)
        html  = resp2.text
    else:
        html = resp.text

    student_name, courses = parse_attendance(html)

    if not courses:
        snippet = BeautifulSoup(html, "html.parser").get_text()[:600]
        return jsonify({
            "ok": False,
            "error": "Logged in but couldn't find the attendance table. Make sure you're on the Attendance Register page on ERP and have searched for a semester.",
            "debug": snippet
        })

    return jsonify({"ok": True, "studentName": student_name, "courses": courses})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
