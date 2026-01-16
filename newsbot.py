import streamlit as st
import feedparser
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import time
from openai import OpenAI
import os
import re
import requests

# --- 1. Sette opp siden ---
st.set_page_config(page_title="TA Monitor", page_icon="🗞️", layout="wide")

# --- 2. Konfigurasjon ---
DB_FILE = "ta_nyhetsbot.db"

# Henter API-nøkler
try:
    OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
except:
    OPENAI_API_KEY = ""

try:
    SLACK_WEBHOOK_URL = st.secrets["SLACK_WEBHOOK_URL"]
except:
    SLACK_WEBHOOK_URL = ""

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
    "E18", "E134", "Riksvei 36", "Fylkesvei", "Geiteryggen",
    "Breviksbrua", "Grenlandsbrua", "Yara", "Herøya", "Hydro", 
    "Sykehuset Telemark", "Universitetet i Sørøst-Norge", "Skagerak Energi",
    "Odd", "Urædd", "Pors", "Siljan"
]

# --- 3. Tids-fikser (UTC + 1 time) ---
def get_norway_time():
    return datetime.now() + timedelta(hours=1)

# --- 4. Hjelpefunksjoner ---
def clean_html(raw_html):
    if not isinstance(raw_html, str): return ""
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html).strip()

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, title TEXT, link TEXT, summary TEXT, source TEXT, published TEXT, found_at TEXT, matched_keyword TEXT, ai_score INTEGER, ai_reason TEXT, status TEXT DEFAULT 'Ny')''')
            conn.commit()
    except Exception as e:
        st.error(f"Database-feil: {e}")

def article_exists(link):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            res = c.execute("SELECT 1 FROM articles WHERE link = ?", (link,)).fetchone()
        return res is not None
    except:
        return False

# --- SLACK VARSLING (Oppdatert grense) ---
def send_slack_notification(title, link, score, reason, source):
    if not SLACK_WEBHOOK_URL:
        return 
    
    # ENDRET: Terskel senket til 70
    if score < 70:
        return

    # Tilpasser overskriften basert på score
    prefix = "🚨 *BREAKING*" if score >= 90 else "📣 *VIKTIG SAK*"

    payload = {
        "text": f"{prefix} ({score} poeng)\n*<{link}|{title}>*\n🤖 {reason}\n📰 Kilde: {source}"
    }
    
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"Klarte ikke sende til Slack: {e}")

def save_article(entry, source, keyword, score, reason):
    try:
        title = clean_html(entry.title)
        summary = clean_html(getattr(entry, 'summary', ''))
        link = entry.link
        published = getattr(entry, 'published', 'Ukjent')
        found_at = get_norway_time().strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", 
                     (link, title, link, summary, source, published, found_at, keyword, score, reason, 'Ny'))
            conn.commit()
        
        # Sender til Slack (hvis score >= 70)
        send_slack_notification(title, link, score, reason, source)
        
        return True
    except Exception as e:
        st.error(f"Lagringsfeil: {e}")
        return False

def analyze_relevance_with_ai(title, summary, keyword):
    if not client: return 50, "Mangler nøkkel"
    
    clean_title = clean_html(title)
    clean_summary = clean_html(summary)
    
    prompt = f"""
    Du er nyhetsredaktør for Telemarksavisa.
    Søkeord: '{keyword}'.
    Tittel: {clean_title}
    Ingress: {clean_summary}
    
    Gi score 0-100 basert på lokal relevans for Telemark.
    
    SKALA:
    0-39: Irrelevant/Støy.
    40-69: LAV (Generell sak, eller stedsnavn nevnt i bisetning).
    70-89: HØY (Handler om Telemark/lokale forhold).
    90-100: BREAKING/KRITISK (Blålys, store kriser, store lokale nyheter).
    
    Vær streng på 90+.
    Begrunnelse: Maks 8 ord.
    Format: Score: [tall] Begrunnelse: [tekst]
    """
    
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content
        
        score = 0
        if "Score:" in content:
            score_part = content.split("Score:")[1].split("\n")[0]
            score = int(''.join(filter(str.isdigit, score_part)))
        
        reason = "Relevant"
        if "Begrunnelse:" in content:
            reason = content.split("Begrunnelse:")[1].strip()
            
        return score, reason
    except Exception:
        return 50, "AI feilet"

def fetch_and_filter_news(keywords):
    new_hits = 0
    status_box = st.sidebar.empty()
    progress = st.sidebar.progress(0)
    
    USER_AGENT = "Mozilla/5.0"

    for i, url in enumerate(RSS_SOURCES):
        status_box.text(f"Leser {url}...")
        try: 
            feed = feedparser.parse(url, agent=USER_AGENT)
            for entry in feed.entries:
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
                        success = save_article(entry, feed.feed.get('title', url), hit, score, reason)
                        if success:
                            new_hits += 1
        except Exception:
            continue
        progress.progress((i+1)/len(RSS_SOURCES))
    
    status_box.empty() 
    progress.empty()   
    return new_hits

# --- 5. Hovedprogrammet ---
def main():
    st.title("🗞️ Nyhetsstrøm for Telemark")
    init_db()

    # --- SIDEBAR KONFIGURASJON ---
    with st.sidebar:
        st.header("TA Monitor")
        
        if st.button("🗑️ Nullstill database"):
            try:
                os.remove(DB_FILE)
                st.success("Slettet!")
                time.sleep(1)
                st.rerun()
            except: pass

        st.subheader("📍 Geofilter")
        user_input = st.text_area("Søkeord", value=", ".join(DEFAULT_KEYWORDS), height=150)
        active_keywords = [k.strip() for k in user_input.split(",") if k.strip()]
        st.divider()
        
        auto_run = st.toggle("🔄 Autopilot")
        
        if st.button("🔎 Søk manuelt", type="primary"):
            hits = fetch_and_filter_news(active_keywords)
            if hits > 0: 
                # Lagrer info
                st.session_state.last_hits_count = hits
                st.session_state.last_hits_time = get_norway_time().strftime("%H:%M")
                st.rerun()
            else: 
                st.info("Ingen nye treff.")

    # --- AUTOPILOT LOGIKK ---
    if auto_run:
        if 'last_check' not in st.session_state:
            st.session_state.last_check = datetime.min
        
        # Hvis det er tid for ny sjekk
        if datetime.now() - st.session_state.last_check > timedelta(minutes=10):
            hits = fetch_and_filter_news(active_keywords)
            st.session_state.last_check = datetime.now()
            
            st.session_state.last_hits_count = hits
            st.session_state.last_hits_time = get_norway_time().strftime("%H:%M")
            
            st.rerun()

    # --- VISNING AV NYHETER ---
    
    # 1. VARSEL OM NYE SAKER
    if 'last_hits_count' in st.session_state and st.session_state.last_hits_count > 0:
        st.success(f"🚨 Siste søk (kl {st.session_state.last_hits_time}) fant **{st.session_state.last_hits_count}** nye saker!")

    # 2. HENT DATA
    try:
        with sqlite3.connect(DB_FILE) as conn:
            df = pd.read_sql_query("SELECT * FROM articles ORDER BY found_at DESC", conn)
    except:
        df = pd.DataFrame()

    if not df.empty:
        today = get_norway_time().strftime("%Y-%m-%d")
        todays_news = df[df['found_at'].str.contains(today)]
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Saker i dag", len(todays_news))
        c2.metric("Snitt-score", int(df['ai_score'].mean()) if not df.empty else 0)
        c3.metric("Siste sjekk", get_norway_time().strftime("%H:%M"))
        st.divider()

        cols_per_row = 3
        for i in range(0, len(df), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(df):
                    row = df.iloc[i + j]
                    score = row['ai_score'] if row['ai_score'] else 0
                    
                    header_color = "red" if score >= 85 else "orange" if score >= 60 else "grey"
                    
                    with cols[j]:
                        with st.container(border=True):
                            st.markdown(f"**Score: :{header_color}[{score}]**")
                            st.markdown(f"#### [{row['title']}]({row['link']})")
                            st.info(f"🤖 {row['ai_reason']}")
                            st.caption(f"📍 {row['matched_keyword']} | 📰 {row['source']}")
                            st.caption(f"🕒 {row['found_at']}")
    else:
        st.info("Ingen saker funnet ennå.")

    # --- AUTOPILOT PAUSE ---
    if auto_run:
        next_run_server = st.session_state.last_check + timedelta(minutes=10)
        next_run_display = next_run_server + timedelta(hours=1)
        st.sidebar.info(f"💤 Neste sjekk: {next_run_display.strftime('%H:%M')}")
        time.sleep(30)
        st.rerun()

if __name__ == "__main__":
    main()
