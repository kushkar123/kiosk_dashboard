import os
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
    if _cache["weather"]["data"] and (now_ts - _cache["weather"]["ts"] < 300):
        return _cache["weather"]["data"]
    try:
        url = "https://api.open-meteo.com/v1/forecast?latitude=28.367&longitude=79.4304&current=temperature_2m,weather_code&timezone=auto"
        aq_url = "https://air-quality-api.open-meteo.com/v1/air-quality?latitude=28.367&longitude=79.4304&current=us_aqi&timezone=auto"
        res = requests.get(url, verify=False, timeout=4).json()
        aq_res = requests.get(aq_url, verify=False, timeout=4).json()
        temp = res['current']['temperature_2m']
        code = res['current']['weather_code']
        aqi = aq_res['current']['us_aqi']
        code_map = {
            0: 'Clear', 1: 'Mainly Clear', 2: 'Partly Cloudy', 3: 'Overcast',
            45: 'Foggy', 48: 'Foggy', 51: 'Drizzle', 61: 'Light Rain', 63: 'Rain',
            71: 'Snow', 80: 'Rain Showers', 95: 'Thunderstorm'
        }
        condition = code_map.get(code, 'Clear')
        data = {
            "city": "Bareilly",
            "temp": f"{temp}°C",
            "condition": condition,
            "aqi": aqi
        }
        _cache["weather"] = {"data": data, "ts": now_ts}
        return data
    except Exception:
        if _cache["weather"]["data"]:
            return _cache["weather"]["data"]
        return {"city": "Bareilly", "temp": "--°C", "condition": "--", "aqi": "--"}

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
        word = random.choice(lines).split('|')
        return jsonify({"german": word[0].strip(), "english": word[1].strip(), "sentence": word[2].strip()})
    except Exception:
        return jsonify({"german": "Keine Daten", "english": "No data", "sentence": "Check german_words.txt format."})

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
        news = get_rss("https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms", 6, "https://www.moneycontrol.com/rss/buzzingstocks.xml")
    elif 6 <= hour < 9:
        mode = "Morning Briefing"
        market_label = "Nifty 50 (Pre-Open)"
        currency = "₹"
        movers = get_nifty_movers()
        news_badge = "Morning Cues"
        news = get_rss("https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms", 6, "https://www.moneycontrol.com/rss/buzzingstocks.xml")
    else:
        mode = "US Market Hours"
        market_label = "Nasdaq 100"
        currency = "$"
        movers = get_nasdaq_movers()
        news_badge = "US Markets"
        news = get_rss("https://www.cnbc.com/id/10000664/device/rss/rss.html", 6, "https://news.google.com/rss/headlines/section/topic/BUSINESS")

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
        "news": news,
        "trends": trends,
        "breaking_india": breaking_india,
        "ticker": ticker
    }

    return jsonify(payload)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(debug=False, host='0.0.0.0', port=port)
