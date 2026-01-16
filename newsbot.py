import streamlit as st
import feedparser
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
import time
from openai import OpenAI
import os
import re

# --- 1. Sette opp siden ---
st.set_page_config(page_title="TA Monitor", page_icon="🗞️", layout="wide")

# --- 2. Konfigurasjon ---
DB_FILE = "ta_nyhetsbot.db"

try:
    OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
except:
    OPENAI_API_KEY = "" 

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
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS articles (id TEXT PRIMARY KEY, title TEXT, link TEXT, summary TEXT, source TEXT, published TEXT, found_at TEXT, matched_keyword TEXT, ai_score INTEGER, ai_reason TEXT, status TEXT DEFAULT 'Ny')''')
            conn.commit()
    except Exception as e:
        st.error(f"Database-feil ved oppstart: {e}")

def article_exists(link):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            res = c.execute("SELECT 1 FROM articles WHERE link = ?", (link,)).fetchone()
        return res is not None
    except:
        return False

def save_article(entry, source, keyword, score, reason):
    try:
        title = clean_html(entry.title)
        summary = clean_html(getattr(entry, 'summary', ''))
        link = entry.link
        published = getattr(entry, 'published', 'Ukjent')
        found_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?)", 
                     (link, title, link, summary, source, published, found_at, keyword, score, reason, 'Ny'))
            conn.commit()
        return True
    except Exception as e:
        st.error(f"Kunne ikke lagre sak: {e}")
        return False

def analyze_relevance_with_ai(title, summary, keyword):
    if not client: return 85, "Automatisk score (mangler nøkkel)"
    
    clean_title = clean_html(title)
    clean_summary = clean_html(summary)
    
    # AGGRESSIV INSTRUKS (Høy score)
    prompt = f"""
    Du er nyhetssjef for Telemarksavisa.
    Søkeord funnet: '{keyword}'.
    Tittel: {clean_title}
    Ingress: {clean_summary}
    
    INSTRUKSJONER:
    1. Vær RAUS med poengene.
    2. Hvis søkeordet '{keyword}' er nevnt -> Gi MINST 80 poeng.
    3. Hvis saken handler om Telemark -> Gi 90-100 poeng.
    4. Begrunnelse: MAKS 10 ord.
    
    Format: 
    Score: [tall 0-100] 
    Begrunnelse: [Kort tekst]
    """
    
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content
        
        score = 0
        if "Score:" in content:
            score_part = content.split("Score:")[1].split("\n")[0]
            score = int(''.join(filter(str.isdigit, score_part)))
            
        reason = "Relevant for Telemark"
        if "Begrunnelse:" in content:
            reason = content.split("Begrunnelse:")[1].strip()
            
        return score, reason
    except Exception:
        return 80, "AI feilet, satte standard score"

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
                        success = save_article(entry, feed.feed.get('title', url), hit, score, reason)
                        if success:
                            new_hits += 1
        except Exception as e:
            st.sidebar.error(f"Feil i feed: {e}")
            continue
        progress.progress((i+1)/len(RSS_SOURCES))
    
    status_box.empty() 
    progress.empty()   
    return new_hits

# --- 5. Hovedprogrammet ---
def main():
    st.title("🗞️ Nyhetsstrøm for Telemark")
    init_db()

    # --- SIDEBAR ---
    with st.sidebar:
        st.header("TA Monitor")
        
        if st.button("🗑️ Nullstill database"):
            try:
                os.remove(DB_FILE)
                st.success("Slettet! Laster på nytt...")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.warning(f"Kunne ikke slette: {e}")

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
                class MockEntry: pass
                dummy = MockEntry()
                dummy.link = f"http://test{int(time.time())}.no"
                dummy.title = "Test-sak fra Skien"
                dummy.summary = "Dette er en test."
                dummy.published = "Nå"
                if save_article(dummy, "TestKilde", "Skien", 95, "Test av høy score"):
                    st.success("Test lagret!")
                    time.sleep(1)
                    st.rerun()
            except Exception as e:
                st.error(f"Test feilet: {e}")

    # --- HOVEDVISNING (Kronologisk liste) ---
    try:
        with sqlite3.connect(DB_FILE) as conn:
            # Henter ALLE saker sortert på TID (nyeste funn øverst)
            df = pd.read_sql_query("SELECT * FROM articles ORDER BY found_at DESC", conn)
    except Exception as e:
        st.error(f"Databasefeil: {e}")
        df = pd.DataFrame()

    if not df.empty:
        today = datetime.now().strftime("%Y-%m-%d")
        todays_news = df[df['found_at'].str.contains(today)]
        
        # Toppstatistikk
        c1, c2, c3 = st.columns(3)
        c1.metric("Saker i dag", len(todays_news))
        c2.metric("Gjennomsnittsscore", int(df['ai_score'].mean()) if not df.empty else 0)
        c3.metric("Siste oppdatering", datetime.now().strftime("%H:%M"))
        st.divider()

        # TEGNER KORTENE (Uten faner, rett nedover)
        cols_per_row = 3
        for i in range(0, len(df), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(df):
                    row = df.iloc[i + j]
                    score = row['ai_score'] if row['ai_score'] else 0
                    
                    # Fargekoder basert på score
                    header_color = "red" if score >= 80 else "orange" if score >= 50 else "grey"
                    
                    with cols[j]:
                        with st.container(border=True):
                            # Tittel og Score
                            st.markdown(f"**Score: :{header_color}[{score}]**")
                            st.markdown(f"#### [{row['title']}]({row['link']})")
                            
                            # Info
                            st.info(f"🤖 {row['ai_reason']}")
                            st.caption(f"📍 {row['matched_keyword']} | 📰 {row['source']}")
                            st.caption(f"🕒 Funnet: {row['found_at']}")
    else:
        st.info("Ingen saker funnet ennå. Trykk på '🔎 Søk manuelt' for å starte.")

if __name__ == "__main__":
    main()
