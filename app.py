"""
Nkɔsoɔ AI - Complete Farming Assistant for Ghana
Version 2.0 — Crops + Livestock + Aquaculture

Features:
  - Unified theme (green for crops, amber for livestock)
  - Live weather for all 16 Ghana regions
  - AI chat in English and Twi (Claude)
  - Photo crop AND animal disease diagnosis
  - Market prices — crops and livestock
  - Pest & disease alerts
  - Farm profile with Ghana Card + GPS
  - Livestock profile — poultry, cattle, goats, sheep, pigs, fish
  - Voice interface — speak question, hear answer
  - Usage limits (5 questions/day free, unlimited Pro)
  - Paystack payments GHS 30/month
  - Admin dashboard

Environment variables:
  ANTHROPIC_API_KEY
  PAYSTACK_SECRET_KEY
  PAYSTACK_PUBLIC_KEY
  ADMIN_PASSWORD
  SECRET_KEY
"""

import os, json, time, re, sqlite3, requests, anthropic
from flask import (Flask, render_template, request, jsonify,
                   stream_with_context, Response, session, redirect)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nkosoo-secret-2024")

client          = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY","")
PAYSTACK_PUBLIC = os.environ.get("PAYSTACK_PUBLIC_KEY","")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD","nkosoo2024")

FREE_DAILY_LIMIT    = 5
FREE_DIAGNOSE_LIMIT = 3

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Use persistent disk if available, otherwise local
_data_dir = "/var/data" if os.path.isdir("/var/data") else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_data_dir, "nkosoo.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            email TEXT, plan TEXT DEFAULT 'free',
            region TEXT DEFAULT 'greater_accra',
            lang TEXT DEFAULT 'en',
            created_at TEXT DEFAULT (datetime('now')),
            pro_since TEXT, paystack_ref TEXT
        );
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, type TEXT NOT NULL,
            question TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, reference TEXT UNIQUE NOT NULL,
            amount INTEGER, status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS farm_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            farmer_name TEXT, phone TEXT,
            farm_size TEXT, farm_unit TEXT, crops TEXT,
            crop_type TEXT, ghana_card TEXT,
            ghana_card_valid INTEGER DEFAULT 0,
            soil_type TEXT, water_source TEXT,
            region TEXT, email TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS livestock_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            farmer_name TEXT, phone TEXT,
            ghana_card TEXT, ghana_card_valid INTEGER DEFAULT 0,
            region TEXT, email TEXT,
            animal_type TEXT,
            total_count INTEGER DEFAULT 0,
            sick_count INTEGER DEFAULT 0,
            housing_type TEXT, purpose TEXT,
            feed_source TEXT, water_source TEXT,
            nearest_vet TEXT, notes TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS health_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            animal_type TEXT,
            title TEXT, description TEXT,
            ai_tip TEXT, log_date TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        # Migrate existing farm_profiles — add missing columns safely
        existing = [r[1] for r in db.execute("PRAGMA table_info(farm_profiles)").fetchall()]
        for col, defn in [
            ("farm_unit","TEXT"), ("crop_type","TEXT"),
            ("region","TEXT"), ("email","TEXT"),
        ]:
            if col not in existing:
                db.execute(f"ALTER TABLE farm_profiles ADD COLUMN {col} {defn}")
        # Migrate existing livestock_profiles — add missing columns safely
        l_existing = [r[1] for r in db.execute("PRAGMA table_info(livestock_profiles)").fetchall()]
        for col, defn in [
            ("farmer_name","TEXT"), ("phone","TEXT"),
            ("ghana_card","TEXT"), ("ghana_card_valid","INTEGER DEFAULT 0"),
            ("region","TEXT"), ("email","TEXT"),
        ]:
            if col not in l_existing:
                db.execute(f"ALTER TABLE livestock_profiles ADD COLUMN {col} {defn}")
        db.commit()


init_db()

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def get_sid():
    if "sid" not in session:
        import uuid; session["sid"] = str(uuid.uuid4())
    return session["sid"]

