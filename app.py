from flask import Flask, request, jsonify, session, render_template
import os, json, requests, re
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash, check_password_hash

import psycopg2
from urllib.parse import urlparse

app = Flask(__name__)

# ===== Session 安全配置 =====
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key_change_me")
app.permanent_session_lifetime = timedelta(days=7)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True
)

# ===== 时区（关键：用中国市场日期）=====
CN_TZ = ZoneInfo("Asia/Shanghai")

def now_cn():
    return datetime.now(CN_TZ)

def today_cn():
    return now_cn().date()

# ===== PostgreSQL 连接 =====
# ✅ 优先用环境变量，没有则用你原来的连接串
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://fund_manager_j5ml_user:Ph7l3aNSGQZEXAUtN6sakueJdrSJMKG9@dpg-d67kfi95pdvs73egn6mg-a.oregon-postgres.render.com/fund_manager_j5ml"
)

def get_conn():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL 未设置（建议用环境变量）")

    url = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        dbname=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port,
        sslmode="require"
    )
    return conn


# ===== 初始化数据库 =====
def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            buy_price REAL NOT NULL,
            amount REAL NOT NULL,
            UNIQUE(user_id, code),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()

init_db()


# ===== 用户管理 =====
@app.route("/register", methods=["POST"])
def register():
    data = request.json or {}
    username = (data.get("username","") or "").strip()
    password = (data.get("password","") or "").strip()
    if not username or not password:
        return jsonify({"error":"用户名和密码不能为空"}),400

    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO users (username,password) VALUES (%s,%s)",
            (username, generate_password_hash(password))
        )
        conn.commit()
        conn.close()
        return jsonify({"status":"ok"})
    except Exception:
        conn.rollback()
        conn.close()
        return jsonify({"error":"用户名已存在"}),400


@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = (data.get("username","") or "").strip()
    password = (data.get("password","") or "").strip()

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id,password FROM users WHERE username=%s", (username,))
    row = c.fetchone()
    conn.close()

    if not row or not check_password_hash(row[1], password):
        return jsonify({"error":"用户名或密码错误"}),400

    session.permanent = True
    session["user_id"] = row[0]
    session["username"] = username

    return jsonify({"status":"ok","username":username})


@app.route("/logout")
def logout():
    session.clear()
    return jsonify({"status":"ok"})


@app.route("/me")
def me():
    if "user_id" not in session:
        return jsonify({"logged_in": False})
    return jsonify({
        "logged_in": True,
        "username": session.get("username")
    })


def current_user():
    user_id = session.get("user_id")
    username = session.get("username")
    if user_id:
        return user_id, username
    return None, None


# ===== 基金接口 =====
def fetch_realtime(code):
    try:
        url = f"http://fundgz.1234567.com.cn/js/{code}.js"
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=5)
        text = r.text.replace("jsonpgz(", "").replace(");", "")
        return json.loads(text)
    except:
        return None


# ===== 缓存 =====
history_cache = {}
history_cache_time = {}
CACHE_EXPIRE = 1800  # 30分钟

def fetch_history(code):
    now = now_cn().timestamp()

    if code in history_cache and (now - history_cache_time.get(code, 0) < CACHE_EXPIRE):
        return history_cache[code]

    try:
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=8)
        match = re.search(r"Data_netWorthTrend\s*=\s*(.*?);", r.text)
        if not match:
            return []

        data_json = json.loads(match.group(1))

        # epoch(ms) -> UTC -> 上海时区日期，避免跨日错判
        data = []
        for d in data_json:
            dt = datetime.fromtimestamp(d["x"]/1000, tz=timezone.utc).astimezone(CN_TZ)
            data.append({
                "date": dt.strftime("%Y-%m-%d"),
                "value": d["y"]
            })

        history_cache[code] = data
        history_cache_time[code] = now
        return data
    except:
        return []


