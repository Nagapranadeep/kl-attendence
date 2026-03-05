from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re, os

app = Flask(__name__)
CORS(app, origins="*")

ERP_BASE = "https://newerp.kluniversity.in"
LOGIN_URL = f"{ERP_BASE}/index.php?r=site/login"
ATTENDANCE_URL = f"{ERP_BASE}/index.php?r=student/student-attendanceregister"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_csrf(session):
    resp = session.get(LOGIN_URL, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", {"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    inp = soup.find("input", {"name": "_csrf"})
    if inp and inp.get("value"):
        return inp["value"]
    for inp in soup.find_all("input", {"type": "hidden"}):
        val = inp.get("value", "")
        if len(val) > 20 and re.match(r"^[A-Za-z0-9_\-]+$", val):
            return val
    return None


def do_login(session, username, password):
    csrf = fetch_csrf(session)
    if not csrf:
        return False, "Could not fetch CSRF token from ERP."
    payload = {
        "_csrf": csrf,
        "LoginForm[username]": username,
        "LoginForm[password]": password,
        "LoginForm[rememberMe]": "0",
    }
    resp = session.post(LOGIN_URL, data=payload,
                        headers={**HEADERS, "Referer": LOGIN_URL},
                        timeout=15, allow_redirects=True)
    soup = BeautifulSoup(resp.text, "html.parser")
    login_form = soup.find("input", {"name": "LoginForm[username]"})
    if login_form:
        err_div = soup.find(class_=re.compile(r"error|alert-danger", re.I))
        err_text = err_div.get_text(strip=True) if err_div else ""
        return False, err_text or "Invalid username or password."
    return True, ""


def parse_attendance(html):
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    student_name = ""

    for sel in [".user-name", "#profile-name", ".username", "[class*='student']"]:
        el = soup.select_one(sel)
        if el:
            student_name = el.get_text(strip=True)
            break

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_row = rows[0]
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        joined = " ".join(headers)
        has_subject = any(k in joined for k in ["course", "subject", "name", "code"])
        has_numbers = any(k in joined for k in ["conduct", "attend", "held", "present", "percent"])
        if not has_subject and not has_numbers:
            continue

        col = {"code": -1, "name": -1, "conducted": -1, "attended": -1}
        for i, h in enumerate(headers):
            if "code" in h and col["code"] == -1: col["code"] = i
            if any(k in h for k in ["course name", "subject name", "name", "description", "title"]) and col["name"] == -1: col["name"] = i
            if any(k in h for k in ["conduct", "held", "total class"]): col["conducted"] = i
            if any(k in h for k in ["attend", "present"]) and col["attended"] == -1: col["attended"] = i

        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            def safe_int(idx):
                if idx == -1 or idx >= len(cells): return None
                val = re.sub(r"[^\d]", "", cells[idx])
                return int(val) if val else None

            def safe_str(idx):
                if idx == -1 or idx >= len(cells): return ""
                return cells[idx].strip()

            code = safe_str(col["code"])
            name = safe_str(col["name"])
            conducted = safe_int(col["conducted"])
            attended = safe_int(col["attended"])

            if not conducted:
                nums = [int(re.sub(r"[^\d]", "", c)) for c in cells
                        if re.sub(r"[^\d]", "", c) and 0 < int(re.sub(r"[^\d]", "", c)) < 1000]
                if len(nums) >= 2:
                    conducted, attended = nums[0], nums[1]

            if not conducted or conducted == 0: continue
            if attended is None: attended = 0

            if not name and not code:
                for c in cells:
                    if c and not re.match(r"^[\d\s.%()]+$", c) and len(c) > 2:
                        name = c; break

            if not name and not code: continue

            pct = round((attended / conducted) * 100, 1) if conducted > 0 else 0.0
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


def fetch_attendance(session):
    resp = session.get(ATTENDANCE_URL, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", method=re.compile("post", re.I))
    if form:
        csrf_inp = form.find("input", {"name": "_csrf"}) or soup.find("input", {"name": "_csrf"})
        data = {}
        if csrf_inp: data["_csrf"] = csrf_inp.get("value", "")
        for inp in form.find_all("input"):
            n, v, t = inp.get("name", ""), inp.get("value", ""), inp.get("type", "text").lower()
            if n and t not in ("submit", "button", "reset"): data[n] = v
        for sel in form.find_all("select"):
            n = sel.get("name", "")
            if not n: continue
            opts = [o for o in sel.find_all("option") if o.get("value", "").strip() and o.get("value") not in ("", "0")]
            if opts: data[n] = opts[0]["value"]
        action = form.get("action", "") or ATTENDANCE_URL
        if not action.startswith("http"): action = ERP_BASE + "/" + action.lstrip("/")
        resp2 = session.post(action, data=data,
                             headers={**HEADERS, "Referer": ATTENDANCE_URL}, timeout=15)
        return resp2.text
    return resp.text


@app.route("/api/login-attendance", methods=["POST"])
def login_and_fetch():
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Username and password required."}), 400

    session = get_session()
    ok, err = do_login(session, username, password)
    if not ok:
        return jsonify({"ok": False, "error": err or "Login failed."}), 401

    try:
        html = fetch_attendance(session)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to load attendance page: {str(e)}"}), 500

    student_name, courses = parse_attendance(html)
    if not courses:
        soup = BeautifulSoup(html, "html.parser")
        snippet = soup.get_text()[:800]
        return jsonify({
            "ok": False,
            "error": "Logged in but no attendance table found. Try selecting a semester on the ERP first.",
            "debug": snippet
        })

    return jsonify({"ok": True, "studentName": student_name, "courses": courses})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
