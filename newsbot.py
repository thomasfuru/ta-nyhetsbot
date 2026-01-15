import streamlit as st
import feedparser
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import time
from openai import OpenAI
import re

# --- 1. Sette opp siden ---
st.set_page_config(page_title="TA Monitor", page_icon="🗞️", layout="wide")

# --- 2. Konfigurasjon ---
DB_FILE = "ta_nyhetsbot.db"

# Henter nøkkelen fra secrets (skyen) eller fallback (lokalt)
try:
    OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
except:
    OPENAI_API_KEY = "" 

# Initialiser AI
client = None
if "sk-" in OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

RSS_SOURCES = [
    "https://www.nrk.no/toppsaker.rss",
    "https://www.vg.no/rss/feed",
    "https://www.dagbladet.no/rss/nyheter",
    "https://www.e24.no/rss",
    "https://www.nrk.no/vestfoldogtelemark/siste.rss",
    "https://news.google.com/rss/search?q=Telemark+OR+Skien+OR+Porsgrunn+when:1d&hl=no&gl=NO&ceid=NO:no"
]

DEFAULT_KEYWORDS = [
    "Telemark", "Skien", "Porsgrunn", "Bamble", "Kragerø", 
    "Notodden", "Tinn", "Vinje", "Nome", "Seljord", "Kviteseid",
    "Nissedal", "Fyresdal", "Tokke", "Hjartdal", "Bø", "Sauherad",
    "Grenland", "Vest-Telemark", "Øst-Telemark", "Midt-Telemark",
    "E18", "E134", "Riksvei 36", "Fylkesvei", "Gullknapp", "Geiteryggen",
    "Breviksbrua", "Grenlandsbrua", "Yara", "Herøya", "Hydro", "Equinor", 
    "Sykehuset Telemark", "Universitetet i Sørøst-Norge", "Skagerak Energi",
    "Odd", "Urædd", "Pors", "Notodden FK"
]

# --- 3. Hjelpefunksjoner ---
def clean_html(raw_html):
    if not isinstance(raw_html, str): return ""
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html).strip()

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, title TEXT, link TEXT, summary TEXT, source TEXT, published TEXT, found_at TEXT, matched_keyword TEXT, ai_score INTEGER, ai_reason TEXT, status TEXT DEFAULT 'Ny')''')
        conn.commit()

def article_exists(link):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        res = c.execute("SELECT 1 FROM articles WHERE link = ?", (link,)).fetchone()
    return res is not None

def save_article(entry, source, keyword, score, reason):
    title = clean_html(entry.title)
    summary = clean_html(getattr(entry, 'summary', ''))
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", (entry.link, title, entry.link, summary, source, getattr(entry, 'published', 'Ukjent'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), keyword, score, reason, 'Ny'))
            conn.commit()
        return True
    except: 
        return False

def analyze_relevance_with_ai(title, summary, keyword):
    if not client: return 50, "Mangler API-nøkkel"
    clean_title = clean_html(title)
    clean_summary = clean_html(summary)
    
    prompt = f"Vurder sak for Telemarksavisa. Søkeord: '{keyword}'. Tittel: {clean_title}. Ingress: {clean_summary}. VIKTIG: Begrunnelsen skal være ekstremt kort (maks 10-15 ord). Format: Score: [tall] Begrunnelse: [tekst]"
    
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content
        
        score = 0
        if "Score:" in content:
            score_part = content.split("Score:")[1].split("\n")[0]
            score = int(''.join(filter(str.isdigit, score_part)))
            
        reason = "Ingen begrunnelse"
        if "Begrunnelse:" in content:
            reason = content.split("Begrunnelse:")[1].strip()
            
        return score, reason
    except Exception:
        return 0, "AI feilet"

def fetch_and_filter_news(keywords):
    new_hits = 0
    status_box = st.sidebar.empty()
    progress = st.sidebar.progress(0)
    
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

    for i, url in enumerate(RSS_SOURCES):
        status_box.text(f"Leser {url}...")
        try: 
            feed = feedparser.parse(url, agent=USER_AGENT)
            for entry in feed.entries:
                
                # EKSKLUDER TA
                t = entry.title.lower()
                s = feed.feed.get('title', '').lower()
                l = entry.link.lower()
                if "telemarksavisa" in t or "telemarksavisa" in s or "ta.no" in l:
                    continue 

                raw_text = (entry.title + " " + getattr(entry, 'summary', '')).lower()
                hit = next((k for k in keywords if k.lower() in raw_text), None)
                
                if hit:
                    if not article_exists(entry.link):
                        score, reason = analyze_relevance_with_ai(entry.title, getattr(entry, 'summary', ''), hit)
                        save_article(entry, feed.feed.get('title', url), hit, score, reason)
                        new_hits += 1
        except Exception: 
            continue
        progress.progress((i+1)/len(RSS_SOURCES))
    
    status_box.empty() 
    progress.empty()   
    return new_hits

# --- 5. Hovedprogrammet ---
def main():
    # START MED Å TEGNE SIDEN (Så du ser den med en gang)
    st.title("🗞️ Nyhetsstrøm for Telemark")
    init_db()

    # --- SIDEBAR LOGIKK ---
    with st.sidebar:
        st.header("TA Monitor")
        st.subheader("📍 Geofilter")
        
        user_input = st.text_area("Søkeord", value=", ".join(DEFAULT_KEYWORDS), height=150)
        active_keywords = [k.strip() for k in user_input.split(",") if k.strip()]
        st.divider()
        
        auto_run = st.toggle("🔄 Autopilot")
        
        if auto_run:
            hits = fetch_and_filter_news(active_keywords)
            if hits: st.toast(f"Fant {hits} nye saker!", icon="🔥")
            
            next_run = datetime.now() + timedelta(minutes=10)
            t_str = next_run.strftime("%H:%M")
            st.info(f"✅ Ferdig. Sover til {t_str}")
            
            time.sleep(600) 
            st.rerun()
            
        elif st.button("🔎 Søk manuelt", type="primary"):
            hits = fetch_and_filter_news(active_keywords)
            if hits > 0: 
                st.success(f"Fant {hits} nye!")
                time.sleep(1)
                st.rerun()
            else: 
                st.info("Ingen nye treff.")

        if st.button("🛠️ Test"):
            try:
                # Oppretter dummy-data
                tid = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                save_article(
                    type('obj', (object,), {'link':f"http://test{int(time.time())}.no", 'title':"Test-sak", 'summary':"Ingress"}),