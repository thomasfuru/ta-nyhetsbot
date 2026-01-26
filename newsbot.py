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
from bs4 import BeautifulSoup
from time import mktime

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

# --- OPPDATERT KILDELISTE (MED when:1d PÅ ALT) ---
RSS_SOURCES = [
    # Riksdekkende
    "https://www.nrk.no/toppsaker.rss",
    "https://www.vg.no/rss/feed",
    "https://www.dagbladet.no/rss/nyheter",
    "https://www.e24.no/rss",
    "https://news.google.com/rss/search?q=site:finansavisen.no+when:1d&hl=no&gl=NO&ceid=NO:no",
    "https://news.google.com/rss/search?q=site:dn.no+when:1d&hl=no&gl=NO&ceid=NO:no",
    "https://news.google.com/rss/search?q=site:nettavisen.no+when:1d&hl=no&gl=NO&ceid=NO:no",
    
    # Regionale / Lokale (Lagt til when:1d for å unngå arkivsaker)
    "https://www.nrk.no/vestfoldogtelemark/siste.rss",
    "https://news.google.com/rss/search?q=site:varden.no+when:1d&hl=no&gl=NO&ceid=NO:no",
    "https://news.google.com/rss/search?q=site:op.no+when:1d&hl=no&gl=NO&ceid=NO:no",  
    "https://news.google.com/rss/search?q=site:pd.no+when:1d&hl=no&gl=NO&ceid=NO:no",  
    "https://news.google.com/rss/search?q=site:kv.no+when:1d&hl=no&gl=NO&ceid=NO:no",  
    "https://news.google.com/rss/search?q=site:telen.no+when:1d&hl=no&gl=NO&ceid=NO:no", 
    "https://news.google.com/rss/search?q=site:tb.no+when:1d&hl=no&gl=NO&ceid=NO:no",   
    "https://news.google.com/rss/search?q=site:sb.no+when:1d&hl=no&gl=NO&ceid=NO:no",   
    "https://news.google.com/rss/search?q=site:drangedalsposten.no+when:1d&hl=no&gl=NO&ceid=NO:no",
    
    # Generelt søk
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
    "Odd", "Urædd", "Pors", "Siljan", "Larvik", "Drangedal"
]

# --- 3. Tids-fikser ---
def get_norway_time():
    return datetime.now() + timedelta(hours=1)

# --- NY: TIDS-POLITIET (Sjekker om saken er eldre enn 24 timer) ---
def is_article_fresh(entry):
    try:
        # Hvis RSS har publiseringsdato
        if hasattr(entry, 'published_parsed') and entry.published_parsed:
            published_dt = datetime.fromtimestamp(mktime(entry.published_parsed))
            # Legg til 24 timer margin. Er den eldre enn det?
            cutoff = datetime.now() - timedelta(hours=24)
            if published_dt < cutoff:
                return False # For gammel!
        return True # Ingen dato eller fersk nok
    except:
        return True # Lar tvilen komme til gode hvis dato mangler

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

# --- SLACK VARSLING ---
def send_slack_notification(title, link, score, reason, source):
    if not SLACK_WEBHOOK_URL:
        return 
    
    if score < 70:
        return

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
        
        send_slack_notification(title, link, score, reason, source)
        return True
    except Exception as e:
        st.error(f"Lagringsfeil: {e}")
        return False

# --- AI ANALYSE (STRENGERE) ---
def analyze_relevance_with_ai(title, summary, keyword):
    if not client: return 50, "Mangler nøkkel"
    
    clean_title = clean_html(title)
    clean_summary = clean_html(summary)
    
    # OPPDATERT PROMPT: Strengere mot gamle saker og støy
    prompt = f"""
    Du er nyhetsredaktør for Telemarksavisa.
    Søkeord/Tema: '{keyword}'.
    Tittel: {clean_title}
    Ingress/Info: {clean_summary}
    
    Gi score 0-100 basert på lokal relevans for Telemark.
    
    REGLER FOR POENG:
    - Er saken fra 2024 eller eldre? -> GI 0 POENG.
    - Er det en samleside/forside uten en konkret nyhet? -> GI 0 POENG.
    - Handler det egentlig om Børsen/Oslo og ikke Bø i Telemark? -> GI 0 POENG.
    
    SKALA HVIS RELEVANT:
    0-39: Irrelevant/Støy/Gammelt.
    40-69: LAV.
    70-89: HØY (Handler om Telemark/lokale forhold).
    90-100: BREAKING/KRITISK (Konkurs, Tvang, blålys, store kriser).
    
    Begrunnelse: Maks 8 ord.
    Format: Score: [tall] Begrunnelse: [tekst]
    """
    
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content
        
        score = 0
        if "Score:" in content:
            score_part = content.split("Score:")[1].split("\n")[0]
            match = re.search(r'\d+', score_part)
            if match:
                score = int(match.group())
                if score > 100: score = 100
        
        reason = "Relevant"
        if "Begrunnelse:" in content:
            reason = content.split("Begrunnelse:")[1].strip()
            
        return score, reason
    except Exception:
        return 50, "AI feilet"

