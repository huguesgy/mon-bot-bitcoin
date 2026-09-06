import os
import re
import html
import time
from datetime import datetime

import requests
import feedparser
from google import genai
from google.genai import errors as genai_errors
from youtube_transcript_api import YouTubeTranscriptApi

# ==============================================================================
# 1. CONFIGURATION GLOBALE
# ==============================================================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Flux RSS réputés et institutionnels (100% gratuits)
RSS_FEEDS = [
    "https://cointelegraph.com/rss/tag/bitcoin",
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://bitcoinmagazine.com/.rss/full/",
    "https://cryptoslate.com/feed/",
    # Sources officielles/primaires : peu de volume, mais signal fort quand un fait apparaît
    "https://www.federalreserve.gov/feeds/press_monetary.xml",  # Décisions de taux, déclarations FOMC
    "https://www.sec.gov/news/pressreleases.rss",  # Actions/poursuites de la SEC
]

# ID des chaînes YouTube ciblées pour extraire automatiquement leur dernière vidéo
YOUTUBE_CHANNELS = [
    {"name": "Grand Angle Crypto", "channel_id": "UCqK_m6k_gq3_bO7J45c7xzg"},
    {"name": "Hasheur", "channel_id": "UChlTcWDE8gd4tsl_L727NrQ"},
]

# Prompt système : anti-bruit + couche pédagogique
# NB : le format utilise **mot** pour le gras -> converti en <b>mot</b> pour Telegram (voir to_telegram_html)
SYSTEM_PROMPT = """
Tu es un analyste qui explique le marché du Bitcoin (BTC) à quelqu'un qui s'intéresse à la finance
mais n'est pas encore à l'aise avec le vocabulaire et les mécanismes du secteur. Ta mission n'est pas
seulement de rapporter des faits : c'est de les rendre compréhensibles.

RÈGLES D'EXCLUSION STRICTES (à ignorer totalement) :
- Avis de traders anonymes, spéculations court terme, titres clickbait ("x100", "krach imminent").
- Redondances : si plusieurs sources traitent du même évènement, synthétise-le en un seul point.

CRITÈRES DE SÉLECTION (ne retenir que les faits matériels) :
1. Macroéconomie US & banques centrales (taux Fed, inflation CPI/PCE, liquidité mondiale, US10Y).
2. Flux institutionnels vérifiés (entrées/sorties nettes des ETF Spot, réserves de trésorerie).
3. Régulation contraignante (lois votées, poursuites judiciaires majeures SEC/CFTC).
4. Dérivés & liquidations (Funding Rate anormal, cascades de liquidations).

COMMENT EXPLIQUER (règle la plus importante — ne l'oublie jamais) :
Pour CHAQUE fait retenu, ne te contente pas de l'énoncer. Ajoute systématiquement :
- Le mécanisme : pourquoi/comment ce fait influence concrètement l'offre, la demande ou le prix du BTC.
  Écris comme si tu expliquais à quelqu'un qui découvre le sujet, pas à un autre analyste.
- Si un terme technique apparaît (funding rate, ETF spot, CPI, liquidité, short squeeze, etc.),
  explique-le en une phrase simple, sans jargon supplémentaire.
- Quand c'est pertinent, fais un rapprochement avec un évènement ou un mécanisme déjà connu
  (ex: "comme lors d'un resserrement monétaire classique...") pour ancrer la compréhension.

FORMAT DE RÉPONSE OBLIGATOIRE :
Utilise **mot** uniquement pour mettre du texte en gras. N'utilise aucun autre symbole de mise en forme
(pas de #, pas de markdown de titre, pas de tirets pour les titres).
Utilise la date fournie dans les informations brutes telle quelle, ne la déduis jamais toi-même.

**📊 BRIEFING BITCOIN — [date fournie]**

**🏷️ Catalyseurs :** #MotClé1 #MotClé2 #MotClé3

**⚡ En bref**
[2-3 phrases simples expliquant la tendance majeure et son impact sur le BTC]

**🔍 Ce qu'il s'est passé**
• **[Catégorie]** — [le fait], ce qui [le mécanisme : comment ça bouge le cours, en langage clair]
• **[Catégorie]** — [le fait], ce qui [le mécanisme]

**🧠 Pour comprendre**
[1 à 3 termes techniques utilisés plus haut, chacun expliqué en une phrase simple]

**⚙️ Indicateur clé**
• Funding Rate : [chiffre] → [neutre / surchauffe haussière / pression vendeuse] — [ce que ça signifie concrètement pour quelqu'un qui débute]

**🔗 Sources**
1. [Nom du média] : [lien]

Si absolument aucun fait matériel n'est détecté dans le lot, réponds exactement :
"AUCUN SIGNAL MAJEUR DÉTECTÉ POUR CE CRÉNEAU."
"""

# ==============================================================================
# 2. COLLECTE DES DONNÉES DE MARCHÉ
# ==============================================================================
def fetch_funding_rate():
    """
    Récupère le dernier funding rate BTCUSDT.
    Bybit en premier, puis OKX en secours : Bybit (et parfois Binance) bloquent les IP
    de certains hébergeurs cloud (dont GitHub Actions), ce qui peut rendre la donnée
    "Indisponible" sans qu'il y ait de bug dans le code.
    """
    # --- Tentative 1 : Bybit ---
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rate = float(data["result"]["list"][0]["fundingRate"]) * 100
        return f"{rate:+.4f}% sur 8 heures (Bybit)"
    except Exception as e:
        print(f"[!] Bybit indisponible ({e}), tentative via OKX...")

    # --- Tentative 2 (secours) : OKX ---
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rate = float(data["data"][0]["fundingRate"]) * 100
        return f"{rate:+.4f}% sur 8 heures (OKX)"
    except Exception as e:
        print(f"[!] Erreur récupération Funding Rate (OKX) : {e}")
        return "Indisponible"

