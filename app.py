"""
Nkɔsoɔ AI - Complete Farming Assistant for Ghana
Nkɔsoɔ means 'growth' in Twi

Features:
  - Live weather for all 16 Ghana regions (Open-Meteo)
  - AI chat in English and Twi (Claude)
  - Photo crop disease diagnosis (Claude Vision)
  - Market prices for 8 major crops
  - Pest & disease alerts
  - Usage limits (5 questions/day free, unlimited Pro)
  - Paystack payments GHS 30/month
  - Admin dashboard

Environment variables (set on Render):
  ANTHROPIC_API_KEY    - from console.anthropic.com
  PAYSTACK_SECRET_KEY  - from paystack.com dashboard
  PAYSTACK_PUBLIC_KEY  - from paystack.com dashboard
  ADMIN_PASSWORD       - your chosen admin password
  SECRET_KEY           - any random string

Run locally:
  pip install flask anthropic requests gunicorn
  export ANTHROPIC_API_KEY=sk-ant-...
  python app.py
"""

import os, json, time, sqlite3, requests, anthropic
from flask import (Flask, render_template, request, jsonify,
                   stream_with_context, Response, session, redirect)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nkosoo-secret-2024")

client          = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC = os.environ.get("PAYSTACK_PUBLIC_KEY", "")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD", "nkosoo2024")

FREE_DAILY_LIMIT    = 5
FREE_DIAGNOSE_LIMIT = 3   # per month

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nkosoo.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT UNIQUE NOT NULL,
            email        TEXT,
            plan         TEXT DEFAULT 'free',
            region       TEXT DEFAULT 'greater_accra',
            lang         TEXT DEFAULT 'en',
            created_at   TEXT DEFAULT (datetime('now')),
            pro_since    TEXT,
            paystack_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS usage (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            type         TEXT NOT NULL,
            question     TEXT,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            reference    TEXT UNIQUE NOT NULL,
            amount       INTEGER,
            status       TEXT DEFAULT 'pending',
            created_at   TEXT DEFAULT (datetime('now'))
        );
        """)

init_db()

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def get_sid():
    if "sid" not in session:
        import uuid
        session["sid"] = str(uuid.uuid4())
    return session["sid"]

def get_or_create_user(sid):
    with get_db() as db:
        user = db.execute("SELECT * FROM users WHERE session_id=?", (sid,)).fetchone()
        if not user:
            db.execute("INSERT INTO users (session_id) VALUES (?)", (sid,))
            db.commit()
            user = db.execute("SELECT * FROM users WHERE session_id=?", (sid,)).fetchone()
    return dict(user)

def get_usage_today(sid):
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM usage WHERE session_id=? AND type='chat' AND date(created_at)=date('now')",
            (sid,)).fetchone()[0]

def get_diagnose_month(sid):
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM usage WHERE session_id=? AND type='diagnose' AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')",
            (sid,)).fetchone()[0]

def log_usage(sid, type_, question=None):
    with get_db() as db:
        db.execute("INSERT INTO usage (session_id,type,question) VALUES (?,?,?)",
                   (sid, type_, question[:300] if question else None))
        db.commit()

def is_pro(user):
    return user.get("plan") == "pro"

# ---------------------------------------------------------------------------
# AI prompts
# ---------------------------------------------------------------------------
SYSTEM_EN = """You are Nkɔsoɔ AI, a knowledgeable and friendly farming assistant for
smallholder farmers in Ghana and West Africa. Nkɔsoɔ means 'growth' in Twi.

Help farmers with:
1. WEATHER & IRRIGATION - planting windows, irrigation timing, climate risks
2. MARKET PRICES - when to sell, price trends for Ghanaian crops
3. PEST & DISEASE - identify symptoms, recommend affordable local treatments
4. CROP PLANNING - which crops suit the season, soil, and rainfall

