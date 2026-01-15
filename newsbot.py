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
    
    # --- STRENGERE INSTRUKS (Kort tekst) ---
    prompt = f"""
    Vurder sak for Telemarksavisa. Søkeord: '{keyword}'. 
    Tittel: {clean_title}
    Ingress: {clean_summary}
    
    VIKTIG: Begrunnelsen skal være ekstremt kort (maks 10-15 ord).
    Format: 
    Score: [tall 0-100] 
    Begrunnelse: [Kort setning]
    """
    
    try:
        response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
        content = response.choices[0].message.content