# ==============================================================================
# 3. COLLECTE DES FLUX RSS
# ==============================================================================
def fetch_rss_news(limit_per_feed=3):
    """Extrait les derniers articles publiés depuis les flux RSS."""
    articles = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:limit_per_feed]:
                title = getattr(entry, "title", "Sans titre").strip()
                summary = getattr(entry, "summary", "").strip()
                link = getattr(entry, "link", "").strip()
                articles.append(f"- Titre: {title}\n  Résumé: {summary[:250]}...\n  Lien: {link}")
        except Exception as e:
            print(f"[!] Erreur lecture flux {feed_url} : {e}")
    return "\n".join(articles) if articles else "Aucun article disponible."

# ==============================================================================
# 4. TRANSCRIPTIONS YOUTUBE SANS API PAYANTE
# ==============================================================================
def fetch_youtube_transcripts():
    """Récupère les sous-titres de la dernière vidéo de chaque chaîne ciblée."""
    transcripts = []
    for ch in YOUTUBE_CHANNELS:
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={ch['channel_id']}"
        try:
            feed = feedparser.parse(feed_url)
            if not feed.entries:
                continue

            latest_video = feed.entries[0]
            video_id = latest_video.yt_videoid
            video_title = latest_video.title
            video_link = latest_video.link

            transcript_data = YouTubeTranscriptApi.get_transcript(video_id, languages=['fr', 'en'])
            full_text = " ".join([item['text'] for item in transcript_data])

            transcripts.append(
                f"- Chaîne: {ch['name']}\n  Titre: {video_title}\n  Lien: {video_link}\n"
                f"  Extrait Transcription: {full_text[:2000]}..."
            )
        except Exception as e:
            print(f"[i] Transcription non disponible pour {ch['name']} : {e}")

    return "\n".join(transcripts) if transcripts else "Aucune vidéo récente exploitable."

# ==============================================================================
# 5. SYNTHÈSE & ANALYSE AVEC GEMINI
# ==============================================================================
def analyze_with_gemini(raw_context, max_retries=3, base_delay=20):
    """
    Envoie l'agrégat d'informations au modèle pour filtrage, explication et mise en forme.
    Réessaie automatiquement en cas de saturation temporaire de Gemini (erreur 503 UNAVAILABLE,
    fréquente sur le tier gratuit aux heures de pointe). N'insiste pas sur les erreurs côté requête
    (clé invalide, quota dépassé, etc.), qui ne se résoudront pas en réessayant.
    """
    if not GEMINI_API_KEY:
        raise ValueError("La variable GEMINI_API_KEY est manquante.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    full_prompt = f"{SYSTEM_PROMPT}\n\n=== INFORMATIONS BRUTES À TRAITER ===\n{raw_context}"

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model="gemini-3.6-flash",
                contents=full_prompt
            )
            return response.text
        except genai_errors.ServerError as e:
            last_error = e
            wait = base_delay * attempt
            print(f"[!] Gemini indisponible (tentative {attempt}/{max_retries}) : {e}")
            if attempt < max_retries:
                print(f"    Nouvel essai dans {wait}s...")
                time.sleep(wait)
        except genai_errors.ClientError as e:
            # Erreur côté requête (clé invalide, quota, prompt trop long...) : inutile de réessayer
            raise

    raise RuntimeError(f"Gemini indisponible après {max_retries} tentatives : {last_error}")

# ==============================================================================
# 6. MISE EN FORME TELEGRAM (HTML)
# ==============================================================================
def to_telegram_html(text):
    """
    Convertit **mot** (demandé au modèle) en <b>mot</b>, et échappe le reste
    pour un envoi sûr avec parse_mode='HTML'.
    """
    escaped = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)

# ==============================================================================
# 7. ENVOI DE L'ALERTE SUR TELEGRAM
# ==============================================================================
def send_telegram(message_text):
    """Expédie le message formaté (HTML) sur votre canal ou chat privé Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Identifiants Telegram manquants. Message non envoyé.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": to_telegram_html(message_text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            print("[✓] Briefing envoyé avec succès sur Telegram !")
        else:
            print(f"[!] Erreur Telegram ({res.status_code}) : {res.text}")
    except Exception as e:
        print(f"[!] Exception lors de l'envoi Telegram : {e}")

# ==============================================================================
# 8. EXÉCUTION DU PIPELINE
# ==============================================================================
def main():
    print("--> 1. Collecte des métriques et des actualités...")
    date_str = datetime.now().strftime("%d/%m/%Y — %H:%M")
    funding = fetch_funding_rate()
    rss_news = fetch_rss_news()
    yt_news = fetch_youtube_transcripts()

    raw_payload = f"""
[DATE ET HEURE DU BRIEFING — à recopier telle quelle dans le titre]
{date_str}

[MÉTRIQUES TECHNIQUES]
- Funding Rate BTCUSDT : {funding}

[FLUX ACTUALITÉS RSS]
{rss_news}

[ANALYSES VIDÉOS YOUTUBE]
{yt_news}
"""

    print("--> 2. Analyse par Gemini...")
    try:
        report = analyze_with_gemini(raw_payload)
    except Exception as e:
        print(f"[!] Impossible de générer le briefing : {e}")
        send_telegram(
            "⚠️ Le briefing Bitcoin n'a pas pu être généré (service Gemini temporairement "
            "indisponible ou saturé). Nouvelle tentative au prochain cycle."
        )
        return

    print("\n--- RÉSULTAT DU RAPPORT ---\n")
    print(report)

    print("\n--> 3. Envoi du briefing...")
    # Envoie systématiquement le rapport généré par Gemini
send_telegram(report)

if __name__ == "__main__":
    main()
