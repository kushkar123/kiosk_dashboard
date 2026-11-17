import os
import re
import json
import random
import time
from datetime import datetime
import pytz
import requests
import feedparser
import urllib3

# Suppress insecure request warnings due to local SSL bypass on Windows
urllib3.disable_warnings()

# Patch curl_cffi on Windows to avoid SSL local issuer certificate verification errors with Yahoo Finance
try:
    import curl_cffi.requests
    _orig_request = curl_cffi.requests.Session.request
    def _patched_request(self, method, url, *args, **kwargs):
        kwargs['verify'] = False
        return _orig_request(self, method, url, *args, **kwargs)
    curl_cffi.requests.Session.request = _patched_request
except Exception:
    pass

import yfinance as yf
from flask import Flask, render_template, jsonify

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
IST = pytz.timezone('Asia/Kolkata')

@app.after_request
def add_header(response):
    """Disable browser caching so changes reflect immediately."""
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# In-memory caches to guarantee sub-second dashboard refresh speeds
_cache = {
    "weather": {"data": None, "ts": 0},
    "nasdaq": {"data": None, "ts": 0},
    "nifty": {"data": None, "ts": 0},
    "ticker": {"data": None, "ts": 0},
}

# Persistent last known weather for Bareilly so it NEVER displays '--'
_last_known_weather = {
    "city": "Bareilly",
    "temp": "27.6°C",
    "condition": "Partly Cloudy",
    "aqi": 166
}

# Persistent last known rates for commodities so they NEVER disappear
_last_known_rates = {
    "Gold": {"price": "₹136,655", "change": 0.32},
    "Silver": {"price": "₹178,199", "change": -0.29},
    "Brent": {"price": "$96.17", "change": 0.45},
    "USD/INR": {"price": "₹94.24", "change": 0.58}
}

CACHE_TTL_SECS = 45  # Refresh market data every 45 seconds