def get_or_create_user(sid):
    with get_db() as db:
        u = db.execute("SELECT * FROM users WHERE session_id=?",(sid,)).fetchone()
        if not u:
            db.execute("INSERT INTO users (session_id) VALUES (?)",(sid,)); db.commit()
            u = db.execute("SELECT * FROM users WHERE session_id=?",(sid,)).fetchone()
    return dict(u)

def get_usage_today(sid):
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM usage WHERE session_id=? AND type='chat' AND date(created_at)=date('now')",(sid,)
        ).fetchone()[0]

def get_diagnose_month(sid):
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM usage WHERE session_id=? AND type='diagnose' AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')",(sid,)
        ).fetchone()[0]

def log_usage(sid, type_, question=None):
    with get_db() as db:
        db.execute("INSERT INTO usage (session_id,type,question) VALUES (?,?,?)",
                   (sid,type_,question[:300] if question else None)); db.commit()

def is_pro(user): return user.get("plan")=="pro"

# ---------------------------------------------------------------------------
# AI Prompts
# ---------------------------------------------------------------------------
SYSTEM_EN = """You are Nkɔsoɔ AI, a knowledgeable and friendly farming assistant for
smallholder farmers in Ghana and West Africa. Nkɔsoɔ means 'growth' in Twi.

Help farmers with:
1. CROPS — weather timing, planting, pest management, market prices, fertilizer advice
2. LIVESTOCK — poultry, cattle, goats, sheep, pigs, fish health and management
3. WEATHER — irrigation timing, planting windows, climate risks
4. MARKET — when to sell crops and livestock for best price

Be concise, warm, practical. Use simple language. Mention local markets (Kumasi, Accra,
Tamale) and local product names where relevant. Keep replies under 250 words.
"""

SYSTEM_TW = """Wo yɛ Nkɔsoɔ AI, obi a ɔnim adwuma ho asɛm na ɔboa nnomkuo afuom adwumayɛfoɔ
wɔ Ghana ne Atɔeɛ Afrika mu. Nkɔsoɔ kyerɛ sɛ 'nkɔso' wɔ Twi kasa mu.
Woboa wɔn ho asɛm a ɛfa aba dua, mmoa dii, ɔhaw, aguadi ne mmoa yareɛ.
Ka asɛm no ntɛm, dwoodwoo, na sɔ wɔn da. Fa kasa a ɛyɛ mmerɛw.
"""

DISEASE_CROP_EN = """You are an expert plant pathologist for Ghana and West Africa.
Analyze this crop photo and provide:
1. DISEASE/PEST NAME
2. CROP AFFECTED
3. SEVERITY — Low / Medium / High
4. SYMPTOMS visible in the photo
5. TREATMENT — affordable steps using locally available products in Ghana
6. PREVENTION for the future
Be specific and practical. If not a crop photo, say so politely.
"""

DISEASE_ANIMAL_EN = """You are an expert veterinarian for livestock in Ghana and West Africa.
Analyze this animal photo and provide:
1. DISEASE/CONDITION NAME
2. ANIMAL AFFECTED
3. SEVERITY — Low / Medium / High
4. SYMPTOMS visible in the photo
5. TREATMENT — practical steps for Ghana farmers, mention local products
6. PREVENTION — vaccination or management advice
If not an animal photo, say so politely.
"""

DISEASE_CROP_TW = """Wo yɛ ogya a ɔnim aba yareɛ wɔ Ghana afuo mu.
Hwɛ saa foto yi na ka: yareɛ din, aba a ɛwɔ so, yareɛ tenten, nsɛnkyerɛnne, aduro, ne banbɔ.
"""

DISEASE_ANIMAL_TW = """Wo yɛ onipa a ɔnim mmoa yareɛ wɔ Ghana mu.
Hwɛ saa foto yi na ka: yareɛ din, mmoa a ɛwɔ so, yareɛ tenten, nsɛnkyerɛnne, aduro, ne banbɔ.
"""

