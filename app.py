from flask import Flask, request, jsonify, session, render_template
import sqlite3, os, json, requests, re
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = "replace_with_random_secret_key"
DB_PATH = os.path.join(os.path.dirname(__file__), "fund.db")

# ===== 初始化数据库 =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 用户表
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 持仓表
    c.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username,password) VALUES (?,?)",
                  (username, generate_password_hash(password)))
        conn.commit()
        conn.close()
        return jsonify({"status":"ok"})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error":"用户名已存在"}),400

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username","").strip()
    password = data.get("password","").strip()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id,password FROM users WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    if not row or not check_password_hash(row[1], password):
        return jsonify({"error":"用户名或密码错误"}),400
    session["user_id"] = row[0]
    session["username"] = username
    return jsonify({"status":"ok", "username":username})

@app.route("/logout")
def logout():
    session.clear()
    return jsonify({"status":"ok"})

def current_user():
    if "user_id" in session:
        return session["user_id"], session["username"]
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
    if not user_id: return jsonify({"error":"请先登录"}),401
    data = request.json
    code = data["code"].strip()
    buy_price = float(data["buy_price"])
    amount = float(data["amount"])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM holdings WHERE user_id=? AND code=?", (user_id, code))
    if c.fetchone():
        conn.close()
        return jsonify({"error":"该基金已存在，请直接操作仓位"}),400
    c.execute("INSERT INTO holdings (user_id, code, buy_price, amount) VALUES (?,?,?,?)",
              (user_id, code, buy_price, amount))
    conn.commit()
    conn.close()
    return jsonify({"status":"ok"})

@app.route("/update/<code>", methods=["POST"])
def update_position(code):
    user_id,_ = current_user()
    if not user_id: return jsonify({"error":"请先登录"}),401
    data = request.json
    delta = float(data.get("delta",0))
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT amount FROM holdings WHERE user_id=? AND code=?", (user_id, code))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error":"基金不存在"}),400
    new_amount = row[0] + delta
    if new_amount <= 0:
        c.execute("DELETE FROM holdings WHERE user_id=? AND code=?", (user_id, code))
        new_amount = 0
    else:
        c.execute("UPDATE holdings SET amount=? WHERE user_id=? AND code=?", (new_amount, user_id, code))
    conn.commit()
    conn.close()
    return jsonify({"status":"ok","new_amount":new_amount})

@app.route("/delete/<code>", methods=["DELETE"])
def delete(code):
    user_id,_ = current_user()
    if not user_id: return jsonify({"error":"请先登录"}),401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM holdings WHERE user_id=? AND code=?", (user_id, code))
    conn.commit()
    conn.close()
    return jsonify({"status":"deleted"})

@app.route("/holdings")
def holdings():
    user_id,_ = current_user()
    if not user_id: return jsonify({"error":"请先登录"}),401
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code,buy_price,amount FROM holdings WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()

    funds=[]
    total_asset=0
    total_cost=0
    for code,buy_price,amount in rows:
        realtime = fetch_realtime(code)
        if not realtime: continue
        current=float(realtime["gsz"])
        asset=current*amount
        cost=buy_price*amount
        profit=asset-cost
        percent=(current-buy_price)/buy_price*100
        total_asset+=asset
        total_cost+=cost
        funds.append({
            "code":code,
            "name":realtime["name"],
            "current":current,
            "buy_price":buy_price,
            "amount":amount,
            "profit":round(profit,2),
            "percent":round(percent,2),
            "gszzl":realtime["gszzl"]
        })
    return jsonify({
        "funds":funds,
        "total_asset":round(total_asset,2),
        "total_profit":round(total_asset-total_cost,2),
        "total_percent":round((total_asset-total_cost)/total_cost*100,2) if total_cost>0 else 0
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
