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
    OPENAI_API_KEY = "" # La stå tom lokalt hvis du skal pushe til GitHub

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
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, title TEXT, link TEXT, summary TEXT, source TEXT, published TEXT, found_at TEXT, matched_keyword TEXT, ai_score INTEGER, ai_reason TEXT, status TEXT DEFAULT 'Ny')''')
    conn.commit(); conn.close()

def article_exists(link):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    res = c.execute("SELECT 1 FROM articles WHERE link = ?", (link,)).fetchone()
    conn.close(); return res is not None

def save_article(entry, source, keyword, score, reason):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    title = clean_html(entry.title)
    summary = clean_html(getattr(entry, 'summary', ''))
    try:
        c.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", (entry.link, title, entry.link, summary, source, getattr(entry, 'published', 'Ukjent'), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), keyword, score, reason, 'Ny'))
        conn.commit(); return True
    except: return False
    finally: conn.close()

def analyze_relevance_with_ai(title, summary, keyword):
    if not client: return 50, "Mangler API-nøkkel"
    clean_title = clean_html(title)
    clean_summary = clean_html(summary)
    prompt = f"Vurder sak for Telemarksavisa. Søkeord: '{keyword}'. Tittel: {clean_title}. Ingress: {clean_summary}. Score 0-100. Format: Score: [tall] Begrunnelse: [tekst]"
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content
        score = int(''.join(filter(str.isdigit, content.split("Score:")[1].split("\n")[0])))
        reason = content.split("Begrunnelse:")[1].strip()
        return score, reason
    except: return 0, "AI feilet"

def fetch_and_filter_news(keywords):
    new_hits = 0
    total_checked = 0
    status_box = st.sidebar.empty() # Boks for statusmeldinger
    progress = st.sidebar.progress(0)
    
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"

    for i, url in enumerate(RSS_SOURCES):
        status_box.text(f"Leser {url}...")
        try: 
            feed = feedparser.parse(url, agent=USER_AGENT)
            for entry in feed.entries:
                total_checked += 1
                
                # --- EKSKLUDERINGS-FILTER ---
                title_lower = entry.title.lower()
                source_lower = feed.feed.get('title', '').lower()
                link_lower = entry.link.lower()
                
                if "telemarksavisa" in title_lower or "telemarksavisa" in source_lower or "ta.no" in link_lower:
                    continue 
                # ----------------------------

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
    
    status_box.empty() # Fjerner teksten "Leser..." når den er ferdig
    progress.empty()   # Fjerner progressbaren
    return new_hits

# --- 5. Hovedprogrammet ---
def main():
    init_db()

    with st.sidebar:
        st.header("TA Monitor")
        st.subheader("📍 Geofilter")
        
        user_input = st.text_area("Søkeord", value=", ".join(DEFAULT_KEYWORDS), height=150)
        active_keywords = [k.strip() for k in user_input.split(",") if k.strip()]
        st.divider()
        
        auto_run = st.toggle("🔄 Autopilot")
        
        if auto_run:
            # 1. KJØR SJEKK
            hits = fetch_and_filter_news(active_keywords)
            if hits: st.toast(f"Fant {hits} nye saker!", icon="🔥")
            
            # 2. VIS VENTEMELDING (UTEN NEDTELLING SOM FYLLER OPP)
            next_run = datetime.now() + timedelta(minutes=10)
            time_str = next_run.strftime("%H:%M")
            
            st.info(f"✅ Ferdig sjekket. \n💤 Sover til kl {time_str}")
            
            # 3. SOV I BAKGRUNNEN (Sparer ressurser og unngår spam)
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
            conn = sqlite3.connect(DB_FILE); c = conn.cursor()
            try:
                c.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"test_{int(time.time())}", "Test-sak fra Skien", "http://test.no", "Ingress.", "TestKilde", "Nå", datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "Skien", 85, "Test", 'Ny'))
                conn.commit()
            except: pass
            conn.close(); st.rerun()

    st.title("🗞️ Nyhetsstrøm for Telemark")
    
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM articles ORDER BY found_at DESC", conn)
    conn.close()

    if not df.empty:
        today = datetime.now().strftime("%Y-%m-%d")
        todays_news = df[df['found_at'].str.contains(today)]
        c1, c2, c3 = st.columns(3)
        c1.metric("Saker i dag", len(todays_news))
        c2.metric("🔥 Høy relevans", len(todays_news[todays_news['ai_score'] > 70]))
        c3.metric("Siste sjekk", datetime.now().strftime("%H:%M"))
        st.divider()

        tab1, tab2 = st.tabs(["🔥 Viktigste", "🗄️ Arkiv"])
        
        def render_grid(dataframe):
            cols_per_row = 3
            for i in range(0, len(dataframe), cols_per_row):
                cols = st.columns(cols_per_row)
                for j in range(cols_per_row):
                    if i + j < len(dataframe):
                        row = dataframe.iloc[i + j]
                        score = row['ai_score'] if row['ai_score'] else 0
                        header_color = "red" if score > 70 else "orange" if score > 30 else "grey"
                        
                        with cols[j]:
                            with st.container(border=True):
                                st.markdown(f"**Score: :{header_color}[{score}]**")
                                st.markdown(f"#### [{row['title']}]({row['link']})")
                                st.info(f"🤖 {row['ai_reason']}")
                                st.caption(f"📍 {row['matched_keyword']} | 📰 {row['source']}")
                                st.caption(f"🕒 {row['found_at']}")

        with tab1: render_grid(df[df['ai_score'] > 70])
        with tab2: render_grid(df)
    else:
        st.info("Ingen saker funnet ennå. Autopilot kjører...")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.error(f"En kritisk feil oppstod: {e}")