# ---------------------------------------------------------------------------
# Ghana regions
# ---------------------------------------------------------------------------
GHANA_REGIONS = {
    "greater_accra": {"name":"Greater Accra (Accra)",       "name_tw":"Accra Kuro",            "lat":5.55,  "lon":-0.20},
    "ashanti":       {"name":"Ashanti (Kumasi)",             "name_tw":"Ashanti (Kumasi)",       "lat":6.69,  "lon":-1.62},
    "northern":      {"name":"Northern (Tamale)",            "name_tw":"Atifi (Tamale)",         "lat":9.40,  "lon":-0.85},
    "central":       {"name":"Central (Cape Coast)",         "name_tw":"Mfinimfini (Cape Coast)","lat":5.10,  "lon":-1.25},
    "bono":          {"name":"Bono (Sunyani)",               "name_tw":"Bono (Sunyani)",         "lat":7.33,  "lon":-2.33},
    "eastern":       {"name":"Eastern (Koforidua)",          "name_tw":"Apuei (Koforidua)",      "lat":6.09,  "lon":-0.26},
    "volta":         {"name":"Volta (Ho)",                   "name_tw":"Volta (Ho)",             "lat":6.60,  "lon": 0.47},
    "upper_west":    {"name":"Upper West (Wa)",              "name_tw":"Atifi Atɔeɛ (Wa)",      "lat":10.06, "lon":-2.50},
    "upper_east":    {"name":"Upper East (Bolgatanga)",      "name_tw":"Atifi Apuei (Bolgatanga)","lat":10.79,"lon":-0.85},
    "western":       {"name":"Western (Takoradi)",           "name_tw":"Atɔeɛ (Takoradi)",      "lat":4.90,  "lon":-1.76},
    "oti":           {"name":"Oti (Dambai)",                 "name_tw":"Oti (Dambai)",           "lat":7.97,  "lon": 0.18},
    "bono_east":     {"name":"Bono East (Techiman)",         "name_tw":"Bono Apuei (Techiman)",  "lat":7.59,  "lon":-1.94},
    "ahafo":         {"name":"Ahafo (Goaso)",                "name_tw":"Ahafo (Goaso)",          "lat":6.80,  "lon":-2.52},
    "western_north": {"name":"Western North (Sefwi Wiawso)","name_tw":"Atɔeɛ Atifi (Sefwi)",   "lat":6.20,  "lon":-2.47},
    "north_east":    {"name":"North East (Nalerigu)",        "name_tw":"Atifi Apuei (Nalerigu)", "lat":10.52, "lon":-0.36},
    "savannah":      {"name":"Savannah (Damongo)",           "name_tw":"Savannah (Damongo)",     "lat":9.08,  "lon":-1.82},
}

# ---------------------------------------------------------------------------
# Weather
# ---------------------------------------------------------------------------
_weather_cache = {}

FALLBACK_W = {
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
    "advice":"Weather data temporarily unavailable.",
    "advice_tw":"Ɔhaw ho nsɛm nni hɔ.",
    "advice_livestock":"Check on your animals and ensure they have water.",
    "advice_livestock_tw":"Hwɛ wo mmoa na ma wɔn nsuo.",
    "risk":"Data unavailable","risk_tw":"Nsɛm nni hɔ","risk_level":"warn","live":False,
}

