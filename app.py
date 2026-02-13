from flask import Flask, request, jsonify, session, render_template
import os, json, requests, re
from datetime import datetime, timedelta
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

# ===== PostgreSQL 连接 =====
# DATABASE_URL = "postgresql://fund_manager_j5ml_user:Ph7l3aNSGQZEXAUtN6sakueJdrSJMKG9@dpg-d67kfi95pdvs73egn6mg-a.oregon-postgres.render.com/fund_manager_j5ml"
DATABASE_URL = os.environ.get("DATABASE_URL")
def get_conn():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL 未设置")

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
    data = request.json
    username = data.get("username","").strip()
    password = data.get("password","").strip()
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
    data = request.json
    username = data.get("username","").strip()
    password = data.get("password","").strip()

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
        text = r.text.replace("jsonpgz(","").replace(");","")
        return json.loads(text)
    except:
        return None


def fetch_history(code):
    try:
        url = f"https://fund.eastmoney.com/pingzhongdata/{code}.js"
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=8)
        match = re.search(r"Data_netWorthTrend\s*=\s*(.*?);", r.text)
        if not match: return []
        data_json = json.loads(match.group(1))
        return [{"date": datetime.fromtimestamp(d["x"]/1000).strftime("%Y-%m-%d"), "value": d["y"]} for d in data_json]
    except:
        return []


# ===== 持仓操作 =====
@app.route("/add", methods=["POST"])
def add():
    user_id,_ = current_user()
    if not user_id:
        return jsonify({"error":"请先登录"}),401

    data = request.json
    code = data.get("code","").strip()
    buy_price = data.get("buy_price")
    amount = data.get("amount")

    # 1. 基础输入校验
    if not code:
        return jsonify({"error":"基金代码不能为空"}),400
    try:
        buy_price = float(buy_price)
        amount = float(amount)
        if buy_price <= 0 or amount <= 0:
            return jsonify({"error":"买入价格和份额必须大于0"}),400
    except:
        return jsonify({"error":"买入价格和份额必须为数字"}),400

    # 2. 校验基金是否存在
    realtime = fetch_realtime(code)
    if not realtime:
        return jsonify({"error":"基金代码无效或不存在"}),400

    conn = get_conn()
    c = conn.cursor()
    # 3. 防止重复添加
    c.execute("SELECT id FROM holdings WHERE user_id=%s AND code=%s", (user_id, code))
    if c.fetchone():
        conn.close()
        return jsonify({"error":"该基金已存在，请直接操作仓位"}),400

    # 4. 插入数据库
    c.execute(
        "INSERT INTO holdings (user_id, code, buy_price, amount) VALUES (%s,%s,%s,%s)",
        (user_id, code, buy_price, amount)
    )
    conn.commit()
    conn.close()

    return jsonify({"status":"ok","name":realtime["name"]})



@app.route("/update/<code>", methods=["POST"])
def update_position(code):
    user_id,_ = current_user()
    if not user_id: return jsonify({"error":"请先登录"}),401

    data = request.json
    delta = float(data.get("delta",0))
    add_price = float(data.get("buy_price",0))

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
    if not user_id: return jsonify({"error":"请先登录"}),401

    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM holdings WHERE user_id=%s AND code=%s", (user_id, code))
    conn.commit()
    conn.close()

    return jsonify({"status":"deleted"})


@app.route("/holdings")
def holdings():
    user_id,_ = current_user()
    if not user_id: return jsonify({"error":"请先登录"}),401

    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT code,buy_price,amount FROM holdings WHERE user_id=%s", (user_id,))
    rows = c.fetchall()
    conn.close()

    funds = []
    total_asset = 0
    total_cost = 0
    total_today_profit = 0

    for code, buy_price, amount in rows:
        realtime = fetch_realtime(code)
        if not realtime: continue

        current = float(realtime["gsz"])
        gszzl = float(realtime["gszzl"])
        asset = current * amount
        cost = buy_price * amount
        profit = asset - cost
        today_profit = round(asset * gszzl / 100, 2)

        total_asset += asset
        total_cost += cost
        total_today_profit += today_profit

        funds.append({
            "code": code,
            "name": realtime["name"],
            "current": current,
            "buy_price": buy_price,
            "amount": amount,
            "profit": round(profit, 2),
            "percent": round(profit / cost * 100, 2) if cost > 0 else 0,
            "holding": round(asset, 2),
            "gszzl": gszzl,
            "today_profit": today_profit
        })

    return jsonify({
        "funds": funds,
        "total_asset": round(total_asset, 2),
        "total_profit": round(total_asset - total_cost, 2),
        "total_percent": round((total_asset - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
        "today_profit": round(total_today_profit, 2)
    })


@app.route("/history/<code>/<period>")
def history(code, period):
    data = fetch_history(code)
    if not data: return jsonify([])

    today = datetime.now()
    if period=="1m": cutoff=today-timedelta(days=30)
    elif period=="3m": cutoff=today-timedelta(days=90)
    elif period=="6m": cutoff=today-timedelta(days=180)
    else: cutoff=today-timedelta(days=365)

    filtered=[d for d in data if datetime.strptime(d["date"],"%Y-%m-%d")>=cutoff]
    return jsonify(filtered)


@app.route("/")
def home():
    return render_template("index.html")


if __name__=="__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
