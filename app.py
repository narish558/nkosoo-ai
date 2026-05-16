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

import os, json, time, re, sqlite3, requests, anthropic, csv, io, hashlib
from flask import (Flask, render_template, request, jsonify,
                   stream_with_context, Response, session, redirect, make_response)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nkosoo-secret-2024")

client          = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY",""))
PAYSTACK_SECRET = os.environ.get("PAYSTACK_SECRET_KEY","")
PAYSTACK_PUBLIC = os.environ.get("PAYSTACK_PUBLIC_KEY","")
ADMIN_PASSWORD  = os.environ.get("ADMIN_PASSWORD","nkosoo2024")

FREE_DAILY_LIMIT    = 3
FREE_DIAGNOSE_LIMIT = 1

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
            name TEXT, phone TEXT, email TEXT,
            password_hash TEXT,
            plan TEXT DEFAULT 'free',
            region TEXT DEFAULT 'greater_accra',
            lang TEXT DEFAULT 'en',
            registered INTEGER DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS price_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS agronomists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            speciality TEXT,
            region TEXT,
            qualifications TEXT,
            fee REAL DEFAULT 0,
            consult_type TEXT DEFAULT 'call',
            ghana_card TEXT,
            ghana_card_valid INTEGER DEFAULT 0,
            rating REAL DEFAULT 5.0,
            consult_count INTEGER DEFAULT 0,
            verified INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)
        # Migrate users table — add new columns if missing
        u_cols = [r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()]
        for col, defn in [
            ("name","TEXT"), ("phone","TEXT"),
            ("password_hash","TEXT"), ("registered","INTEGER DEFAULT 0"),
        ]:
            if col not in u_cols:
                try: db.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")
                except: pass
        db.commit()
        fp_cols = [r[1] for r in db.execute("PRAGMA table_info(farm_profiles)").fetchall()]
        old_fp_cols = ['farm_name', 'farming_type', 'nearest_market', 'latitude', 'longitude', 'farm_address']
        has_old_fp = any(c in fp_cols for c in old_fp_cols)
        missing_fp = any(c not in fp_cols for c in ['farm_unit','crop_type','region','email'])
        if has_old_fp or missing_fp:
            # Build INSERT only from columns that exist in the old table
            safe_cols = [c for c in ['session_id','farmer_name','phone','farm_size','crops','ghana_card','ghana_card_valid','soil_type','water_source'] if c in fp_cols]
            cols_str  = ','.join(safe_cols)
            db.executescript(f"""
                CREATE TABLE IF NOT EXISTS farm_profiles_new (
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
                INSERT OR IGNORE INTO farm_profiles_new ({cols_str})
                SELECT {cols_str} FROM farm_profiles;
                DROP TABLE farm_profiles;
                ALTER TABLE farm_profiles_new RENAME TO farm_profiles;
            """)
        else:
            for col, defn in [
                ("farm_unit","TEXT"),("crop_type","TEXT"),
                ("region","TEXT"),("email","TEXT"),
            ]:
                if col not in fp_cols:
                    try: db.execute(f"ALTER TABLE farm_profiles ADD COLUMN {col} {defn}")
                    except: pass

        # Migrate livestock_profiles — check if old UNIQUE(session_id,animal_type) exists
        # If so, recreate with UNIQUE(session_id) only
        live_info = db.execute("PRAGMA index_list(livestock_profiles)").fetchall()
        live_cols = [r[1] for r in db.execute("PRAGMA table_info(livestock_profiles)").fetchall()]
        has_old_unique = any('animal_type' in str(i) for i in live_info)
        # Also check if new columns exist
        needs_new_cols = any(c not in live_cols for c in ['farmer_name','region','email'])
        if has_old_unique or needs_new_cols:
            # Recreate table with correct schema
            db.executescript("""
                CREATE TABLE IF NOT EXISTS livestock_profiles_new (
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
                INSERT OR IGNORE INTO livestock_profiles_new
                    (session_id,animal_type,total_count,sick_count,
                     housing_type,purpose,feed_source,water_source,nearest_vet,notes)
                SELECT session_id,animal_type,total_count,sick_count,
                       housing_type,purpose,feed_source,water_source,nearest_vet,notes
                FROM livestock_profiles;
                DROP TABLE livestock_profiles;
                ALTER TABLE livestock_profiles_new RENAME TO livestock_profiles;
            """)
        else:
            for col, defn in [
                ("farmer_name","TEXT"),("phone","TEXT"),
                ("ghana_card","TEXT"),("ghana_card_valid","INTEGER DEFAULT 0"),
                ("region","TEXT"),("email","TEXT"),
            ]:
                if col not in live_cols:
                    try: db.execute(f"ALTER TABLE livestock_profiles ADD COLUMN {col} {defn}")
                    except: pass
        db.commit()


