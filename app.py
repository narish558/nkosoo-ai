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
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

FREE_DAILY_LIMIT    = 5
FREE_DIAGNOSE_LIMIT = 3   # per month

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nkosoo.db"))

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
        CREATE TABLE IF NOT EXISTS farm_profiles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT UNIQUE NOT NULL,
            farmer_name  TEXT,
            phone        TEXT,
            ghana_card   TEXT,
            farm_size    REAL,
            size_unit    TEXT DEFAULT 'acres',
            soil_type    TEXT DEFAULT 'loamy',
            water_source TEXT DEFAULT 'rain',
            crops        TEXT DEFAULT '[]',
            region       TEXT DEFAULT 'greater_accra',
            latitude     REAL,
            longitude    REAL,
            gps_accuracy REAL,
            updated_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS planting_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL,
            crop         TEXT NOT NULL,
            action       TEXT NOT NULL,
            note         TEXT,
            log_date     TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        );
        """)

init_db()

# ---------------------------------------------------------------------------
# Migration — safely add new columns to existing live databases on Render
# ---------------------------------------------------------------------------
def run_migrations():
    with get_db() as db:
        existing = {row[1] for row in db.execute("PRAGMA table_info(farm_profiles)").fetchall()}
        migrations = [
            ("ghana_card",   "ALTER TABLE farm_profiles ADD COLUMN ghana_card TEXT"),
            ("latitude",     "ALTER TABLE farm_profiles ADD COLUMN latitude REAL"),
            ("longitude",    "ALTER TABLE farm_profiles ADD COLUMN longitude REAL"),
            ("gps_accuracy", "ALTER TABLE farm_profiles ADD COLUMN gps_accuracy REAL"),
        ]
        for col, sql in migrations:
            if col not in existing:
                db.execute(sql)
        db.commit()

run_migrations()

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
# Farm profile helpers
# ---------------------------------------------------------------------------
def get_farm_profile(sid):
    with get_db() as db:
        row = db.execute("SELECT * FROM farm_profiles WHERE session_id=?", (sid,)).fetchone()
    return dict(row) if row else {}

def save_farm_profile(sid, data):
    crops_json = json.dumps(data.get("crops", []))
    with get_db() as db:
        db.execute("""
            INSERT INTO farm_profiles
              (session_id,farmer_name,phone,ghana_card,farm_size,size_unit,
               soil_type,water_source,crops,region,latitude,longitude,gps_accuracy,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                farmer_name=excluded.farmer_name,
                phone=excluded.phone,
                ghana_card=excluded.ghana_card,
                farm_size=excluded.farm_size,
                size_unit=excluded.size_unit,
                soil_type=excluded.soil_type,
                water_source=excluded.water_source,
                crops=excluded.crops,
                region=excluded.region,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                gps_accuracy=excluded.gps_accuracy,
                updated_at=datetime('now')
        """, (sid,
              data.get("farmer_name", ""),
              data.get("phone", ""),
              data.get("ghana_card", ""),
              data.get("farm_size", 1),
              data.get("size_unit", "acres"),
              data.get("soil_type", "loamy"),
              data.get("water_source", "rain"),
              crops_json,
              data.get("region", "greater_accra"),
              data.get("latitude"),
              data.get("longitude"),
              data.get("gps_accuracy")))
        db.commit()

def get_planting_logs(sid):
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM planting_logs WHERE session_id=? ORDER BY log_date DESC LIMIT 50", (sid,)
        ).fetchall()
    return [dict(r) for r in rows]

def build_calendar_prompt(crop, profile, region_name, lang="en"):
    """Build a prompt for a SINGLE crop — fast, focused, never times out."""
    soil  = profile.get("soil_type", "loamy")
    water = profile.get("water_source", "rain")
    size  = f"{profile.get('farm_size', 1)} {profile.get('size_unit', 'acres')}"

    if lang == "tw":
        return f"""Wo yɛ Nkɔsoɔ AI. Yɛ aba dua kalenda ma {crop} nko ara:

Afuo kɛseɛ: {size} | Asaase: {soil} | Nsuo: {water} | Man: {region_name}

Yɛ JSON array a ɛwɔ osram 12 mu ma {crop} nko ara.
Gyina Ghana osu ɔberɛ so: Major rains (Mar-Jun), Minor rains (Sep-Nov), Dry (Nov-Mar) wɔ South;
Wet (May-Oct), Dry (Nov-Apr) wɔ North.
Ka JSON nko ara — mma preamble, mma markdown:
[{{"month":"January","tasks":[{{"action":"Land preparation","note":"..."}}]}}]"""
    else:
        return f"""You are Nkɔsoɔ AI. Generate a 12-month planting calendar for ONE crop only: {crop}

Farm: {size} | Soil: {soil} | Water: {water} | Region: {region_name}

List exact monthly tasks for {crop} only: planting, fertilizing, weeding, pest control, harvesting.
Use Ghana's seasons: Major rains (Mar–Jun), Minor rains (Sep–Nov), Dry (Nov–Mar) for South;
Single wet season (May–Oct), Dry (Nov–Apr) for Northern regions.
Include specific quantities, product names, and spacing where relevant.
If {crop} has no major tasks in a month, return an empty tasks array for that month.

Respond ONLY with valid JSON — no preamble, no markdown fences:
[{{"month":"January","tasks":[{{"action":"Land preparation","note":"Clear and till to 20cm. Apply lime if pH below 5.5."}}]}}]"""

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
    profile = get_farm_profile(sid)
    if profile and isinstance(profile.get("crops"), str):
        try: profile["crops"] = json.loads(profile["crops"])
        except: profile["crops"] = []
    return render_template("index.html",
        weather        = get_weather(region),
        prices         = PRICE_DATA,
        pests          = PEST_DATA,
        quick_en       = QUICK_EN,
        quick_tw       = QUICK_TW,
        regions        = GHANA_REGIONS,
        user           = user,
        profile        = profile,
        used_today     = get_usage_today(sid),
        used_diagnose  = get_diagnose_month(sid),
        free_limit     = FREE_DAILY_LIMIT,
        diagnose_limit = FREE_DIAGNOSE_LIMIT,
        api_ready      = bool(client.api_key),
        paystack_public= PAYSTACK_PUBLIC,
        google_maps_key= GOOGLE_MAPS_KEY,
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
        # ── Core stats ──────────────────────────────────────────
        total      = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pro        = db.execute("SELECT COUNT(*) FROM users WHERE plan='pro'").fetchone()[0]
        q_today    = db.execute("SELECT COUNT(*) FROM usage WHERE type='chat' AND date(created_at)=date('now')").fetchone()[0]
        d_today    = db.execute("SELECT COUNT(*) FROM usage WHERE type='diagnose' AND date(created_at)=date('now')").fetchone()[0]
        q_total    = db.execute("SELECT COUNT(*) FROM usage WHERE type='chat'").fetchone()[0]
        d_total    = db.execute("SELECT COUNT(*) FROM usage WHERE type='diagnose'").fetchone()[0]
        top_q      = db.execute("SELECT question,COUNT(*) cnt FROM usage WHERE type='chat' AND question IS NOT NULL GROUP BY question ORDER BY cnt DESC LIMIT 10").fetchall()
        users      = db.execute("SELECT u.*,fp.farmer_name,fp.farm_size,fp.size_unit,fp.crops,fp.ghana_card,fp.latitude,fp.longitude FROM users u LEFT JOIN farm_profiles fp ON u.session_id=fp.session_id ORDER BY u.created_at DESC LIMIT 20").fetchall()
        regions    = db.execute("SELECT region,COUNT(*) cnt FROM users GROUP BY region ORDER BY cnt DESC").fetchall()

        # ── Farm profile stats ──────────────────────────────────
        profiles_total  = db.execute("SELECT COUNT(*) FROM farm_profiles").fetchone()[0]
        profiles_named  = db.execute("SELECT COUNT(*) FROM farm_profiles WHERE farmer_name IS NOT NULL AND farmer_name!=''").fetchone()[0]
        avg_farm_size   = db.execute("SELECT ROUND(AVG(farm_size),1) FROM farm_profiles WHERE farm_size IS NOT NULL").fetchone()[0] or 0
        soil_stats      = db.execute("SELECT soil_type,COUNT(*) cnt FROM farm_profiles GROUP BY soil_type ORDER BY cnt DESC").fetchall()
        water_stats     = db.execute("SELECT water_source,COUNT(*) cnt FROM farm_profiles GROUP BY water_source ORDER BY cnt DESC").fetchall()
        size_unit_stats = db.execute("SELECT size_unit,COUNT(*) cnt FROM farm_profiles GROUP BY size_unit ORDER BY cnt DESC").fetchall()

        # ── Crop popularity ─────────────────────────────────────
        all_crops_rows  = db.execute("SELECT crops FROM farm_profiles WHERE crops IS NOT NULL AND crops!='[]'").fetchall()
        crop_counter    = {}
        for row in all_crops_rows:
            try:
                crops = json.loads(row[0]) if isinstance(row[0],str) else row[0]
                for c in crops:
                    crop_counter[c] = crop_counter.get(c,0) + 1
            except: pass
        top_crops = sorted(crop_counter.items(), key=lambda x: x[1], reverse=True)[:12]

        # ── Diary / calendar stats ──────────────────────────────
        logs_total      = db.execute("SELECT COUNT(*) FROM planting_logs").fetchone()[0]
        logs_today      = db.execute("SELECT COUNT(*) FROM planting_logs WHERE date(created_at)=date('now')").fetchone()[0]
        top_log_actions = db.execute("SELECT action,COUNT(*) cnt FROM planting_logs GROUP BY action ORDER BY cnt DESC LIMIT 6").fetchall()
        top_log_crops   = db.execute("SELECT crop,COUNT(*) cnt FROM planting_logs GROUP BY crop ORDER BY cnt DESC LIMIT 6").fetchall()

        # ── 7-day activity (questions per day) ──────────────────
        daily_activity  = db.execute("""
            SELECT date(created_at) dy, COUNT(*) cnt
            FROM usage WHERE type='chat'
            AND created_at >= date('now','-6 days')
            GROUP BY dy ORDER BY dy ASC
        """).fetchall()

    return render_template("admin.html",
        total_users=total, pro_users=pro, total_revenue=pro*30,
        questions_today=q_today, diagnoses_today=d_today,
        questions_total=q_total, diagnoses_total=d_total,
        top_questions=top_q, recent_users=users, region_stats=regions,
        profiles_total=profiles_total, profiles_named=profiles_named,
        avg_farm_size=avg_farm_size,
        soil_stats=soil_stats, water_stats=water_stats,
        size_unit_stats=size_unit_stats, top_crops=top_crops,
        logs_total=logs_total, logs_today=logs_today,
        top_log_actions=top_log_actions, top_log_crops=top_log_crops,
        daily_activity=daily_activity,
        regions=GHANA_REGIONS)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin",None); return redirect("/admin")

# ---------------------------------------------------------------------------
# Ghana Card validation (format check — NIA has no public API yet)
# ---------------------------------------------------------------------------
@app.route("/api/validate-ghana-card", methods=["POST"])
def validate_ghana_card():
    import re
    data   = request.get_json() or {}
    number = (data.get("number") or "").strip().upper()
    # Ghana Card format: GHA-XXXXXXXXX-X (GHA + 9 digits + 1 digit/letter)
    pattern = r'^GHA-\d{9}-\d$'
    if re.match(pattern, number):
        return jsonify({"valid": True})
    return jsonify({"valid": False,
        "message": "Format must be GHA-XXXXXXXXX-X (e.g. GHA-123456789-0)"})
@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    sid = get_sid()
    profile = get_farm_profile(sid)
    if profile and isinstance(profile.get("crops"), str):
        try: profile["crops"] = json.loads(profile["crops"])
        except: profile["crops"] = []
    return jsonify(profile)

@app.route("/api/profile", methods=["POST"])
def api_profile_save():
    sid  = get_sid()
    data = request.get_json()
    if not data: return jsonify({"error": "No data"}), 400
    # keep region in sync with users table too
    region = data.get("region","greater_accra")
    with get_db() as db:
        db.execute("UPDATE users SET region=? WHERE session_id=?",(region,sid)); db.commit()
    save_farm_profile(sid, data)
    return jsonify({"ok": True})

# ---------------------------------------------------------------------------
# Planting Calendar API — ONE crop per request, streams fast
# ---------------------------------------------------------------------------
@app.route("/api/calendar", methods=["POST"])
def api_calendar():
    sid  = get_sid()
    data = request.get_json() or {}
    lang = data.get("lang", "en")
    crop = (data.get("crop") or "").strip()

    if not crop:
        return jsonify({"error": "No crop specified"}), 400
    if not client.api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"}), 500

    # load saved profile for farm context; allow client to override fields
    profile = get_farm_profile(sid) or {}
    for field in ("region","farm_size","size_unit","soil_type","water_source"):
        if data.get(field):
            profile[field] = data[field]

    region_key  = profile.get("region", "greater_accra")
    region_name = GHANA_REGIONS.get(region_key, {}).get("name", "Ghana")
    prompt      = build_calendar_prompt(crop, profile, region_name, lang)

    def generate():
        full_text = ""
        try:
            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,          # single crop — much smaller, much faster
                messages=[{"role": "user", "content": prompt}]
            ) as stream:
                for text in stream.text_stream:
                    full_text += text
                    yield f"data: {json.dumps({'chunk': text})}\n\n"

            clean = full_text.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            calendar = json.loads(clean)
            yield f"data: {json.dumps({'done': True, 'crop': crop, 'calendar': calendar})}\n\n"

        except json.JSONDecodeError as e:
            print(f"[Calendar JSON error] {crop}: {e} | Raw: {full_text[:300]}")
            yield f"data: {json.dumps({'error': f'Invalid calendar for {crop}. Please try again.'})}\n\n"
        except Exception as e:
            print(f"[Calendar error] {crop}: {type(e).__name__}: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")

# ---------------------------------------------------------------------------
# Planting Log API
# ---------------------------------------------------------------------------
@app.route("/api/log", methods=["GET"])
def api_log_get():
    sid = get_sid()
    return jsonify({"logs": get_planting_logs(sid)})

@app.route("/api/log", methods=["POST"])
def api_log_add():
    sid  = get_sid()
    data = request.get_json()
    crop   = (data.get("crop","") or "").strip()
    action = (data.get("action","") or "").strip()
    note   = (data.get("note","") or "").strip()
    date   = (data.get("date","") or time.strftime("%Y-%m-%d"))
    if not crop or not action:
        return jsonify({"error":"crop and action required"}),400
    with get_db() as db:
        db.execute("INSERT INTO planting_logs (session_id,crop,action,note,log_date) VALUES (?,?,?,?,?)",
                   (sid,crop,action,note,date))
        db.commit()
    return jsonify({"ok":True})

@app.route("/api/log/<int:log_id>", methods=["DELETE"])
def api_log_delete(log_id):
    sid = get_sid()
    with get_db() as db:
        db.execute("DELETE FROM planting_logs WHERE id=? AND session_id=?",(log_id,sid))
        db.commit()
    return jsonify({"ok":True})

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