def get_bareilly_weather():
    now_ts = time.time()
    # Dynamic 60-second cache for weather and AQI
    if _cache["weather"]["data"] and (now_ts - _cache["weather"]["ts"] < 60):
        return _cache["weather"]["data"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    # 1. Fetch Temperature & Weather Condition (Open-Meteo primary, wttr.in fallback)
    weather_fetched = False
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=28.367&longitude=79.4304&current=temperature_2m,weather_code&timezone=auto"
        res = requests.get(url, headers=headers, verify=False, timeout=5).json()
        cur = res.get('current', {})
        if 'temperature_2m' in cur:
            _last_known_weather["temp"] = f"{cur['temperature_2m']}°C"
            code_map = {
                0: 'Clear', 1: 'Mainly Clear', 2: 'Partly Cloudy', 3: 'Overcast',
                45: 'Foggy', 48: 'Foggy', 51: 'Drizzle', 61: 'Light Rain', 63: 'Rain',
                71: 'Snow', 80: 'Rain Showers', 95: 'Thunderstorm'
            }
            _last_known_weather["condition"] = code_map.get(cur.get('weather_code'), 'Clear')
            weather_fetched = True
    except Exception:
        pass

    if not weather_fetched:
        try:
            w_res = requests.get('https://wttr.in/Bareilly?format=j1', headers=headers, verify=False, timeout=5).json()
            cur_w = w_res.get('current_condition', [{}])[0]
            if 'temp_C' in cur_w:
                _last_known_weather["temp"] = f"{cur_w['temp_C']}°C"
                desc = cur_w.get('weatherDesc', [{}])[0].get('value', 'Clear').strip()
                _last_known_weather["condition"] = desc
        except Exception:
            pass

    # 2. Fetch Live Ground Station AQI for Bareilly Civil Lines via AQICN / CPCB
    try:
        aq_res = requests.get('https://aqicn.org/city/india/bareilly/civil-lines/', headers=headers, verify=False, timeout=5)
        m = re.findall(r'"aqi":(\d+)', aq_res.text)
        if m:
            _last_known_weather["aqi"] = int(m[0])
        else:
            m2 = re.search(r'id=["\']aqivignette["\'][^>]*>(\d+)', aq_res.text)
            if m2:
                _last_known_weather["aqi"] = int(m2.group(1))
    except Exception:
        pass

    data = {
        "city": "Bareilly",
        "temp": _last_known_weather["temp"],
        "condition": _last_known_weather["condition"],
        "aqi": _last_known_weather["aqi"]
    }
    _cache["weather"] = {"data": data, "ts": now_ts}
    return data

def get_rss(url, limit=5, fallback_url=None):
    try:
        feed = feedparser.parse(url)
        if feed.entries:
            return [{"title": entry.title} for entry in feed.entries[:limit]]
    except Exception:
        pass
    
    if fallback_url and fallback_url != url:
        try:
            feed = feedparser.parse(fallback_url)
            if feed.entries:
                return [{"title": entry.title} for entry in feed.entries[:limit]]
        except Exception:
            pass
            
    return []

def get_fresh_news(limit_per_category=4):
    """Fetches real-time Global and India news with relative timestamps."""
    global_url = "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"
    local_url = "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en"
    
    def parse_feed_items(url, source_label, fallback_url=None):
        items = []
        now_ts = time.time()
        feed = None
        try:
            feed = feedparser.parse(url)
        except Exception:
            pass

        if (not feed or not feed.entries) and fallback_url:
            try:
                feed = feedparser.parse(fallback_url)
            except Exception:
                pass

        if feed and feed.entries:
            for e in feed.entries[:10]:
                title = e.get('title', '').strip()
                if not title:
                    continue
                
                pub_ts = None
                if hasattr(e, 'published_parsed') and e.published_parsed:
                    try:
                        pub_ts = time.mktime(e.published_parsed)
                    except Exception:
                        pass
                
                time_tag = "Live"
                if pub_ts:
                    diff_mins = int((now_ts - pub_ts) / 60)
                    if diff_mins < 0:
                        diff_mins = 0
                    if diff_mins < 60:
                        time_tag = f"{diff_mins}m ago"
                    elif diff_mins < 1440:
                        time_tag = f"{diff_mins // 60}h ago"
                    else:
                        time_tag = f"{diff_mins // 1440}d ago"

                items.append({
                    "title": title,
                    "time_tag": time_tag,
                    "source": source_label,
                    "pub_ts": pub_ts or now_ts
                })
        items.sort(key=lambda x: x['pub_ts'], reverse=True)
        return items[:limit_per_category]

    global_news = parse_feed_items(global_url, "Global", "https://search.cnbc.com/rs/search/view.html?partnerId=2000&keywords=markets&sort=date&type=rss")
    local_news = parse_feed_items(local_url, "India", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms")

    combined = []
    for i in range(max(len(global_news), len(local_news))):
        if i < len(global_news):
            combined.append(global_news[i])
        if i < len(local_news):
            combined.append(local_news[i])

    return {
        "global": global_news,
        "local": local_news,
        "combined": combined[:6]
    }

def get_nasdaq_movers():
    now_ts = time.time()
    if _cache["nasdaq"]["data"] and (now_ts - _cache["nasdaq"]["ts"] < CACHE_TTL_SECS):
        return _cache["nasdaq"]["data"]

    nasdaq_symbols = [
        'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AVGO', 'COST', 'PEP',
        'NFLX', 'AMD', 'ADBE', 'QCOM', 'TXN', 'AMAT', 'TMUS', 'ISRG', 'INTU', 'BKNG',
        'AMGN', 'HON', 'VRTX', 'LRCX', 'PANW', 'ADI', 'GILD', 'MU', 'KLAC', 'PDD',
        'MELI', 'SNPS', 'CDNS', 'CRWD', 'MAR', 'CSX', 'PYPL', 'ORLY', 'CTAS', 'NXPI'
    ]
    try:
        df = yf.download(nasdaq_symbols, period='2d', interval='1d', progress=False)
        closes = df['Close']
        changes = []
        for sym in nasdaq_symbols:
            if sym in closes.columns:
                s = closes[sym].dropna()
                if len(s) >= 2:
                    prev = float(s.iloc[-2])
                    curr = float(s.iloc[-1])
                    if prev != 0:
                        chg = ((curr - prev) / prev) * 100
                        changes.append({"name": sym, "price": round(curr, 2), "change": round(chg, 2)})

        changes.sort(key=lambda x: x['change'], reverse=True)
        gainers = changes[:6]
        losers = sorted(changes[-6:], key=lambda x: x['change'])
        data = {"gainers": gainers, "losers": losers}
        _cache["nasdaq"] = {"data": data, "ts": now_ts}
        return data
    except Exception:
        if _cache["nasdaq"]["data"]:
            return _cache["nasdaq"]["data"]
        return {"gainers": [], "losers": []}

def get_nifty_movers():
    now_ts = time.time()
    if _cache["nifty"]["data"] and (now_ts - _cache["nifty"]["ts"] < CACHE_TTL_SECS):
        return _cache["nifty"]["data"]

    nifty_symbols = [
        'RELIANCE.NS', 'TCS.NS', 'HDFCBANK.NS', 'INFY.NS', 'ICICIBANK.NS', 'BHARTIARTL.NS',
        'ITC.NS', 'SBIN.NS', 'LT.NS', 'HINDUNILVR.NS', 'BAJFINANCE.NS', 'HCLTECH.NS',
        'MARUTI.NS', 'SUNPHARMA.NS', 'KOTAKBANK.NS', 'TITAN.NS', 'ONGC.NS',
        'ADANIENT.NS', 'NTPC.NS', 'POWERGRID.NS', 'TATASTEEL.NS', 'AXISBANK.NS', 'M&M.NS',
        'COALINDIA.NS', 'ASIANPAINT.NS', 'WIPRO.NS', 'ULTRACEMCO.NS', 'JSWSTEEL.NS'
    ]
    try:
        df = yf.download(nifty_symbols, period='2d', interval='1d', progress=False)
        closes = df['Close']
        changes = []
        for sym in nifty_symbols:
            if sym in closes.columns:
                s = closes[sym].dropna()
                if len(s) >= 2:
                    prev = float(s.iloc[-2])
                    curr = float(s.iloc[-1])
                    if prev != 0:
                        chg = ((curr - prev) / prev) * 100
                        clean_name = sym.replace('.NS', '')
                        changes.append({"name": clean_name, "price": round(curr, 2), "change": round(chg, 2)})

        changes.sort(key=lambda x: x['change'], reverse=True)
        gainers = changes[:6]
        losers = sorted(changes[-6:], key=lambda x: x['change'])
        data = {"gainers": gainers, "losers": losers}
        _cache["nifty"] = {"data": data, "ts": now_ts}
        return data
    except Exception:
        if _cache["nifty"]["data"]:
            return _cache["nifty"]["data"]
        return {"gainers": [], "losers": []}

def get_tv_commodities():
    """Fetches real-time MCX:GOLD1!, MCX:SILVER1!, and FX:UKOIL prices via TradingView scanner."""
    url = "https://scanner.tradingview.com/global/scan"
    payload = {
        "symbols": {"tickers": ["MCX:GOLD1!", "MCX:SILVER1!", "FX:UKOIL"]},
        "columns": ["name", "close", "change"]
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # Attempt 1: Try curl_cffi with chrome impersonation (handles Cloudflare on Render)
    try:
        import curl_cffi.requests as c_req
        r = c_req.post(url, json=payload, headers=headers, impersonate="chrome", verify=False, timeout=4)
        if r.status_code == 200:
            result = {}
            for item in r.json().get("data", []):
                sym = item.get("s")
                cols = item.get("d", [])
                if len(cols) >= 3:
                    result[sym] = {"name": cols[0], "price": cols[1], "change": round(cols[2], 2)}
            if result:
                return result
    except Exception:
        pass

    # Attempt 2: Standard requests
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=4)
        if r.status_code == 200:
            result = {}
            for item in r.json().get("data", []):
                sym = item.get("s")
                cols = item.get("d", [])
                if len(cols) >= 3:
                    result[sym] = {"name": cols[0], "price": cols[1], "change": round(cols[2], 2)}
            if result:
                return result
    except Exception:
        pass

    return {}

def get_inr_ticker_data():
    now_ts = time.time()
    if _cache["ticker"]["data"] and (now_ts - _cache["ticker"]["ts"] < CACHE_TTL_SECS):
        return _cache["ticker"]["data"]

    items = []
    usd_inr = 94.24
    usd_inr_change = 0.58

    # 1. Fetch Nifty, Sensex, and USD/INR from Yahoo Finance
    try:
        symbols = ['^NSEI', '^BSESN', 'USDINR=X']
        df = yf.download(symbols, period='2d', interval='1d', progress=False)
        closes = df['Close']
        
        if 'USDINR=X' in closes and len(closes['USDINR=X'].dropna()) > 0:
            usd_inr = float(closes['USDINR=X'].dropna().iloc[-1])
            usd_inr_prev = float(closes['USDINR=X'].dropna().iloc[-2]) if len(closes['USDINR=X'].dropna()) > 1 else usd_inr
            usd_inr_change = ((usd_inr - usd_inr_prev) / usd_inr_prev) * 100 if usd_inr_prev else 0
            _last_known_rates["USD/INR"] = {"price": f"₹{usd_inr:.2f}", "change": round(usd_inr_change, 2)}

        # Nifty 50
        if '^NSEI' in closes and len(closes['^NSEI'].dropna()) >= 2:
            s = closes['^NSEI'].dropna()
            curr, prev = float(s.iloc[-1]), float(s.iloc[-2])
            items.append({"name": "Nifty 50", "price": f"₹{curr:,.0f}", "change": round(((curr - prev)/prev)*100, 2)})

        # Sensex
        if '^BSESN' in closes and len(closes['^BSESN'].dropna()) >= 2:
            s = closes['^BSESN'].dropna()
            curr, prev = float(s.iloc[-1]), float(s.iloc[-2])
            items.append({"name": "Sensex", "price": f"₹{curr:,.0f}", "change": round(((curr - prev)/prev)*100, 2)})
    except Exception:
        pass

    # 2. Real-time MCX:GOLD1!, MCX:SILVER1!, and FX:UKOIL from TradingView
    tv = get_tv_commodities()

    # Gold (MCX:GOLD1!) - with persistent last-known guarantee
    if 'MCX:GOLD1!' in tv:
        g = tv['MCX:GOLD1!']
        _last_known_rates["Gold"] = {"price": f"₹{g['price']:,.0f}", "change": g['change']}
    items.append({"name": "Gold", "price": _last_known_rates["Gold"]["price"], "change": _last_known_rates["Gold"]["change"]})

    # Silver (MCX:SILVER1!) - with persistent last-known guarantee
    if 'MCX:SILVER1!' in tv:
        s = tv['MCX:SILVER1!']
        _last_known_rates["Silver"] = {"price": f"₹{s['price']:,.0f}", "change": s['change']}
    items.append({"name": "Silver", "price": _last_known_rates["Silver"]["price"], "change": _last_known_rates["Silver"]["change"]})

    # Brent (UKOIL in Dollar terms) - with persistent last-known guarantee
    if 'FX:UKOIL' in tv:
        b = tv['FX:UKOIL']
        _last_known_rates["Brent"] = {"price": f"${b['price']:.2f}", "change": b['change']}
    items.append({"name": "Brent", "price": _last_known_rates["Brent"]["price"], "change": _last_known_rates["Brent"]["change"]})

    # USD/INR
    items.append({"name": "USD/INR", "price": _last_known_rates["USD/INR"]["price"], "change": _last_known_rates["USD/INR"]["change"]})

    _cache["ticker"] = {"data": items, "ts": now_ts}
    return items

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/flashcard')
def flashcard():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, 'german_words.txt')
        if not os.path.exists(file_path):
            file_path = os.path.join(base_dir, 'german-words.txt')
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines() if '|' in line]
        if not lines:
            raise ValueError
        parts = [p.strip() for p in random.choice(lines).split('|')]
        german = parts[0] if len(parts) >= 1 else "Keine Daten"
        english = parts[1] if len(parts) >= 2 else ""
        sentence = parts[2] if len(parts) >= 3 else ""
        return jsonify({"german": german, "english": english, "sentence": sentence})
    except Exception:
        return jsonify({"german": "Keine Daten", "english": "No data", "sentence": "Check german_words.txt format."})