init_db()

# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def get_sid():
    if "sid" not in session:
        import uuid; session["sid"] = str(uuid.uuid4())
    return session["sid"]

def hash_password(pw):
    return hashlib.sha256(pw.strip().encode()).hexdigest()

def is_registered():
    """Check if current session user is registered."""
    if session.get("registered"):
        return True
    sid = get_sid()
    try:
        with get_db() as db:
            u = db.execute("SELECT registered FROM users WHERE session_id=?",(sid,)).fetchone()
            return bool(u and u["registered"] == 1)
    except:
        return False

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

def is_pro(user): return (user["plan"] if user else "free") == "pro"

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
# Live market prices — AI-refreshed weekly, cached in DB
# ---------------------------------------------------------------------------
_price_cache = {"ts": 0, "data": None}
PRICE_REFRESH_HOURS = 168  # refresh weekly

PRICE_FALLBACK_CROPS = [
    {"crop":"Maize",    "crop_tw":"Aburo",   "unit":"50kg bag",   "price":240,"change":8,  "trend":"up"},
    {"crop":"Tomatoes", "crop_tw":"Ntomato", "unit":"crate",      "price":180,"change":-12,"trend":"down"},
    {"crop":"Cassava",  "crop_tw":"Bankye",  "unit":"100kg bag",  "price":310,"change":0,  "trend":"stable"},
    {"crop":"Plantain", "crop_tw":"Ɔgede",   "unit":"bunch",      "price":55, "change":3,  "trend":"up"},
    {"crop":"Yam",      "crop_tw":"Bayerɛ",  "unit":"tuber (lg)", "price":35, "change":5,  "trend":"up"},
    {"crop":"Cocoa",    "crop_tw":"Kookoo",  "unit":"kg dry bean","price":24, "change":2,  "trend":"up"},
    {"crop":"Pepper",   "crop_tw":"Mako",    "unit":"kg",         "price":28, "change":-5, "trend":"down"},
    {"crop":"Groundnut","crop_tw":"Nkatie",  "unit":"50kg bag",   "price":290,"change":1,  "trend":"stable"},
]

PRICE_FALLBACK_LIVESTOCK = [
    {"animal":"Live chicken","animal_tw":"Akoko",    "unit":"per kg",    "price":35,  "change":5,  "trend":"up"},
    {"animal":"Eggs",        "animal_tw":"Nkosua",   "unit":"tray of 30","price":85,  "change":3,  "trend":"up"},
    {"animal":"Cattle",      "animal_tw":"Nanka",    "unit":"per head",  "price":4500,"change":0,  "trend":"stable"},
    {"animal":"Goat",        "animal_tw":"Abirekyi", "unit":"per head",  "price":420, "change":8,  "trend":"up"},
    {"animal":"Sheep",       "animal_tw":"Odwan",    "unit":"per head",  "price":380, "change":4,  "trend":"up"},
    {"animal":"Pig",         "animal_tw":"Prako",    "unit":"per head",  "price":900, "change":-3, "trend":"down"},
    {"animal":"Tilapia",     "animal_tw":"Nsuo Mmoa","unit":"per kg",    "price":28,  "change":6,  "trend":"up"},
    {"animal":"Catfish",     "animal_tw":"Nsuo Mmoa","unit":"per kg",    "price":32,  "change":2,  "trend":"up"},
]