# ===== 今日状态/涨跌计算（增强：输出预估/实际分开） =====
def _parse_ymd(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except:
        return None

def _realtime_gz_date(rt: dict):
    gztime = rt.get("gztime")
    if not gztime:
        return None
    try:
        return datetime.strptime(gztime[:10], "%Y-%m-%d").date()
    except:
        return None

def _history_latest_and_prev(history):
    if not history:
        return None, None, None, None
    latest = history[-1]
    latest_date = _parse_ymd(latest.get("date",""))
    latest_value = latest.get("value")
    prev_date = None
    prev_value = None
    if len(history) >= 2:
        prev = history[-2]
        prev_date = _parse_ymd(prev.get("date",""))
        prev_value = prev.get("value")
    return latest_date, latest_value, prev_date, prev_value

def compute_today_metrics(realtime: dict, history: list):
    """
    返回（全部是“每份”的值，外层 holdings 会乘份额）：
    - market_status: "real" | "open_estimate" | "closed" | "unknown"
    - today_estimate_profit_per_share
    - today_real_profit_per_share
    - today_estimate_percent
    - today_real_percent
    - display_text
    - today_profit_per_share: 兼容字段（优先真实，其次预估，否则 0）
    """
    today = today_cn()

    latest_date, latest_value, prev_date, prev_value = _history_latest_and_prev(history)
    gz_date = _realtime_gz_date(realtime)

    if not latest_date or latest_value is None:
        return {
            "market_status": "unknown",
            "today_estimate_profit_per_share": None,
            "today_real_profit_per_share": None,
            "today_estimate_percent": None,
            "today_real_percent": None,
            "today_profit_per_share": 0.0,
            "display_text": "数据不足"
        }

    # 1) 实际净值已公布（历史最新就是今天）
    if latest_date == today and prev_value is not None:
        prev_v = float(prev_value)
        latest_v = float(latest_value)
        real_profit = (latest_v - prev_v)
        real_percent = (latest_v - prev_v) / prev_v * 100 if prev_v != 0 else 0.0

        return {
            "market_status": "real",
            "today_estimate_profit_per_share": None,
            "today_real_profit_per_share": real_profit,
            "today_estimate_percent": None,
            "today_real_percent": real_percent,
            "today_profit_per_share": real_profit,
            "display_text": "已更新(真实)"
        }

    # 2) 盘中估算（用 gztime 判断）
    if gz_date == today:
        yesterday_v = float(latest_value)

        try:
            gsz = float(realtime.get("gsz"))
        except:
            gsz = None

        if gsz is None or yesterday_v == 0:
            return {
                "market_status": "open_estimate",
                "today_estimate_profit_per_share": None,
                "today_real_profit_per_share": None,
                "today_estimate_percent": None,
                "today_real_percent": None,
                "today_profit_per_share": 0.0,
                "display_text": "估算中"
            }

        est_profit = (gsz - yesterday_v)
        est_percent = (gsz - yesterday_v) / yesterday_v * 100

        return {
            "market_status": "open_estimate",
            "today_estimate_profit_per_share": est_profit,
            "today_real_profit_per_share": None,
            "today_estimate_percent": est_percent,
            "today_real_percent": None,
            "today_profit_per_share": est_profit,
            "display_text": "估算中"
        }

    # 3) 未开盘/停市
    return {
        "market_status": "closed",
        "today_estimate_profit_per_share": None,
        "today_real_profit_per_share": None,
        "today_estimate_percent": None,
        "today_real_percent": None,
        "today_profit_per_share": 0.0,
        "display_text": "未开盘"
    }


# ===== 持仓操作 =====
@app.route("/add", methods=["POST"])
def add():
    user_id,_ = current_user()
    if not user_id:
        return jsonify({"error":"请先登录"}),401

    data = request.json or {}
    code = (data.get("code","") or "").strip()
    buy_price = data.get("buy_price")
    amount = data.get("amount")

    if not code:
        return jsonify({"error":"基金代码不能为空"}),400
    try:
        buy_price = float(buy_price)
        amount = float(amount)
        if buy_price <= 0 or amount <= 0:
            return jsonify({"error":"买入价格和份额必须大于0"}),400
    except:
        return jsonify({"error":"买入价格和份额必须为数字"}),400

    realtime = fetch_realtime(code)
    if not realtime:
        return jsonify({"error":"基金代码无效或不存在"}),400

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM holdings WHERE user_id=%s AND code=%s", (user_id, code))
    if c.fetchone():
        conn.close()
        return jsonify({"error":"该基金已存在，请直接操作仓位"}),400

    c.execute(
        "INSERT INTO holdings (user_id, code, buy_price, amount) VALUES (%s,%s,%s,%s)",
        (user_id, code, buy_price, amount)
    )
    conn.commit()
    conn.close()
    return jsonify({"status":"ok","name":realtime.get("name")})


@app.route("/update/<code>", methods=["POST"])
def update_position(code):
    user_id,_ = current_user()
    if not user_id:
        return jsonify({"error":"请先登录"}),401

    data = request.json or {}
    delta = float(data.get("delta", 0) or 0)
    add_price = float(data.get("buy_price", 0) or 0)

    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT amount, buy_price FROM holdings WHERE user_id=%s AND code=%s", (user_id, code))
    row = c.fetchone()

    if not row:
        conn.close()
        return jsonify({"error":"基金不存在"}),400

    old_amount, old_price = row
    new_amount = old_amount + delta

    if new_amount <= 0:
        c.execute("DELETE FROM holdings WHERE user_id=%s AND code=%s", (user_id, code))
        new_amount = 0
        new_price = old_price
    else:
        if delta > 0:
            avg_price = (old_price*old_amount + add_price*delta)/new_amount
        else:
            avg_price = old_price

        c.execute(
            "UPDATE holdings SET amount=%s, buy_price=%s WHERE user_id=%s AND code=%s",
            (new_amount, avg_price, user_id, code)
        )
        new_price = avg_price

    conn.commit()
    conn.close()

    return jsonify({"status":"ok","new_amount":new_amount,"buy_price":new_price})


@app.route("/delete/<code>", methods=["DELETE"])
def delete(code):
    user_id,_ = current_user()
    if not user_id:
        return jsonify({"error":"请先登录"}),401

    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM holdings WHERE user_id=%s AND code=%s", (user_id, code))
    conn.commit()
    conn.close()

    return jsonify({"status":"deleted"})


@app.route("/holdings")
def holdings():
    user_id,_ = current_user()
    if not user_id:
        return jsonify({"error":"请先登录"}),401

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT code,buy_price,amount FROM holdings WHERE user_id=%s", (user_id,))
    rows = c.fetchall()
    conn.close()

    funds = []
    total_asset = 0.0
    total_cost = 0.0
    total_today_profit = 0.0

    for code, buy_price, amount in rows:
        realtime = fetch_realtime(code)
        if not realtime:
            continue

        history = fetch_history(code)

        # 当前估算净值
        try:
            current_est = float(realtime.get("gsz"))
        except:
            current_est = None

        # 最新已公布净值（fundgz 可能滞后，不一定可靠）
        try:
            dwjz = float(realtime.get("dwjz")) if realtime.get("dwjz") else None
        except:
            dwjz = None

        info = compute_today_metrics(realtime, history)
        market_status = info["market_status"]

        latest_date, latest_value, _, _ = _history_latest_and_prev(history)

        # ===== ✅ 净值日：跟随 current 的来源 =====
        # 盘中估算：显示 gztime 的日期（今天）
        gztime = realtime.get("gztime")  # e.g. "2026-02-28 14:56"
        gz_date = gztime[:10] if gztime and len(gztime) >= 10 else None

        history_date = latest_date.strftime("%Y-%m-%d") if latest_date else None
        jzrq = realtime.get("jzrq")  # 已公布净值日期（可能昨天）

        if market_status == "open_estimate":
            nav_date = gz_date or history_date or jzrq or "—"
        elif market_status in ("real", "closed"):
            nav_date = history_date or jzrq or gz_date or "—"
        else:
            nav_date = history_date or jzrq or gz_date or "—"

        # current 展示逻辑
        if market_status == "real" and latest_value is not None:
            current = float(latest_value)
        elif market_status == "open_estimate" and current_est is not None:
            current = float(current_est)
        elif market_status == "closed":
            if latest_value is not None:
                current = float(latest_value)
            elif dwjz is not None:
                current = float(dwjz)
            else:
                current = 0.0
        else:
            current = float(current_est) if current_est is not None else (float(dwjz) if dwjz is not None else 0.0)

        asset = current * float(amount)
        cost = float(buy_price) * float(amount)
        profit = asset - cost

        # 预估/实际收益（乘份额）
        est_ps = info.get("today_estimate_profit_per_share")
        real_ps = info.get("today_real_profit_per_share")

        today_estimate_profit = (float(est_ps) * float(amount)) if est_ps is not None else None
        today_real_profit = (float(real_ps) * float(amount)) if real_ps is not None else None

        # 兼容字段：today_profit（优先真实，否则预估，否则 0）
        today_profit = float(info.get("today_profit_per_share", 0.0)) * float(amount)

        total_asset += asset
        total_cost += cost
        total_today_profit += today_profit

        today_estimate_percent = info.get("today_estimate_percent")
        today_real_percent = info.get("today_real_percent")

        funds.append({
            "code": code,
            "name": realtime.get("name"),
            "current": round(current, 4),
            "buy_price": float(buy_price),
            "amount": float(amount),
            "profit": round(profit, 2),
            "percent": round(profit / cost * 100, 2) if cost > 0 else 0,
            "holding": round(asset, 2),

            # ✅ 前端 “净值日” 就读这个
            "nav_date": nav_date,

            "gszzl": float(realtime.get("gszzl") or 0),

            "today_profit": round(today_profit, 2),

            "today_estimate_percent": round(today_estimate_percent, 2) if today_estimate_percent is not None else None,
            "today_real_percent": round(today_real_percent, 2) if today_real_percent is not None else None,
            "today_estimate_profit": round(today_estimate_profit, 2) if today_estimate_profit is not None else None,
            "today_real_profit": round(today_real_profit, 2) if today_real_profit is not None else None,

            "market_status": market_status,
            "today_display": info.get("display_text")
        })

    # 可选：把组合层面的预估/实际也带上（不影响你现有前端）
    return jsonify({
        "funds": funds,
        "total_asset": round(total_asset, 2),
        "total_profit": round(total_asset - total_cost, 2),
        "total_percent": round((total_asset - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
        "today_profit": round(total_today_profit, 2),
    })


@app.route("/history/<code>/<period>")
def history(code, period):
    data = fetch_history(code)
    if not data:
        return jsonify([])

    today = now_cn()
    if period == "1m":
        cutoff = today - timedelta(days=30)
    elif period == "3m":
        cutoff = today - timedelta(days=90)
    elif period == "6m":
        cutoff = today - timedelta(days=180)
    else:
        cutoff = today - timedelta(days=365)

    cutoff_naive = cutoff.replace(tzinfo=None)
    filtered = [d for d in data if datetime.strptime(d["date"], "%Y-%m-%d") >= cutoff_naive]
    return jsonify(filtered)


@app.route("/")
def home():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