# --- BRØNNØYSUND-SJEKK ---
def check_brreg():
    hits = 0
    today = datetime.now()
    date_to = today.strftime("%d.%m.%Y")
    date_from = (today - timedelta(days=7)).strftime("%d.%m.%Y")
    
    search_types = [
        {"id": "51", "name": "Konkursåpning"},
        {"id": "52", "name": "Tvangsavvikling"},
        {"id": "53", "name": "Tvangsoppløsning"},
        {"id": "56", "name": "Gjeldsforhandling"},
        {"id": "55", "name": "Oppbud"}
    ]
    
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html,application/xhtml+xml,application/xml"
    }

    debug_url = f"https://w2.brreg.no/kunngjoring/kombisok.jsp?datoFra={date_from}&datoTil={date_to}&id_region=400&id_fylke=40&id_kommune=-+-+-&id_niva1=51&id_niva2=-+-+-&id_bransje1=0"
    st.sidebar.markdown("### 🕵️‍♂️ Debug Brreg")
    st.sidebar.markdown(f"[Åpne manuelt søk]({debug_url})")

    for stype in search_types:
        url = f"https://w2.brreg.no/kunngjoring/kombisok.jsp?datoFra={date_from}&datoTil={date_to}&id_region=400&id_fylke=40&id_kommune=-+-+-&id_niva1={stype['id']}&id_niva2=-+-+-&id_bransje1=0"
        
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.encoding = 'ISO-8859-1' 
            soup = BeautifulSoup(r.text, 'html.parser')
            
            for link in soup.find_all('a', href=True):
                href = link['href']
                if "hent_kunngjoring.jsp" in href or "hent_enhet.jsp" in href:
                    company_name = link.text.strip()
                    if len(company_name) < 2: continue
                    
                    full_link = f"https://w2.brreg.no/kunngjoring/{href}"
                    
                    if not article_exists(full_link):
                        class BrregEntry: pass
                        entry = BrregEntry()
                        entry.title = f"{stype['name'].upper()}: {company_name}"
                        entry.summary = f"Brønnøysundmelding for Telemark. Kategori: {stype['name']}."
                        entry.link = full_link
                        entry.published = date_to
                        
                        score, reason = analyze_relevance_with_ai(entry.title, entry.summary, stype['name'])
                        
                        if save_article(entry, "Brønnøysund", stype['name'], score, reason):
                            hits += 1
                                    
        except Exception as e:
            print(f"Brreg-feil ({stype['name']}): {e}")
            time.sleep(1) 
        
    return hits

def fetch_and_filter_news(keywords):
    new_hits = 0
    status_box = st.sidebar.empty()
    progress = st.sidebar.progress(0)
    
    # 1. BRØNNØYSUND
    status_box.text("Sjekker Brønnøysundregistrene...")
    brreg_hits = check_brreg()
    new_hits += brreg_hits

    # 2. RSS
    USER_AGENT = "Mozilla/5.0"
    for i, url in enumerate(RSS_SOURCES):
        status_box.text(f"Leser {url}...")
        try: 
            feed = feedparser.parse(url, agent=USER_AGENT)
            for entry in feed.entries:
                
                # --- SJEKK 1: ER SAKEN FERSK? (Tidspoliti) ---
                if not is_article_fresh(entry):
                    continue

                t = entry.title.lower()
                s = feed.feed.get('title', '').lower()
                l = entry.link.lower()
                if "telemarksavisa" in t or "telemarksavisa" in s or "ta.no" in l:
                    continue 

                raw_text = (entry.title + " " + getattr(entry, 'summary', '')).lower()
                
                # --- SJEKK 2: NØKKELORD (Regex) ---
                hit = None
                for k in keywords:
                    pattern = r"\b" + re.escape(k.lower()) + r"\b"
                    if re.search(pattern, raw_text):
                        hit = k
                        break
                
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
                st.session_state.last_hits_count = hits
                st.session_state.last_hits_time = get_norway_time().strftime("%H:%M")
                st.rerun()
            else: 
                st.info("Ingen nye treff.")
        
        st.divider()
        if st.button("🛠️ Test Slack"):
            try:
                class MockEntry: pass
                dummy = MockEntry()
                dummy.link = f"http://test-slack-{int(time.time())}.no"
                dummy.title = "TEST: Stor brannøvelse på Herøya"
                dummy.summary = "Dette er en test for Slack-varsling."
                dummy.published = "Nå"
                if save_article(dummy, "Systemtest", "Herøya", 95, "Test"):
                    st.toast("Test sendt!", icon="🚀")
                    time.sleep(1)
                    st.rerun()
            except Exception as e:
                st.error(f"Feil: {e}")

    # Autopilot
    if auto_run:
        if 'last_check' not in st.session_state:
            st.session_state.last_check = datetime.min
        
        if datetime.now() - st.session_state.last_check > timedelta(minutes=10):
            hits = fetch_and_filter_news(active_keywords)
            st.session_state.last_check = datetime.now()
            st.session_state.last_hits_count = hits
            st.session_state.last_hits_time = get_norway_time().strftime("%H:%M")
            st.rerun()

    # Visning
    if 'last_hits_count' in st.session_state and st.session_state.last_hits_count > 0:
        st.success(f"🚨 Siste søk (kl {st.session_state.last_hits_time}) fant **{st.session_state.last_hits_count}** nye saker!")

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

    if auto_run:
        next_run_server = st.session_state.last_check + timedelta(minutes=10)
        next_run_display = next_run_server + timedelta(hours=1)
        st.sidebar.info(f"💤 Neste sjekk: {next_run_display.strftime('%H:%M')}")
        time.sleep(30)
        st.rerun()

if __name__ == "__main__":
    main()