Give practical, affordable advice. Be concise, warm, use simple language.
Mention local crop names and markets (Kumasi, Accra, Tamale) when relevant.
Keep replies under 250 words. If asked about non-farming topics, redirect gently.
"""

SYSTEM_TW = """Wo yɛ Nkɔsoɔ AI, obi a ɔnim adwuma ho asɛm na ɔboa nnomkuo afuom adwumayɛfoɔ
wɔ Ghana ne Atɔeɛ Afrika mu. Nkɔsoɔ kyerɛ sɛ 'nkɔso' wɔ Twi kasa mu.

Woboa wɔn ho asɛm a ɛfa:
1. ƆHAW NE NSUO - ɛberɛ a ɛsɛ sɛ wɔdua aba, nsuo hohorow, ne ɔhaw tumi
2. AGUADI TENTEENE - ɛberɛ a ɛsɛ sɛ wɔtɔn aba ne wuramu tenteene
3. MMOA NE YAREƐ - hunu nsɛnkyerɛnne, ka aduro ho asɛm
4. DUA ABA NHYEHYƐE - aba bɛn na ɛfata ɔberɛ no, asaase no, ne osu no

Ka asɛm no ntɛm, dwoodwoo, na sɔ wɔn da. Fa kasa a ɛyɛ mmerɛw.
"""

DISEASE_EN = """You are an expert plant pathologist for Ghana and West Africa crops.
Analyze this crop photo and provide a clear report with these sections:

1. DISEASE/PEST NAME
2. CROP AFFECTED
3. SEVERITY — Low / Medium / High
4. SYMPTOMS — describe what you see in the photo
5. TREATMENT — affordable steps for Ghana smallholder farmers (mention local products)
6. PREVENTION — how to stop this happening again

Be specific and practical. If the image is not a crop or plant, say so politely.
"""

DISEASE_TW = """Wo yɛ ogya a ɔnim aba yareɛ wɔ Ghana ne Atɔeɛ Afrika afuo mu.
Hwɛ saa foto yi na ka asɛm wɔ akwan wɔ ase yi mu:

1. YAREƐ/MMOA DIN
2. ABA A ƐWƆ SO
3. YAREƐ TENTEN — Ketewa / Mfinimfini / Kɛseɛ
4. NSƐNKYERƐNNE — ka deɛ wohunu wɔ foto no mu
5. ADURO — nkyerɛ aduro a ɛyɛ mmerɛw na ɛho hia a nnomkuo wɔ Ghana bɛtumi de ayɛ
6. BANBƆ — ɛdeɛn na wɔbɛtumi ayɛ sɛ eyi ammɛba bio
"""

# ---------------------------------------------------------------------------
# Ghana regions
# ---------------------------------------------------------------------------
GHANA_REGIONS = {
    "greater_accra": {"name":"Greater Accra (Accra)",        "name_tw":"Accra Kuro",             "lat":5.55,  "lon":-0.20},
    "ashanti":       {"name":"Ashanti (Kumasi)",              "name_tw":"Ashanti (Kumasi)",        "lat":6.69,  "lon":-1.62},
    "northern":      {"name":"Northern (Tamale)",             "name_tw":"Atifi (Tamale)",          "lat":9.40,  "lon":-0.85},
    "central":       {"name":"Central (Cape Coast)",          "name_tw":"Mfinimfini (Cape Coast)", "lat":5.10,  "lon":-1.25},
    "bono":          {"name":"Bono (Sunyani)",                "name_tw":"Bono (Sunyani)",          "lat":7.33,  "lon":-2.33},
    "eastern":       {"name":"Eastern (Koforidua)",           "name_tw":"Apuei (Koforidua)",       "lat":6.09,  "lon":-0.26},
    "volta":         {"name":"Volta (Ho)",                    "name_tw":"Volta (Ho)",              "lat":6.60,  "lon": 0.47},
    "upper_west":    {"name":"Upper West (Wa)",               "name_tw":"Atifi Atɔeɛ (Wa)",       "lat":10.06, "lon":-2.50},
    "upper_east":    {"name":"Upper East (Bolgatanga)",       "name_tw":"Atifi Apuei (Bolgatanga)","lat":10.79, "lon":-0.85},
    "western":       {"name":"Western (Takoradi)",            "name_tw":"Atɔeɛ (Takoradi)",       "lat":4.90,  "lon":-1.76},
    "oti":           {"name":"Oti (Dambai)",                  "name_tw":"Oti (Dambai)",            "lat":7.97,  "lon": 0.18},
    "bono_east":     {"name":"Bono East (Techiman)",          "name_tw":"Bono Apuei (Techiman)",   "lat":7.59,  "lon":-1.94},
    "ahafo":         {"name":"Ahafo (Goaso)",                 "name_tw":"Ahafo (Goaso)",           "lat":6.80,  "lon":-2.52},
    "western_north": {"name":"Western North (Sefwi Wiawso)", "name_tw":"Atɔeɛ Atifi (Sefwi)",    "lat":6.20,  "lon":-2.47},
    "north_east":    {"name":"North East (Nalerigu)",         "name_tw":"Atifi Apuei (Nalerigu)",  "lat":10.52, "lon":-0.36},
    "savannah":      {"name":"Savannah (Damongo)",            "name_tw":"Savannah (Damongo)",      "lat":9.08,  "lon":-1.82},
}

# ---------------------------------------------------------------------------
# Weather (cached 30 min to avoid rate limits)
# ---------------------------------------------------------------------------
_weather_cache = {}

FALLBACK = {
    "today_high":34,"humidity":78,"rain_chance":60,"wind_kmh":14,
    "forecast":[
        {"day":"Mon","icon":"☀️","high":34,"rain":10},
        {"day":"Tue","icon":"⛅","high":33,"rain":30},
        {"day":"Wed","icon":"🌧️","high":29,"rain":75},
        {"day":"Thu","icon":"🌧️","high":28,"rain":80},
        {"day":"Fri","icon":"⛅","high":31,"rain":40},
        {"day":"Sat","icon":"☀️","high":33,"rain":15},
        {"day":"Sun","icon":"☀️","high":34,"rain":10},
    ],
    "advice":"Weather data temporarily unavailable. Please check back shortly.",
    "advice_tw":"Ɔhaw ho nsɛm nni hɔ seesei.",
    "risk":"Data unavailable","risk_tw":"Nsɛm nni hɔ","risk_level":"warn","live":False,
}

def get_weather(region_key="greater_accra"):
    region  = GHANA_REGIONS.get(region_key, GHANA_REGIONS["greater_accra"])
    cached  = _weather_cache.get(region_key)
    if cached and time.time() - cached["ts"] < 1800:
        return cached["data"]
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={region['lat']}&longitude={region['lon']}"
               f"&daily=temperature_2m_max,precipitation_probability_max,windspeed_10m_max"
               f"&hourly=relativehumidity_2m&forecast_days=7&timezone=Africa%2FAccra")
        r = requests.get(url, timeout=8); r.raise_for_status()
        d = r.json(); daily = d["daily"]
        days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        icon = lambda p: "☀️" if p<30 else ("⛅" if p<60 else "🌧️")
        forecast = [{"day":days[i%7],"icon":icon(daily["precipitation_probability_max"][i]),
                     "high":round(daily["temperature_2m_max"][i]),
                     "rain":daily["precipitation_probability_max"][i]}
                    for i in range(min(7,len(daily["temperature_2m_max"])))]
        rain = daily["precipitation_probability_max"][0]
        if rain >= 70:
            adv    = f"Heavy rain expected in {region['name']}. Avoid pesticides. Check drainage."
            adv_tw = f"Osu kɛseɛ reba wɔ {region['name_tw']}. Mma aduro gu afuo so."
            risk,risk_tw,rl = "High flood risk","Osu kɛseɛ tumi de ɔhaw ba","danger"
        elif rain >= 40:
            adv    = f"Moderate rain likely in {region['name']}. Skip irrigation on rainy days."
            adv_tw = f"Osu kakra reba wɔ {region['name_tw']}. Mma nsuo gu afuo so."
            risk,risk_tw,rl = "Moderate moisture risk","Nsuo haw mfinimfini","warn"
        else:
            adv    = f"Dry conditions in {region['name']}. Irrigate crops well, especially seedlings."
            adv_tw = f"Awia bɛyɛ den wɔ {region['name_tw']}. Ma nsuo gu wo aba so."
            risk,risk_tw,rl = "Low rainfall — monitor moisture","Osu ketewa — hwɛ asaase nsuo","ok"
        result = {
            "location":region["name"],"location_tw":region["name_tw"],
            "today_high":round(daily["temperature_2m_max"][0]),
            "humidity":d["hourly"]["relativehumidity_2m"][12],
            "rain_chance":rain,"wind_kmh":round(daily["windspeed_10m_max"][0]),
            "forecast":forecast,"advice":adv,"advice_tw":adv_tw,
            "risk":risk,"risk_tw":risk_tw,"risk_level":rl,"live":True,
        }
        _weather_cache[region_key] = {"ts":time.time(),"data":result}
        return result
    except Exception as e:
        print(f"[Weather error] {e}")
        if cached: return cached["data"]
        fb = dict(FALLBACK)
        fb["location"] = region["name"]; fb["location_tw"] = region["name_tw"]
        return fb

# ---------------------------------------------------------------------------
# Static data
# ---------------------------------------------------------------------------
PRICE_DATA = [
    {"crop":"Maize",    "crop_tw":"Aburo",   "unit":"50kg bag",   "price":240,"change":8,  "trend":"up"},
    {"crop":"Tomatoes", "crop_tw":"Ntomato", "unit":"crate",      "price":180,"change":-12,"trend":"down"},
    {"crop":"Cassava",  "crop_tw":"Bankye",  "unit":"100kg bag",  "price":310,"change":0,  "trend":"stable"},
    {"crop":"Plantain", "crop_tw":"Ɔgede",   "unit":"bunch",      "price":55, "change":3,  "trend":"up"},
    {"crop":"Yam",      "crop_tw":"Bayerɛ",  "unit":"tuber (lg)", "price":35, "change":5,  "trend":"up"},
    {"crop":"Cocoa",    "crop_tw":"Kookoo",  "unit":"kg dry bean","price":24, "change":2,  "trend":"up"},
    {"crop":"Pepper",   "crop_tw":"Mako",    "unit":"kg",         "price":28, "change":-5, "trend":"down"},
    {"crop":"Groundnut","crop_tw":"Nkatie",  "unit":"50kg bag",   "price":290,"change":1,  "trend":"stable"},
]

PEST_DATA = [
    {"name":"Fall Armyworm","name_tw":"Aburo Mmoa","crops":"Maize","crops_tw":"Aburo",
     "description":"High risk season. Check leaves for feeding damage and egg masses.",
     "description_tw":"Ɔberɛ kɛseɛ. Hwɛ nkotokuo so sɛ mmoa adidi so.",
     "level":"high","tip":"Apply neem-based spray early morning. Report to extension officer.",
     "tip_tw":"De neem aduro gu so anɔpa."},
    {"name":"Cassava Leaf Blight","name_tw":"Bankye Nkotokuo Yareɛ","crops":"Cassava","crops_tw":"Bankye",
     "description":"Humidity-driven fungal disease. Angular brown spots on leaves.",
     "description_tw":"Yareɛ a nsuo ɛma aba. Bankye nkotokuo so akyene borɔ aba.",
     "level":"medium","tip":"Remove infected leaves. Space plants for better airflow.",
     "tip_tw":"Yi nkotokuo a yareɛ wɔ so no."},
    {"name":"Aphids","name_tw":"Mmoa Ketewa","crops":"Vegetables","crops_tw":"Atosɔde",
     "description":"Low pressure this week. Monitor undersides of leaves.",
     "description_tw":"Ɔhaw ketewa wiemuhyɛn yi. Hwɛ nkotokuo ase.",
     "level":"low","tip":"Spray with diluted soapy water or introduce ladybird beetles.",
     "tip_tw":"De nsuo ne sapo mu ngu so."},
    {"name":"Cassava Mosaic Virus","name_tw":"Bankye Yareɛ Kɛseɛ","crops":"Cassava","crops_tw":"Bankye",
     "description":"Spread by whiteflies. Yellowing and distortion of leaves.",
     "description_tw":"Nsansanwa na ɛde ba. Nkotokuo sere na wɔsɛe.",
     "level":"medium","tip":"Use certified disease-free planting material.",
     "tip_tw":"Fa bankye a yareɛ nni so."},
]

QUICK_EN = [
    "What crop should I plant in Ghana during the minor rainy season?",
    "How do I treat Fall Armyworm on my maize with local methods?",
    "When is the best time to sell my tomatoes for the best price?",
    "How do I know if my soil is ready for planting?",
    "What fertilizer should I use for maize farming in Ghana?",
    "How can I store my harvest longer without refrigeration?",
]

QUICK_TW = [
    "Aba bɛn na mɛdua wɔ Ghana wɔ osu ketewa ɔberɛ?",
    "Ɛdeɛn na mɛyɛ aburo mmoa ho wɔ me aburo afuo so?",
    "Ɛberɛ bɛn na ɛsɛ sɛ metɔn me ntomato na menya wuramu pa?",
    "Ɛdeɛn na mɛhunu sɛ me asaase atoto adua?",
    "Aduro bɛn na mɛfa ama aburo adwuma wɔ Ghana?",
    "Ɛdeɛn na mɛtumi de me aba sie akɔ akyiri sen saa?",
]

# ---------------------------------------------------------------------------
# Main route
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    sid  = get_sid()
    user = get_or_create_user(sid)
    region = user.get("region", "greater_accra")
    return render_template("index.html",
        weather        = get_weather(region),
        prices         = PRICE_DATA,
        pests          = PEST_DATA,
        quick_en       = QUICK_EN,
        quick_tw       = QUICK_TW,
        regions        = GHANA_REGIONS,
        user           = user,
        used_today     = get_usage_today(sid),
        used_diagnose  = get_diagnose_month(sid),
        free_limit     = FREE_DAILY_LIMIT,
        diagnose_limit = FREE_DIAGNOSE_LIMIT,
        api_ready      = bool(client.api_key),
        paystack_public= PAYSTACK_PUBLIC,
    )

# ---------------------------------------------------------------------------
# Weather API
# ---------------------------------------------------------------------------
@app.route("/api/weather")
def api_weather():
    key = request.args.get("region","greater_accra")
    if key not in GHANA_REGIONS: key = "greater_accra"
    sid = get_sid()
    with get_db() as db:
        db.execute("UPDATE users SET region=? WHERE session_id=?",(key,sid)); db.commit()
    return jsonify(get_weather(key))

# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    sid  = get_sid()
    user = get_or_create_user(sid)
    data = request.get_json()
    messages = data.get("messages",[])
    lang     = data.get("lang","en")
    if not messages: return jsonify({"error":"No messages"}),400
    if not client.api_key: return jsonify({"error":"ANTHROPIC_API_KEY not set"}),500
    if not is_pro(user):
        used = get_usage_today(sid)
        if used >= FREE_DAILY_LIMIT:
            return jsonify({
                "error":"limit_reached",
                "message":f"You have used all {FREE_DAILY_LIMIT} free questions today. Upgrade to Pro for unlimited access.",
                "message_tw":f"Woafa wo nsɛmmisa {FREE_DAILY_LIMIT} nyinaa nnɛ. Sesa akɔ Pro.",
                "used":used,"limit":FREE_DAILY_LIMIT
            }),429
    log_usage(sid,"chat",messages[-1].get("content","") if messages else "")
    system = SYSTEM_TW if lang=="tw" else SYSTEM_EN
    def generate():
        with client.messages.stream(model="claude-sonnet-4-20250514",max_tokens=1024,
                system=system,messages=messages) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text':text})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(stream_with_context(generate()),mimetype="text/event-stream")

# ---------------------------------------------------------------------------
# Diagnose API
# ---------------------------------------------------------------------------
@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    sid  = get_sid()
    user = get_or_create_user(sid)
    if not client.api_key: return jsonify({"error":"ANTHROPIC_API_KEY not set"}),500
    if not is_pro(user):
        used = get_diagnose_month(sid)
        if used >= FREE_DIAGNOSE_LIMIT:
            return jsonify({"error":"limit_reached",
                "message":f"You have used all {FREE_DIAGNOSE_LIMIT} free diagnoses this month. Upgrade to Pro for unlimited."}),429
    data       = request.get_json()
    image_b64  = data.get("image")
    media_type = data.get("media_type","image/jpeg")
    lang       = data.get("lang","en")
    if not image_b64: return jsonify({"error":"No image provided"}),400
    log_usage(sid,"diagnose","photo diagnosis")
    prompt = DISEASE_TW if lang=="tw" else DISEASE_EN
    def generate():
        with client.messages.stream(model="claude-sonnet-4-20250514",max_tokens=1024,
                messages=[{"role":"user","content":[
                    {"type":"image","source":{"type":"base64","media_type":media_type,"data":image_b64}},
                    {"type":"text","text":prompt}
                ]}]) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text':text})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(stream_with_context(generate()),mimetype="text/event-stream")

# ---------------------------------------------------------------------------
# Usage API
# ---------------------------------------------------------------------------
@app.route("/api/usage")
def api_usage():
    sid  = get_sid()
    user = get_or_create_user(sid)
    return jsonify({"plan":user["plan"],"used_today":get_usage_today(sid),
                    "used_diagnose":get_diagnose_month(sid),
                    "free_limit":FREE_DAILY_LIMIT,"diagnose_limit":FREE_DIAGNOSE_LIMIT})

# ---------------------------------------------------------------------------
# Paystack
# ---------------------------------------------------------------------------
@app.route("/api/pay/init", methods=["POST"])
def pay_init():
    sid  = get_sid()
    data = request.get_json()
    email = data.get("email","").strip()
    if not PAYSTACK_SECRET: return jsonify({"error":"Paystack not configured"}),500
    if not email or "@" not in email: return jsonify({"error":"Valid email required"}),400
    with get_db() as db:
        db.execute("UPDATE users SET email=? WHERE session_id=?",(email,sid)); db.commit()
    resp = requests.post("https://api.paystack.co/transaction/initialize",
        headers={"Authorization":f"Bearer {PAYSTACK_SECRET}","Content-Type":"application/json"},
        json={"email":email,"amount":3000,"currency":"GHS",
              "callback_url":request.host_url+"pay/verify",
              "metadata":{"session_id":sid}})
    result = resp.json()
    if result.get("status"):
        ref = result["data"]["reference"]
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO payments (session_id,reference,amount,status) VALUES (?,?,3000,'pending')",(sid,ref))
            db.commit()
        return jsonify({"authorization_url":result["data"]["authorization_url"],"reference":ref})
    return jsonify({"error":result.get("message","Payment init failed")}),400

@app.route("/pay/verify")
def pay_verify():
    ref = request.args.get("reference","")
    if not ref or not PAYSTACK_SECRET: return redirect("/?payment=failed")
    resp = requests.get(f"https://api.paystack.co/transaction/verify/{ref}",
        headers={"Authorization":f"Bearer {PAYSTACK_SECRET}"})
    result = resp.json()
    if result.get("status") and result["data"]["status"]=="success":
        sid = result["data"].get("metadata",{}).get("session_id",get_sid())
        with get_db() as db:
            db.execute("UPDATE payments SET status='success' WHERE reference=?",(ref,))
            db.execute("UPDATE users SET plan='pro',pro_since=datetime('now'),paystack_ref=? WHERE session_id=?",(ref,sid))
            db.commit()
        session["sid"] = sid
        return redirect("/?payment=success")
    return redirect("/?payment=failed")

# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route("/admin", methods=["GET","POST"])
def admin():
    if request.method=="POST":
        if request.form.get("password")==ADMIN_PASSWORD:
            session["admin"]=True
        else:
            return render_template("admin_login.html",error="Wrong password")
    if not session.get("admin"):
        return render_template("admin_login.html",error=None)
    with get_db() as db:
        total   = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pro     = db.execute("SELECT COUNT(*) FROM users WHERE plan='pro'").fetchone()[0]
        q_today = db.execute("SELECT COUNT(*) FROM usage WHERE type='chat' AND date(created_at)=date('now')").fetchone()[0]
        d_today = db.execute("SELECT COUNT(*) FROM usage WHERE type='diagnose' AND date(created_at)=date('now')").fetchone()[0]
        top_q   = db.execute("SELECT question,COUNT(*) cnt FROM usage WHERE type='chat' AND question IS NOT NULL GROUP BY question ORDER BY cnt DESC LIMIT 10").fetchall()
        users   = db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
        regions = db.execute("SELECT region,COUNT(*) cnt FROM users GROUP BY region ORDER BY cnt DESC").fetchall()
    return render_template("admin.html",
        total_users=total,pro_users=pro,total_revenue=pro*30,
        questions_today=q_today,diagnoses_today=d_today,
        top_questions=top_q,recent_users=users,region_stats=regions,
        regions=GHANA_REGIONS)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin",None); return redirect("/admin")

# ---------------------------------------------------------------------------
# Offline cache data API — returns JSON snapshot for service worker to cache
# ---------------------------------------------------------------------------
@app.route("/api/cache-data")
def api_cache_data():
    """Returns a single JSON payload of weather, prices and pests for offline use."""
    sid = get_sid()
    with get_db() as db:
        row = db.execute("SELECT region FROM users WHERE session_id=?", (sid,)).fetchone()
    region = row["region"] if row else "greater_accra"
    return jsonify({
        "weather": get_weather(region),
        "prices":  PRICE_DATA,
        "pests":   PEST_DATA,
        "region":  region,
        "cached_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
    })

# Offline queue — farmer's pending messages when offline
@app.route("/api/offline-queue", methods=["POST"])
def api_offline_queue():
    """Receives queued messages that were saved while offline and processes them."""
    sid  = get_sid()
    data = request.get_json()
    messages = data.get("messages", [])
    lang     = data.get("lang", "en")
    if not messages:
        return jsonify({"error": "No messages"}), 400
    if not client.api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500
    user = get_or_create_user(sid)
    if not is_pro(user):
        used = get_usage_today(sid)
        if used >= FREE_DAILY_LIMIT:
            return jsonify({"error": "limit_reached",
                "message": f"Daily limit reached. Upgrade to Pro."}), 429
    log_usage(sid, "chat", messages[-1].get("content", "") if messages else "")
    system = SYSTEM_TW if lang == "tw" else SYSTEM_EN
    def generate():
        with client.messages.stream(model="claude-sonnet-4-20250514", max_tokens=1024,
                system=system, messages=messages) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text': text})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(stream_with_context(generate()), mimetype="text/event-stream")

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🌿 Nkɔsoɔ AI starting...")
    print("   API key set:", bool(client.api_key))
    print("   Paystack set:", bool(PAYSTACK_SECRET))
    print("   Open: http://localhost:5000\n")
    app.run(debug=True, port=5000)