@app.route('/api/german-screensaver')
def german_screensaver():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, 'german_words.txt')
        if not os.path.exists(file_path):
            file_path = os.path.join(base_dir, 'german-words.txt')
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = [line.strip() for line in f.readlines() if '|' in line]
        if not lines:
            raise ValueError
        
        parsed = []
        for line in lines:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 3:
                parsed.append({"german": parts[0], "english": parts[1], "sentence": parts[2]})
            elif len(parts) == 2:
                parsed.append({"german": parts[0], "english": parts[1], "sentence": ""})

        total = len(parsed)
        # Select 3 jumbled and random words (distinct)
        sample_size = min(3, total)
        selected = random.sample(parsed, sample_size)
        random.shuffle(selected)
            
        return jsonify(selected)
    except Exception:
        fallback = [
            {"german": "der Bahnhof", "english": "the train station", "sentence": "Ich gehe zum Bahnhof."},
            {"german": "die Zeitung", "english": "the newspaper", "sentence": "Sie liest die Zeitung."},
            {"german": "erfolgreich", "english": "successful", "sentence": "Das Projekt war sehr erfolgreich."}
        ]
        return jsonify(fallback)

@app.route('/api/topics')
def get_topics():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, 'topics.json')
        items = []
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                items = json.load(f)
        else:
            txt_path = os.path.join(base_dir, 'topics.txt')
            with open(txt_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if '|' in line:
                        p = line.split('|')
                        topic = p[0].strip()
                        summary = p[1].strip()
                        clean_topic = topic
                        if '.' in topic[:5]:
                            num_part, rest = topic.split('.', 1)
                            if num_part.isdigit():
                                clean_topic = rest.strip()
                        items.append({
                            "id": len(items) + 1,
                            "topic": clean_topic,
                            "summary": summary
                        })

        total = len(items)
        if total == 0:
            raise ValueError

        # Select 2 distinct random and jumbled topics
        sample_size = min(2, total)
        selected = random.sample(items, sample_size)
        random.shuffle(selected)

        return jsonify({
            "topics": selected,
            "total": total
        })
    except Exception:
        fallback = [
            {"id": 1, "topic": "The Musical Instrument Mandar", "summary": "Traditional tribal percussion instrument of Jharkhand, made of clay or wood, used in festivals to preserve cultural heritage."},
            {"id": 2, "topic": "The Sengol", "summary": "A historical silver scepter symbolizing the transfer of power, recently installed in India's new Parliament building as a cultural emblem."}
        ]
        return jsonify({"topics": fallback, "total": 116})

@app.route('/api/data')
def dashboard_data():
    now = datetime.now(IST)
    hour = now.hour
    
    # Dynamic Time-Based Switching Logic
    if 9 <= hour < 17:
        mode = "Indian Market Hours"
        market_label = "Nifty 50"
        currency = "₹"
        movers = get_nifty_movers()
        news_badge = "Indian Markets"
    elif 6 <= hour < 9:
        mode = "Morning Briefing"
        market_label = "Nifty 50 (Pre-Open)"
        currency = "₹"
        movers = get_nifty_movers()
        news_badge = "Morning Cues"
    else:
        mode = "US Market Hours"
        market_label = "Nasdaq 100"
        currency = "$"
        movers = get_nasdaq_movers()
        news_badge = "US Markets"

    # Fresh Real-Time Global & Local News (filtered for today with time tags)
    news_pack = get_fresh_news(4)

    # Weather for Bareilly (always active)
    weather = get_bareilly_weather()

    # Column 3: Top 5 Viral Trends + Top 5 India Breaking News
    trends = get_rss("https://trends.google.com/trending/rss?geo=IN", 5)
    breaking_india = get_rss("https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en", 5)

    # Bottom Ticker Data (Prices in INR)
    ticker = get_inr_ticker_data()

    payload = {
        "time": now.strftime("%I:%M %p"),
        "date": now.strftime("%A, %b %d"),
        "mode": mode,
        "news_badge": news_badge,
        "weather": weather,
        "primary_focus": {
            "market": market_label,
            "currency": currency,
            "gainers": movers.get("gainers", []),
            "losers": movers.get("losers", [])
        },
        "news": news_pack.get("combined", []),
        "news_global": news_pack.get("global", []),
        "news_local": news_pack.get("local", []),
        "trends": trends,
        "breaking_india": breaking_india,
        "ticker": ticker
    }

    return jsonify(payload)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(debug=False, host='0.0.0.0', port=port)