def get_weather(region_key="greater_accra"):
    region = GHANA_REGIONS.get(region_key, GHANA_REGIONS["greater_accra"])
    cached = _weather_cache.get(region_key)
    if cached and time.time()-cached["ts"]<1800: return cached["data"]
    try:
        url=(f"https://api.open-meteo.com/v1/forecast"
             f"?latitude={region['lat']}&longitude={region['lon']}"
             f"&daily=temperature_2m_max,precipitation_probability_max,windspeed_10m_max"
             f"&hourly=relativehumidity_2m&forecast_days=7&timezone=Africa%2FAccra")
        r=requests.get(url,timeout=8); r.raise_for_status()
        d=r.json(); daily=d["daily"]
        days=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        icon=lambda p:"☀️" if p<30 else("⛅" if p<60 else "🌧️")
        forecast=[{"day":days[i%7],"icon":icon(daily["precipitation_probability_max"][i]),
                   "high":round(daily["temperature_2m_max"][i]),
                   "rain":daily["precipitation_probability_max"][i]}
                  for i in range(min(7,len(daily["temperature_2m_max"])))]
        rain=daily["precipitation_probability_max"][0]
        high=round(daily["temperature_2m_max"][0])
        if rain>=70:
            adv=f"Heavy rain in {region['name']}. Avoid pesticides. Check drainage."
            adv_tw=f"Osu kɛseɛ reba wɔ {region['name_tw']}."
            adv_live="Heavy rain coming. Move animals to shelter. Check for flooding in pens."
            adv_live_tw="Osu kɛseɛ reba. Fa wo mmoa kɔ fie mu."
            risk,risk_tw,rl="High flood risk","Osu kɛseɛ tumi de ɔhaw","danger"
        elif rain>=40:
            adv=f"Moderate rain in {region['name']}. Skip irrigation on rainy days."
            adv_tw=f"Osu kakra reba wɔ {region['name_tw']}."
            adv_live="Moderate rain expected. Good weather for animals. Ensure pens have drainage."
            adv_live_tw="Osu kakra reba. Mmoa bɔkɔɔ. Hwɛ sɛ nsuo tumi afi fie mu."
            risk,risk_tw,rl="Moderate moisture risk","Nsuo haw mfinimfini","warn"
        else:
            adv=f"Dry in {region['name']}. Irrigate crops well."
            adv_tw=f"Awia bɛyɛ den wɔ {region['name_tw']}."
            adv_live="Hot dry conditions. Ensure animals have plenty of clean water and shade."
            adv_live_tw="Awia bɛyɛ den. Ma wo mmoa nsuo pa na ɔha."
            if high>=35:
                adv_live="Very hot today. Check on poultry frequently — heat stress is dangerous. Increase ventilation."
            risk,risk_tw,rl="Low rainfall — monitor moisture","Osu ketewa","ok"
        result={
            "location":region["name"],"location_tw":region["name_tw"],
            "today_high":high,"humidity":d["hourly"]["relativehumidity_2m"][12],
            "rain_chance":rain,"wind_kmh":round(daily["windspeed_10m_max"][0]),
            "forecast":forecast,"advice":adv,"advice_tw":adv_tw,
            "advice_livestock":adv_live,"advice_livestock_tw":adv_live_tw,
            "risk":risk,"risk_tw":risk_tw,"risk_level":rl,"live":True,
        }
        _weather_cache[region_key]={"ts":time.time(),"data":result}
        return result
    except Exception as e:
        print(f"[Weather error] {e}")
        if cached: return cached["data"]
        fb=dict(FALLBACK_W); fb["location"]=region["name"]; fb["location_tw"]=region["name_tw"]
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

LIVESTOCK_PRICES = [
    {"animal":"Live chicken","animal_tw":"Akoko","unit":"per kg",    "price":35, "change":5,  "trend":"up"},
    {"animal":"Eggs",        "animal_tw":"Nkosua","unit":"tray of 30","price":85,"change":3,  "trend":"up"},
    {"animal":"Cattle",      "animal_tw":"Nanka","unit":"per head",  "price":4500,"change":0, "trend":"stable"},
    {"animal":"Goat",        "animal_tw":"Abirekyi","unit":"per head","price":420,"change":8, "trend":"up"},
    {"animal":"Sheep",       "animal_tw":"Odwan","unit":"per head",  "price":380,"change":4,  "trend":"up"},
    {"animal":"Pig",         "animal_tw":"Prako","unit":"per head",  "price":900,"change":-3, "trend":"down"},
    {"animal":"Tilapia",     "animal_tw":"Nsuo Mmoa","unit":"per kg","price":28, "change":6,  "trend":"up"},
    {"animal":"Catfish",     "animal_tw":"Nsuo Mmoa","unit":"per kg","price":32, "change":2,  "trend":"up"},
]