def fetch_live_prices():
    """Use Claude AI to generate current GHS market prices based on season and market trends."""
    import datetime
    month = datetime.datetime.now().month
    season = "major rainy season" if month in [4,5,6,7] else ("minor rainy season" if month in [9,10] else "dry season/harmattan")
    prompt = f"""You are a Ghana agricultural market analyst. Today is {datetime.datetime.now().strftime('%B %Y')}, {season} in Ghana.

Generate CURRENT realistic GHS wholesale market prices for these crops at Kumasi Kejetia and Accra Agbogbloshie markets.
Return ONLY valid JSON, no markdown, no explanation.

Format exactly:
{{
  "crops": [
    {{"crop": "Maize", "crop_tw": "Aburo", "unit": "50kg bag", "price": 260, "change": 5, "trend": "up"}},
    {{"crop": "Tomatoes", "crop_tw": "Ntomato", "unit": "crate", "price": 195, "change": -8, "trend": "down"}},
    {{"crop": "Cassava", "crop_tw": "Bankye", "unit": "100kg bag", "price": 320, "change": 0, "trend": "stable"}},
    {{"crop": "Plantain", "crop_tw": "Ɔgede", "unit": "bunch", "price": 60, "change": 3, "trend": "up"}},
    {{"crop": "Yam", "crop_tw": "Bayerɛ", "unit": "tuber (lg)", "price": 38, "change": 2, "trend": "up"}},
    {{"crop": "Cocoa", "crop_tw": "Kookoo", "unit": "kg dry bean", "price": 26, "change": 1, "trend": "up"}},
    {{"crop": "Pepper", "crop_tw": "Mako", "unit": "kg", "price": 30, "change": -3, "trend": "down"}},
    {{"crop": "Groundnut", "crop_tw": "Nkatie", "unit": "50kg bag", "price": 295, "change": 2, "trend": "stable"}}
  ],
  "livestock": [
    {{"animal": "Live chicken", "animal_tw": "Akoko", "unit": "per kg", "price": 38, "change": 4, "trend": "up"}},
    {{"animal": "Eggs", "animal_tw": "Nkosua", "unit": "tray of 30", "price": 90, "change": 5, "trend": "up"}},
    {{"animal": "Cattle", "animal_tw": "Nanka", "unit": "per head", "price": 4800, "change": 0, "trend": "stable"}},
    {{"animal": "Goat", "animal_tw": "Abirekyi", "unit": "per head", "price": 450, "change": 6, "trend": "up"}},
    {{"animal": "Sheep", "animal_tw": "Odwan", "unit": "per head", "price": 400, "change": 3, "trend": "up"}},
    {{"animal": "Pig", "animal_tw": "Prako", "unit": "per head", "price": 950, "change": -2, "trend": "down"}},
    {{"animal": "Tilapia", "animal_tw": "Nsuo Mmoa", "unit": "per kg", "price": 32, "change": 4, "trend": "up"}},
    {{"animal": "Catfish", "animal_tw": "Nsuo Mmoa", "unit": "per kg", "price": 35, "change": 2, "trend": "up"}}
  ],
  "updated": "{datetime.datetime.now().strftime('%d %b %Y')}",
  "season": "{season}",
  "source": "Kumasi Kejetia & Accra Agbogbloshie markets"
}}

Adjust prices realistically for {season}. During rainy season tomatoes are cheaper, maize more expensive. Change is % change from last week. Trend: up/down/stable."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role":"user","content":prompt}]
        )
        text = resp.content[0].text.strip()
        # Clean any markdown fences
        text = text.replace("```json","").replace("```","").strip()
        data = json.loads(text)
        return data
    except Exception as e:
        print(f"[Price fetch error] {e}")
        return None

def get_prices():
    """Return prices from cache or refresh if stale."""
    global _price_cache
    age_hours = (time.time() - _price_cache["ts"]) / 3600
    if _price_cache["data"] and age_hours < PRICE_REFRESH_HOURS:
        return _price_cache["data"]
    # Try to get from DB first
    try:
        with get_db() as db:
            row = db.execute("SELECT data, updated_at FROM price_cache ORDER BY updated_at DESC LIMIT 1").fetchone()
            if row:
                import datetime
                saved = datetime.datetime.fromisoformat(row["updated_at"])
                age = (datetime.datetime.now() - saved).total_seconds() / 3600
                if age < PRICE_REFRESH_HOURS:
                    data = json.loads(row["data"])
                    _price_cache = {"ts": time.time(), "data": data}
                    return data
    except Exception:
        pass
    # Fetch fresh from Claude
    data = fetch_live_prices()
    if data:
        _price_cache = {"ts": time.time(), "data": data}
        try:
            with get_db() as db:
                db.execute("INSERT INTO price_cache (data, updated_at) VALUES (?, datetime('now'))",(json.dumps(data),))
                db.commit()
        except Exception as e:
            print(f"[Price cache save error] {e}")
        return data
    # Fallback to static
    return {"crops": PRICE_FALLBACK_CROPS, "livestock": PRICE_FALLBACK_LIVESTOCK,
            "updated": "Cached", "season": "", "source": "Reference prices"}

# Static fallback aliases for template
PRICE_DATA = PRICE_FALLBACK_CROPS
LIVESTOCK_PRICES = PRICE_FALLBACK_LIVESTOCK

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
    prices_data = get_prices()
    user_registered = session.get("registered", False) or (user["registered"] == 1 if user and "registered" in user.keys() else False)
    user_name = session.get("user_name","") or (user["name"] if user and user["name"] else "")
    return render_template("index.html",
        weather        = get_weather(user["region"] if user and user["region"] else "greater_accra"),
        prices         = prices_data.get("crops", PRICE_FALLBACK_CROPS),
        livestock_prices = prices_data.get("livestock", PRICE_FALLBACK_LIVESTOCK),
        prices_updated = prices_data.get("updated",""),
        prices_source  = prices_data.get("source",""),
        prices_season  = prices_data.get("season",""),
        pests          = PEST_DATA,
        quick_en       = QUICK_EN,
        quick_tw       = QUICK_TW,
        regions        = GHANA_REGIONS,
        user           = user,
        user_registered = user_registered,
        user_name      = user_name,
        used_today     = get_usage_today(sid),
        used_diagnose  = get_diagnose_month(sid),
        free_limit     = FREE_DAILY_LIMIT,
        diagnose_limit = FREE_DIAGNOSE_LIMIT,
        api_ready      = bool(client.api_key),
        paystack_public= PAYSTACK_PUBLIC,
    )

@app.route("/ecosystem")
def ecosystem():
    return render_template("ecosystem.html")

@app.route("/api/prices")
def api_prices():
    """Return live prices — refresh if requested by admin."""
    refresh = request.args.get("refresh") == "1" and session.get("admin")
    if refresh:
        global _price_cache
        _price_cache = {"ts": 0, "data": None}
    data = get_prices()
    return jsonify(data)

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
    if not is_registered():
        return jsonify({"success":False,"error":"Please create a free account to save your farm profile.","gate":"register"}), 401
    sid=get_sid(); data=request.get_json()
    ghana_card=data.get("ghana_card","").strip().upper()
    ghana_card_valid=1 if (ghana_card and re.match(r'^GHA-\d{9}-\d$',ghana_card)) else 0
    try:
        with get_db() as db:
            # Check if row exists
            existing=db.execute("SELECT id FROM farm_profiles WHERE session_id=?",(sid,)).fetchone()
            if existing:
                db.execute("""
                    UPDATE farm_profiles SET
                        farmer_name=?,phone=?,farm_size=?,farm_unit=?,crops=?,crop_type=?,
                        ghana_card=?,ghana_card_valid=?,soil_type=?,water_source=?,
                        region=?,email=?,updated_at=datetime('now')
                    WHERE session_id=?
                """,(data.get("farmer_name",""),data.get("phone",""),
                     data.get("farm_size",""),data.get("farm_unit","acres"),
                     data.get("crops",""),data.get("crop_type","staples"),
                     ghana_card,ghana_card_valid,
                     data.get("soil_type",""),data.get("water_source",""),
                     data.get("region","greater_accra"),data.get("email",""),sid))
            else:
                db.execute("""
                    INSERT INTO farm_profiles
                        (session_id,farmer_name,phone,farm_size,farm_unit,crops,crop_type,
                         ghana_card,ghana_card_valid,soil_type,water_source,region,email)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,(sid,data.get("farmer_name",""),data.get("phone",""),
                     data.get("farm_size",""),data.get("farm_unit","acres"),
                     data.get("crops",""),data.get("crop_type","staples"),
                     ghana_card,ghana_card_valid,
                     data.get("soil_type",""),data.get("water_source",""),
                     data.get("region","greater_accra"),data.get("email","")))
            db.commit()
        return jsonify({"success":True,"ghana_card_valid":bool(ghana_card_valid)})
    except Exception as e:
        print(f"[profile save error] {e}")
        return jsonify({"success":False,"error":str(e)}),500

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
    if not is_registered():
        return jsonify({"success":False,"error":"Please create a free account to save your livestock profile.","gate":"register"}), 401
    sid=get_sid(); data=request.get_json()
    animal_type=data.get("animal_type","")
    if not animal_type: return jsonify({"error":"animal_type required"}),400
    ghana_card=data.get("ghana_card","").strip().upper()
    ghana_card_valid=1 if (ghana_card and re.match(r'^GHA-\d{9}-\d$',ghana_card)) else 0
    try:
        with get_db() as db:
            existing=db.execute("SELECT id FROM livestock_profiles WHERE session_id=?",(sid,)).fetchone()
            if existing:
                db.execute("""
                    UPDATE livestock_profiles SET
                        farmer_name=?,phone=?,ghana_card=?,ghana_card_valid=?,
                        region=?,email=?,animal_type=?,total_count=?,sick_count=?,
                        housing_type=?,purpose=?,feed_source=?,water_source=?,
                        nearest_vet=?,notes=?,updated_at=datetime('now')
                    WHERE session_id=?
                """,(data.get("farmer_name",""),data.get("phone",""),
                     ghana_card,ghana_card_valid,
                     data.get("region","greater_accra"),data.get("email",""),
                     animal_type,
                     data.get("total_count",0),data.get("sick_count",0),
                     data.get("housing_type",""),data.get("purpose",""),
                     data.get("feed_source",""),data.get("water_source",""),
                     data.get("nearest_vet",""),data.get("notes",""),sid))
            else:
                db.execute("""
                    INSERT INTO livestock_profiles
                        (session_id,farmer_name,phone,ghana_card,ghana_card_valid,
                         region,email,animal_type,total_count,sick_count,
                         housing_type,purpose,feed_source,water_source,nearest_vet,notes)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,(sid,data.get("farmer_name",""),data.get("phone",""),
                     ghana_card,ghana_card_valid,
                     data.get("region","greater_accra"),data.get("email",""),
                     animal_type,
                     data.get("total_count",0),data.get("sick_count",0),
                     data.get("housing_type",""),data.get("purpose",""),
                     data.get("feed_source",""),data.get("water_source",""),
                     data.get("nearest_vet",""),data.get("notes","")))
            db.commit()
        return jsonify({"success":True})
    except Exception as e:
        print(f"[livestock save error] {e}")
        return jsonify({"success":False,"error":str(e)}),500

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
            registered   = db.execute("SELECT COUNT(*) FROM users WHERE registered=1").fetchone()[0]
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
            # Agronomist stats
            try:
                agro_total    = db.execute("SELECT COUNT(*) FROM agronomists WHERE active=1").fetchone()[0]
                agro_verified = db.execute("SELECT COUNT(*) FROM agronomists WHERE verified=1 AND active=1").fetchone()[0]
                pending_agros = db.execute("""
                    SELECT id,name,phone,speciality,region,qualifications,fee,consult_type
                    FROM agronomists WHERE verified=0 AND active=1
                    ORDER BY created_at DESC
                """).fetchall()
            except Exception:
                agro_total=0; agro_verified=0; pending_agros=[]
    except Exception as e:
        return f"<h2>Admin Error</h2><pre>{e}</pre><p><a href='/admin/logout'>Sign out</a></p>", 500
    return render_template("admin.html",
        total_users=total, registered_users=registered, pro_users=pro, total_revenue=pro*30,
        questions_today=q_today, diagnoses_today=d_today, voice_today=voice_today,
        top_questions=top_q, recent_users=users, region_stats=region_stats,
        regions=GHANA_REGIONS, profiles=profiles, verified=verified,
        avg_farm_size=avg_farm_size,
        livestock_cnt=livestock_cnt, livestock_verified=livestock_verified,
        sick_animals=sick_animals, animal_stats=animal_stats,
        agro_total=agro_total, agro_verified=agro_verified,
        pending_agros=pending_agros)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin",None); return redirect("/admin")

# ---------------------------------------------------------------------------
# Auth — register / login / logout
# ---------------------------------------------------------------------------
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", regions=GHANA_REGIONS)
    data = request.get_json() or request.form
    name  = (data.get("name","") or "").strip()
    phone = (data.get("phone","") or "").strip()
    region= (data.get("region","") or "greater_accra").strip()
    email = (data.get("email","") or "").strip()
    pw    = (data.get("password","") or "").strip()
    if not name or not phone or not pw:
        return jsonify({"success":False,"error":"Name, phone and password are required."}), 400
    if len(pw) < 6:
        return jsonify({"success":False,"error":"Password must be at least 6 characters."}), 400
    pw_hash = hash_password(pw)
    sid = get_sid()
    try:
        with get_db() as db:
            # Check if phone already registered
            existing = db.execute("SELECT id FROM users WHERE phone=? AND registered=1",(phone,)).fetchone()
            if existing:
                return jsonify({"success":False,"error":"This phone number is already registered. Please log in."}), 409
            cur = db.execute("""
                UPDATE users SET name=?,phone=?,email=?,region=?,
                    password_hash=?,registered=1
                WHERE session_id=?
            """,(name,phone,email,region,pw_hash,sid))
            if cur.rowcount == 0:
                db.execute("""
                    INSERT INTO users (session_id,name,phone,email,region,password_hash,registered)
                    VALUES (?,?,?,?,?,?,1)
                """,(sid,name,phone,email,region,pw_hash))
            db.commit()
        session["registered"] = True
        session["user_name"]   = name
        return jsonify({"success":True,"name":name})
    except Exception as e:
        print(f"[register error] {e}")
        return jsonify({"success":False,"error":"Registration failed. Please try again."}), 500

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    data  = request.get_json() or request.form
    phone = (data.get("phone","") or "").strip()
    pw    = (data.get("password","") or "").strip()
    if not phone or not pw:
        return jsonify({"success":False,"error":"Phone and password are required."}), 400
    pw_hash = hash_password(pw)
    try:
        with get_db() as db:
            u = db.execute("""
                SELECT session_id,name,plan,region
                FROM users WHERE phone=? AND password_hash=? AND registered=1
            """,(phone,pw_hash)).fetchone()
            if not u:
                return jsonify({"success":False,"error":"Incorrect phone or password."}), 401
            # Link this browser session to registered user
            old_sid = u["session_id"]
            new_sid = get_sid()
            if old_sid != new_sid:
                # Merge sessions — update all tables to new sid
                try:
                    db.execute("UPDATE users SET session_id=? WHERE session_id=?",(new_sid,old_sid))
                    for tbl in ["usage","payments","farm_profiles","livestock_profiles","health_logs"]:
                        db.execute(f"UPDATE {tbl} SET session_id=? WHERE session_id=?",(new_sid,old_sid))
                    db.commit()
                except: pass
            session["registered"] = True
            session["user_name"]   = u["name"]
        return jsonify({"success":True,"name":u["name"],"plan":u["plan"]})
    except Exception as e:
        print(f"[login error] {e}")
        return jsonify({"success":False,"error":"Login failed. Please try again."}), 500

@app.route("/logout")
def logout():
    session.pop("registered", None)
    session.pop("user_name", None)
    return redirect("/")

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
@app.route("/api/agronomists", methods=["GET"])
def api_get_agronomists():
    with get_db() as db:
        rows = db.execute("""
            SELECT a.*, r.name as region_name
            FROM agronomists a
            LEFT JOIN (VALUES
                ('greater_accra','Greater Accra'),('ashanti','Ashanti (Kumasi)'),
                ('northern','Northern (Tamale)'),('central','Central (Cape Coast)'),
                ('bono','Bono (Sunyani)'),('eastern','Eastern (Koforidua)'),
                ('volta','Volta (Ho)'),('upper_west','Upper West (Wa)'),
                ('upper_east','Upper East (Bolgatanga)'),('western','Western (Takoradi)'),
                ('oti','Oti (Dambai)'),('bono_east','Bono East (Techiman)'),
                ('ahafo','Ahafo (Goaso)'),('western_north','Western North (Sefwi)'),
                ('north_east','North East (Nalerigu)'),('savannah','Savannah (Damongo)')
            ) AS r(key,name) ON a.region=r.key
            WHERE a.active=1
            ORDER BY a.verified DESC, a.rating DESC, a.created_at DESC
        """).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/agronomists", methods=["POST"])
def api_save_agronomist():
    data = request.get_json()
    name = data.get("name","").strip()
    phone = data.get("phone","").strip()
    if not name or not phone:
        return jsonify({"error":"Name and phone required"}), 400
    ghana_card = data.get("ghana_card","").strip().upper()
    ghana_card_valid = 1 if (ghana_card and re.match(r'^GHA-\d{9}-\d$', ghana_card)) else 0
    try:
        with get_db() as db:
            db.execute("""
                INSERT INTO agronomists
                    (name,phone,speciality,region,qualifications,fee,consult_type,
                     ghana_card,ghana_card_valid,verified,active)
                VALUES (?,?,?,?,?,?,?,?,?,0,1)
            """,(name, phone,
                 data.get("speciality","general"),
                 data.get("region","greater_accra"),
                 data.get("qualifications",""),
                 float(data.get("fee",0) or 0),
                 data.get("consult_type","call"),
                 ghana_card, ghana_card_valid))
            db.commit()
        return jsonify({"success": True})
    except Exception as e:
        print(f"[agronomist save error] {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# --------------------------------------------------------------------------- — CSV downloads
# ---------------------------------------------------------------------------

def require_admin():
    return session.get("admin") == True

@app.route("/admin/agronomists/verify/<int:agro_id>")
def admin_verify_agronomy(agro_id):
    if not session.get("admin"): return redirect("/admin")
    with get_db() as db:
        db.execute("UPDATE agronomists SET verified=1 WHERE id=?",(agro_id,))
        db.commit()
    return redirect("/admin")

@app.route("/admin/export/agronomists")
def export_agronomists():
    if not session.get("admin"): return redirect("/admin")
    with get_db() as db:
        rows = db.execute("""
            SELECT name,phone,speciality,region,qualifications,fee,
                   consult_type,ghana_card,
                   CASE WHEN ghana_card_valid=1 THEN 'Yes' ELSE 'No' END as gc_verified,
                   CASE WHEN verified=1 THEN 'Yes' ELSE 'Pending' END as verified,
                   rating,consult_count,created_at
            FROM agronomists WHERE active=1 ORDER BY created_at DESC
        """).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Name","Phone","Speciality","Region","Qualifications","Fee (GHS)",
                     "Consult type","Ghana Card","GC Verified","Verified","Rating","Consultations","Registered At"])
    for r in rows:
        writer.writerow([r["name"],r["phone"],r["speciality"],
                         GHANA_REGIONS.get(r["region"],{}).get("name",r["region"] or ""),
                         r["qualifications"],r["fee"],r["consult_type"],
                         r["ghana_card"] or "",r["gc_verified"],r["verified"],
                         r["rating"],r["consult_count"],(r["created_at"] or "")[:16]])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment;filename=nkosoo_agronomists.csv"})

@app.route("/admin/export/crop-profiles")
def export_crop_profiles():
    if not require_admin(): return redirect("/admin")
    with get_db() as db:
        rows = db.execute("""
            SELECT farmer_name, phone, ghana_card,
                   CASE WHEN ghana_card_valid=1 THEN 'Yes' ELSE 'No' END as gc_verified,
                   farm_size, farm_unit, crop_type, crops,
                   soil_type, water_source, region, email,
                   created_at, updated_at
            FROM farm_profiles
            ORDER BY created_at DESC
        """).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Full Name","Phone","Ghana Card","GC Verified",
                     "Farm Size","Unit","Crop Type","Crops",
                     "Soil Type","Water Source","Region","Email",
                     "Registered","Last Updated"])
    for row in rows:
        writer.writerow(list(row))
    output.seek(0)
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = "attachment; filename=nkosoo_crop_profiles.csv"
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    return resp

@app.route("/admin/export/livestock-profiles")
def export_livestock_profiles():
    if not require_admin(): return redirect("/admin")
    with get_db() as db:
        rows = db.execute("""
            SELECT farmer_name, phone, ghana_card,
                   CASE WHEN ghana_card_valid=1 THEN 'Yes' ELSE 'No' END as gc_verified,
                   region, email, animal_type,
                   total_count, sick_count,
                   housing_type, purpose, feed_source, water_source,
                   nearest_vet, created_at, updated_at
            FROM livestock_profiles
            WHERE animal_type IS NOT NULL AND animal_type != 'profile'
            ORDER BY created_at DESC
        """).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Full Name","Phone","Ghana Card","GC Verified",
                     "Region","Email","Animal Type",
                     "Total Count","Sick Count",
                     "Housing","Purpose","Feed Source","Water Source",
                     "Nearest Vet","Registered","Last Updated"])
    for row in rows:
        writer.writerow(list(row))
    output.seek(0)
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = "attachment; filename=nkosoo_livestock_profiles.csv"
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    return resp

@app.route("/admin/export/all-farmers")
def export_all_farmers():
    if not require_admin(): return redirect("/admin")
    with get_db() as db:
        rows = db.execute("""
            SELECT
                COALESCE(fp.farmer_name, lp.farmer_name, 'Unknown') as name,
                COALESCE(fp.phone, lp.phone, '') as phone,
                COALESCE(fp.ghana_card, lp.ghana_card, '') as ghana_card,
                CASE WHEN COALESCE(fp.ghana_card_valid, lp.ghana_card_valid, 0)=1
                     THEN 'Yes' ELSE 'No' END as gc_verified,
                COALESCE(fp.region, lp.region, u.region, '') as region,
                COALESCE(fp.email, lp.email, u.email, '') as email,
                CASE WHEN fp.session_id IS NOT NULL THEN 'Crop farmer' ELSE '' END as crop_profile,
                COALESCE(fp.crops,'') as crops,
                CASE WHEN lp.session_id IS NOT NULL THEN 'Yes' ELSE 'No' END as livestock_profile,
                COALESCE(lp.animal_type,'') as animal_type,
                u.plan, u.created_at
            FROM users u
            LEFT JOIN farm_profiles fp ON fp.session_id = u.session_id
            LEFT JOIN livestock_profiles lp ON lp.session_id = u.session_id
            ORDER BY u.created_at DESC
        """).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Full Name","Phone","Ghana Card","GC Verified",
                     "Region","Email","Crop Profile","Crops",
                     "Livestock Profile","Animal Type","Plan","Joined"])
    for row in rows:
        writer.writerow(list(row))
    output.seek(0)
    resp = make_response(output.getvalue())
    resp.headers["Content-Disposition"] = "attachment; filename=nkosoo_all_farmers.csv"
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    return resp

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n🌿 Nkɔsoɔ AI v2.0 starting...")
    print("   Crops + Livestock + Aquaculture")
    print("   API key:", bool(client.api_key))
    print("   Open: http://localhost:5000\n")
    app.run(debug=True, port=5000)
