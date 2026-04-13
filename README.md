# 🌿 Nkɔsoɔ AI
### AI-Powered Farming Assistant for Ghana & West Africa

**Nkɔsoɔ** means *growth* in Twi — and that is exactly what this tool is built for.

Nkɔsoɔ AI helps smallholder farmers in Ghana make better decisions about weather, crop prices, pest management, and farming practices — powered by Claude AI (Anthropic) and built with Python and Flask.

---

## Features

| Feature | Description |
|---|---|
| 🌤 Live weather | Real-time 7-day forecasts for all 16 Ghana regions via Open-Meteo |
| 📈 Market prices | Current GHS prices for maize, cassava, tomatoes, plantain, cocoa and more |
| 🐛 Pest alerts | Active pest and disease alerts with affordable local treatment tips |
| 📷 Photo diagnosis | Upload a photo of a sick crop — AI identifies the disease instantly |
| 🤖 AI farming chat | Ask any farming question in English or Twi — expert answers in seconds |
| 🌍 16 Ghana regions | Weather tailored to every region from Accra to Bolgatanga to Wa |
| 🇬🇭 Twi language | Full interface and AI responses available in Twi |
| 💳 Paystack payments | GHS 30/month Pro plan via MTN MoMo, Telecel Cash, or card |
| 📊 Usage limits | Free users get 5 questions/day — Pro users get unlimited access |
| 🔒 Admin dashboard | Private dashboard at /admin showing users, revenue, and top questions |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Flask |
| AI | Anthropic Claude (claude-sonnet-4) |
| Weather | Open-Meteo API (free, no key needed) |
| Payments | Paystack (Ghana Mobile Money + card) |
| Database | SQLite (built into Python, zero config) |
| Deployment | Render.com |
| Frontend | HTML, CSS, Vanilla JavaScript |

---

## Project Structure

```
nkosoo-ai/
├── app.py                  # Main Flask app — all routes, AI, payments, admin
├── requirements.txt        # Python dependencies
├── Procfile                # Render start command
├── README.md               # This file
└── templates/
    ├── index.html          # Main farmer-facing UI
    ├── admin.html          # Admin dashboard (private)
    └── admin_login.html    # Admin login page
```

---

## Quick Start (Local Development)

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/nkosoo-ai.git
cd nkosoo-ai
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Set environment variables

**Mac / Linux:**
```bash
export ANTHROPIC_API_KEY=sk-ant-your-key-here
export PAYSTACK_SECRET_KEY=sk_test_your-paystack-key
export PAYSTACK_PUBLIC_KEY=pk_test_your-paystack-key
export ADMIN_PASSWORD=your-admin-password
export SECRET_KEY=any-random-string
```

**Windows:**
```bash
set ANTHROPIC_API_KEY=sk-ant-your-key-here
set PAYSTACK_SECRET_KEY=sk_test_your-paystack-key
set PAYSTACK_PUBLIC_KEY=pk_test_your-paystack-key
set ADMIN_PASSWORD=your-admin-password
set SECRET_KEY=any-random-string
```

### 4. Run the app
```bash
python app.py
```

### 5. Open in browser
```
http://localhost:5000
```

---

## Environment Variables

| Variable | Required | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | console.anthropic.com |
| `PAYSTACK_SECRET_KEY` | For payments | paystack.com dashboard → Settings → API Keys |
| `PAYSTACK_PUBLIC_KEY` | For payments | paystack.com dashboard → Settings → API Keys |
| `ADMIN_PASSWORD` | Yes | Choose any password |
| `SECRET_KEY` | Yes | Any random string |

---

## Deploying to Render

### 1. Push code to GitHub
```bash
git init
git add .
git commit -m "Initial commit — Nkɔsoɔ AI"
git remote add origin https://github.com/yourusername/nkosoo-ai.git
git push -u origin main
```

### 2. Create a Render Web Service
1. Go to render.com and sign in with GitHub
2. Click **New → Web Service**
3. Connect your `nkosoo-ai` repository
4. Configure the build settings:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app` |
| Instance Type | Free |

### 3. Add environment variables
In Render → your service → **Environment** tab, add all 5 variables from the table above.

### 4. Deploy
Render auto-deploys every time you push to GitHub. Your app will be live at:
```
https://nkosoo-ai.onrender.com
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Main farmer UI |
| GET | `/api/weather?region=ashanti` | Live weather for a Ghana region |
| GET | `/api/prices` | Current crop market prices |
| GET | `/api/pests` | Active pest alerts |
| POST | `/api/chat` | AI farming chat (streaming) |
| POST | `/api/diagnose` | Crop photo diagnosis (streaming) |
| GET | `/api/usage` | Current user usage stats |
| POST | `/api/pay/init` | Initialise Paystack payment |
| GET | `/pay/verify` | Paystack payment callback |
| GET/POST | `/admin` | Admin dashboard (password protected) |
| GET | `/admin/logout` | Admin sign out |

### Ghana region keys for `/api/weather`

Pass `?region=` with any of these values:

```
greater_accra  ashanti       northern      central
bono           eastern       volta         upper_west
upper_east     western       oti           bono_east
ahafo          western_north north_east    savannah
```

---

## Usage Limits

| Plan | AI questions | Photo diagnoses | Price |
|---|---|---|---|
| Free | 5 per day | 3 per month | GHS 0 |
| Pro | Unlimited | Unlimited | GHS 30/month |

Pro payments are processed by Paystack and support:
- MTN Mobile Money
- Telecel Cash
- Visa / Mastercard

---

## Admin Dashboard

Visit `/admin` and enter your `ADMIN_PASSWORD` to see:

- Total farmers registered
- Pro subscribers
- Monthly revenue (GHS)
- Questions and diagnoses today
- Top 10 farming questions asked
- Recent farmer signups with region
- Farmer count by all 16 Ghana regions

---

## Updating Market Prices

Prices are updated manually. To update them:

1. Open `app.py` on GitHub
2. Find the `PRICE_DATA` list near the top
3. Update `price`, `change`, and `trend` for each crop
4. Commit the change — Render redeploys automatically

Check current Ghana prices from:
- MoFA Ghana — mofa.gov.gh
- Esoko Ghana — esoko.com
- Kumasi Central Market (call local vendors weekly)

---

## Weather Caching

Weather data is cached for 30 minutes per region to avoid hitting Open-Meteo rate limits. If the API is temporarily unavailable, the app serves the last cached data instead of showing an error.

---

## Roadmap

- [ ] WhatsApp integration (Meta Cloud API)
- [ ] Voice interface in English, Twi, and Dagbani
- [ ] Offline mode with service workers
- [ ] Satellite field monitoring (Sentinel Hub NDVI)
- [ ] Farmer profile and crop history
- [ ] Planting calendar by region and crop
- [ ] Farmer-to-buyer marketplace
- [ ] Weekly SMS alerts via Africa's Talking API
- [ ] Google Analytics dashboard

---

## Contributing

Contributions are welcome — especially:

- Twi language corrections and improvements
- Additional language support (Ga, Dagbani, Hausa, Ewe)
- Local pest and disease data for specific Ghana regions
- Live market price integrations

Open an issue or pull request on GitHub.

---

## License

MIT License — free to use, modify, and distribute with attribution.

---

## About

Built by a Business Analyst from Ghana who wanted to go beyond identifying problems and actually build a solution using AI.

**Nkɔsoɔ** — growth for Ghana's farmers, one question at a time. 🌿

---

*Built with Claude AI by Anthropic · Weather by Open-Meteo · Payments by Paystack*