PEST_DATA = [
    {"name":"Fall Armyworm","name_tw":"Aburo Mmoa","crops":"Maize","crops_tw":"Aburo",
     "description":"High risk season. Check leaves for feeding damage.","description_tw":"Ɔberɛ kɛseɛ.",
     "level":"high","tip":"Apply neem-based spray early morning.","tip_tw":"De neem aduro gu so anɔpa."},
    {"name":"Cassava Leaf Blight","name_tw":"Bankye Nkotokuo Yareɛ","crops":"Cassava","crops_tw":"Bankye",
     "description":"Humidity-driven disease. Brown spots on leaves.","description_tw":"Yareɛ a nsuo ɛma aba.",
     "level":"medium","tip":"Remove infected leaves. Space plants for airflow.","tip_tw":"Yi nkotokuo a yareɛ wɔ so."},
    {"name":"Newcastle Disease","name_tw":"Akoko Yareɛ","crops":"Poultry","crops_tw":"Akoko",
     "description":"Highly contagious poultry disease. Sneezing, twisted neck, sudden death.",
     "description_tw":"Akoko yareɛ kɛseɛ. Hwɛ wo akoko.",
     "level":"high","tip":"Vaccinate immediately. Isolate sick birds. Call your vet.","tip_tw":"Kari aduro ntɛm. Fa yareɛ akoko fi."},
    {"name":"Aphids","name_tw":"Mmoa Ketewa","crops":"Vegetables","crops_tw":"Atosɔde",
     "description":"Low pressure this week.","description_tw":"Ɔhaw ketewa.",
     "level":"low","tip":"Spray with diluted soapy water.","tip_tw":"De nsuo ne sapo ngu so."},
]

QUICK_EN = [
    "What crop should I plant in Ghana during the minor rainy season?",
    "How do I treat Fall Armyworm on my maize with local methods?",
    "When is the best time to sell my tomatoes?",
    "My chickens are not eating — what is wrong?",
    "What fertilizer should I use for maize farming in Ghana?",
    "How can I store my harvest longer without refrigeration?",
]

QUICK_TW = [
    "Aba bɛn na mɛdua wɔ Ghana wɔ osu ketewa ɔberɛ?",
    "Ɛdeɛn na mɛyɛ aburo mmoa ho?",
    "Ɛberɛ bɛn na ɛsɛ sɛ metɔn me ntomato?",
    "Me akoko nni aduan — ɛdeɛn na asi?",
    "Aduro bɛn na mɛfa ama aburo?",
    "Ɛdeɛn na mɛtumi de me aba sie akɔ akyiri?",
]

# ---------------------------------------------------------------------------
# Main route
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    sid  = get_sid()
    user = get_or_create_user(sid)
    return render_template("index.html",
        weather        = get_weather(user.get("region","greater_accra")),
        prices         = PRICE_DATA,
        livestock_prices = LIVESTOCK_PRICES,
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

@app.route("/api/weather")
def api_weather():
    key=request.args.get("region","greater_accra")
    if key not in GHANA_REGIONS: key="greater_accra"
    sid=get_sid()
    with get_db() as db:
        db.execute("UPDATE users SET region=? WHERE session_id=?",(key,sid)); db.commit()
    return jsonify(get_weather(key))

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def api_chat():
    sid=get_sid(); user=get_or_create_user(sid)
    data=request.get_json(); messages=data.get("messages",[]); lang=data.get("lang","en")
    if not messages: return jsonify({"error":"No messages"}),400
    if not client.api_key: return jsonify({"error":"ANTHROPIC_API_KEY not set"}),500
    if not is_pro(user):
        used=get_usage_today(sid)
        if used>=FREE_DAILY_LIMIT:
            return jsonify({"error":"limit_reached",
                "message":f"You have used all {FREE_DAILY_LIMIT} free questions today. Upgrade to Pro.",
                "message_tw":f"Woafa wo nsɛmmisa {FREE_DAILY_LIMIT} nyinaa nnɛ.",
                "used":used,"limit":FREE_DAILY_LIMIT}),429
    log_usage(sid,"chat",messages[-1].get("content",""))
    system=SYSTEM_TW if lang=="tw" else SYSTEM_EN
    def generate():
        with client.messages.stream(model="claude-sonnet-4-20250514",max_tokens=1024,
                system=system,messages=messages) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps({'text':text})}\n\n"
        yield "data: [DONE]\n\n"
    return Response(stream_with_context(generate()),mimetype="text/event-stream")

# ---------------------------------------------------------------------------
# Diagnose (crops and animals)
# ---------------------------------------------------------------------------
@app.route("/api/diagnose", methods=["POST"])
def api_diagnose():
    sid=get_sid(); user=get_or_create_user(sid)
    if not client.api_key: return jsonify({"error":"ANTHROPIC_API_KEY not set"}),500
    if not is_pro(user):
        used=get_diagnose_month(sid)
        if used>=FREE_DIAGNOSE_LIMIT:
            return jsonify({"error":"limit_reached",
                "message":f"You have used all {FREE_DIAGNOSE_LIMIT} free diagnoses. Upgrade to Pro."}),429
    data=request.get_json()
    image_b64=data.get("image"); media_type=data.get("media_type","image/jpeg")
    lang=data.get("lang","en"); mode=data.get("mode","crop")
    if not image_b64: return jsonify({"error":"No image"}),400
    log_usage(sid,"diagnose",f"{mode} photo diagnosis")
    if mode=="animal":
        prompt=DISEASE_ANIMAL_TW if lang=="tw" else DISEASE_ANIMAL_EN
    else:
        prompt=DISEASE_CROP_TW if lang=="tw" else DISEASE_CROP_EN
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
# Farm profile
# ---------------------------------------------------------------------------
@app.route("/api/profile", methods=["GET"])
def api_get_profile():
    sid=get_sid()
    with get_db() as db:
        p=db.execute("SELECT * FROM farm_profiles WHERE session_id=?",(sid,)).fetchone()
    return jsonify(dict(p) if p else {})

@app.route("/api/profile", methods=["POST"])
def api_save_profile():
    sid=get_sid(); data=request.get_json()
    ghana_card=data.get("ghana_card","").strip().upper()
    ghana_card_valid=1 if (ghana_card and re.match(r'^GHA-\d{9}-\d$',ghana_card)) else 0
    with get_db() as db:
        db.execute("""
            INSERT INTO farm_profiles
                (session_id,farmer_name,phone,farm_size,farm_unit,crops,crop_type,
                 ghana_card,ghana_card_valid,soil_type,water_source,region,email,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                farmer_name=excluded.farmer_name,
                phone=excluded.phone,
                farm_size=excluded.farm_size,
                farm_unit=excluded.farm_unit,
                crops=excluded.crops,
                crop_type=excluded.crop_type,
                ghana_card=excluded.ghana_card,
                ghana_card_valid=excluded.ghana_card_valid,
                soil_type=excluded.soil_type,
                water_source=excluded.water_source,
                region=excluded.region,
                email=excluded.email,
                updated_at=datetime('now')
        """,(sid,
             data.get("farmer_name",""),data.get("phone",""),
             data.get("farm_size",""),data.get("farm_unit","acres"),
             data.get("crops",""),data.get("crop_type","staples"),
             ghana_card,ghana_card_valid,
             data.get("soil_type",""),data.get("water_source",""),
             data.get("region","greater_accra"),data.get("email","")
        ))
        db.commit()
    return jsonify({"success":True,"ghana_card_valid":bool(ghana_card_valid)})

# ---------------------------------------------------------------------------
# Livestock profile
# ---------------------------------------------------------------------------
@app.route("/api/livestock", methods=["GET"])
def api_get_livestock():
    sid=get_sid()
    with get_db() as db:
        rows=db.execute("SELECT * FROM livestock_profiles WHERE session_id=?",(sid,)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/livestock", methods=["POST"])
def api_save_livestock():
    sid=get_sid(); data=request.get_json()
    animal_type=data.get("animal_type","")
    if not animal_type: return jsonify({"error":"animal_type required"}),400
    ghana_card=data.get("ghana_card","").strip().upper()
    ghana_card_valid=1 if (ghana_card and re.match(r'^GHA-\d{9}-\d$',ghana_card)) else 0
    with get_db() as db:
        db.execute("""
            INSERT INTO livestock_profiles
                (session_id,farmer_name,phone,ghana_card,ghana_card_valid,
                 region,email,animal_type,total_count,sick_count,
                 housing_type,purpose,feed_source,water_source,nearest_vet,notes,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(session_id) DO UPDATE SET
                farmer_name=excluded.farmer_name,
                phone=excluded.phone,
                ghana_card=excluded.ghana_card,
                ghana_card_valid=excluded.ghana_card_valid,
                region=excluded.region,
                email=excluded.email,
                animal_type=excluded.animal_type,
                total_count=excluded.total_count,
                sick_count=excluded.sick_count,
                housing_type=excluded.housing_type,
                purpose=excluded.purpose,
                feed_source=excluded.feed_source,
                water_source=excluded.water_source,
                nearest_vet=excluded.nearest_vet,
                notes=excluded.notes,
                updated_at=datetime('now')
        """,(sid,
             data.get("farmer_name",""),data.get("phone",""),
             ghana_card,ghana_card_valid,
             data.get("region","greater_accra"),data.get("email",""),
             animal_type,
             data.get("total_count",0),data.get("sick_count",0),
             data.get("housing_type",""),data.get("purpose",""),
             data.get("feed_source",""),data.get("water_source",""),
             data.get("nearest_vet",""),data.get("notes","")
        ))
        db.commit()
    return jsonify({"success":True})

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
    return jsonify({"success":True})

@app.route("/api/yearplan/<int:plan_id>", methods=["DELETE"])
def api_delete_yearplan(plan_id):
    sid=get_sid()
    with get_db() as db:
        db.execute("DELETE FROM year_plan WHERE id=? AND session_id=?",(plan_id,sid))
        db.commit()
    return jsonify({"success":True})

@app.route("/api/yearplan/<int:plan_id>/status", methods=["POST"])
def api_update_yearplan_status(plan_id):
    sid=get_sid(); data=request.get_json()
    status=data.get("status","pending")
    with get_db() as db:
        db.execute("UPDATE year_plan SET status=? WHERE id=? AND session_id=?",(status,plan_id,sid))
        db.commit()
    return jsonify({"success":True})

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
@app.route("/api/usage")
def api_usage():
    sid=get_sid(); user=get_or_create_user(sid)
    return jsonify({"plan":user["plan"],"used_today":get_usage_today(sid),
                    "used_diagnose":get_diagnose_month(sid),
                    "free_limit":FREE_DAILY_LIMIT,"diagnose_limit":FREE_DIAGNOSE_LIMIT})

# ---------------------------------------------------------------------------
# Paystack
# ---------------------------------------------------------------------------
@app.route("/api/pay/init", methods=["POST"])
def pay_init():
    sid=get_sid(); data=request.get_json()
    email=data.get("email","").strip()
    if not PAYSTACK_SECRET: return jsonify({"error":"Paystack not configured"}),500
    if not email or "@" not in email: return jsonify({"error":"Valid email required"}),400
    with get_db() as db:
        db.execute("UPDATE users SET email=? WHERE session_id=?",(email,sid)); db.commit()
    resp=requests.post("https://api.paystack.co/transaction/initialize",
        headers={"Authorization":f"Bearer {PAYSTACK_SECRET}","Content-Type":"application/json"},
        json={"email":email,"amount":3000,"currency":"GHS",
              "callback_url":request.host_url+"pay/verify","metadata":{"session_id":sid}})
    result=resp.json()
    if result.get("status"):
        ref=result["data"]["reference"]
        with get_db() as db:
            db.execute("INSERT OR REPLACE INTO payments (session_id,reference,amount,status) VALUES (?,?,3000,'pending')",(sid,ref))
            db.commit()
        return jsonify({"authorization_url":result["data"]["authorization_url"],"reference":ref})
    return jsonify({"error":result.get("message","Failed")}),400

@app.route("/pay/verify")
def pay_verify():
    ref=request.args.get("reference","")
    if not ref or not PAYSTACK_SECRET: return redirect("/?payment=failed")
    resp=requests.get(f"https://api.paystack.co/transaction/verify/{ref}",
        headers={"Authorization":f"Bearer {PAYSTACK_SECRET}"})
    result=resp.json()
    if result.get("status") and result["data"]["status"]=="success":
        sid=result["data"].get("metadata",{}).get("session_id",get_sid())
        with get_db() as db:
            db.execute("UPDATE payments SET status='success' WHERE reference=?",(ref,))
            db.execute("UPDATE users SET plan='pro',pro_since=datetime('now'),paystack_ref=? WHERE session_id=?",(ref,sid))
            db.commit()
        session["sid"]=sid; return redirect("/?payment=success")
    return redirect("/?payment=failed")

# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@app.route("/admin",methods=["GET","POST"])
def admin():
    if request.method=="POST":
        if request.form.get("password")==ADMIN_PASSWORD: session["admin"]=True
        else: return render_template("admin_login.html",error="Wrong password")
    if not session.get("admin"): return render_template("admin_login.html",error=None)
    try:
        with get_db() as db:
            total        = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            pro          = db.execute("SELECT COUNT(*) FROM users WHERE plan='pro'").fetchone()[0]
            q_today      = db.execute("SELECT COUNT(*) FROM usage WHERE type='chat' AND date(created_at)=date('now')").fetchone()[0]
            d_today      = db.execute("SELECT COUNT(*) FROM usage WHERE type='diagnose' AND date(created_at)=date('now')").fetchone()[0]
            voice_today  = db.execute("SELECT COUNT(*) FROM usage WHERE type='voice' AND date(created_at)=date('now')").fetchone()[0]
            top_q        = db.execute("SELECT question,COUNT(*) cnt FROM usage WHERE type='chat' AND question IS NOT NULL GROUP BY question ORDER BY cnt DESC LIMIT 10").fetchall()
            users        = db.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
            region_stats = db.execute("SELECT region,COUNT(*) cnt FROM users GROUP BY region ORDER BY cnt DESC").fetchall()
            # Farm profiles — safe fallback if table/columns missing
            try:
                profiles      = db.execute("SELECT COUNT(*) FROM farm_profiles").fetchone()[0]
                verified      = db.execute("SELECT COUNT(*) FROM farm_profiles WHERE ghana_card_valid=1").fetchone()[0]
                avg_fs_row    = db.execute("SELECT AVG(CAST(farm_size AS REAL)) FROM farm_profiles WHERE farm_size IS NOT NULL AND farm_size!=''").fetchone()[0]
                avg_farm_size = round(avg_fs_row,1) if avg_fs_row else 0
            except Exception:
                profiles=0; verified=0; avg_farm_size=0
            # Livestock profiles — safe fallback
            try:
                livestock_cnt      = db.execute("SELECT COUNT(*) FROM livestock_profiles").fetchone()[0]
                sick_animals       = db.execute("SELECT SUM(sick_count) FROM livestock_profiles").fetchone()[0] or 0
                animal_stats       = db.execute("SELECT animal_type,COUNT(*) cnt FROM livestock_profiles WHERE animal_type IS NOT NULL AND animal_type!='profile' GROUP BY animal_type ORDER BY cnt DESC").fetchall()
                try:
                    livestock_verified = db.execute("SELECT COUNT(*) FROM livestock_profiles WHERE ghana_card_valid=1").fetchone()[0]
                except Exception:
                    livestock_verified = 0
            except Exception:
                livestock_cnt=0; sick_animals=0; animal_stats=[]; livestock_verified=0
    except Exception as e:
        return f"<h2>Admin Error</h2><pre>{e}</pre><p><a href='/admin/logout'>Sign out</a></p>", 500
    return render_template("admin.html",
        total_users=total, pro_users=pro, total_revenue=pro*30,
        questions_today=q_today, diagnoses_today=d_today, voice_today=voice_today,
        top_questions=top_q, recent_users=users, region_stats=region_stats,
        regions=GHANA_REGIONS, profiles=profiles, verified=verified,
        avg_farm_size=avg_farm_size,
        livestock_cnt=livestock_cnt, livestock_verified=livestock_verified,
        sick_animals=sick_animals, animal_stats=animal_stats)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin",None); return redirect("/admin")

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🌿 Nkɔsoɔ AI v2.0 starting...")
    print("   Crops + Livestock + Aquaculture")
    print("   API key:", bool(client.api_key))
    print("   Open: http://localhost:5000\n")
    app.run(debug=True, port=5000)
