import os
import requests
import feedparser
from google import genai
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
]

# ID des chaînes YouTube ciblées pour extraire automatiquement leur dernière vidéo
# Exemples : Grand Angle Crypto, Hasheur
YOUTUBE_CHANNELS = [
    {"name": "Grand Angle Crypto", "channel_id": "UCqK_m6k_gq3_bO7J45c7xzg"},
]

# Prompt système : instructions rigoureuses anti-bruit
SYSTEM_PROMPT = """
Tu es un analyste macroéconomique et de marché senior, dédié exclusivement au Bitcoin (BTC).
Ta mission : analyser les données brutes (articles RSS, métriques de marché, transcriptions vidéo) et produire un briefing clair et actionnable.

RÈGLES D'EXCLUSION STRICTES (À IGNORER TOTALEMENT) :
- Les avis de traders anonymes, spéculations à court terme et titres clickbait ("x100", "krach imminent").
- Les redondances : si plusieurs sources traitent du même événement, synthétise-le en un seul point.

CRITÈRES DE SÉLECTION (RETENIR UNIQUEMENT LES FAITS MATÉRIELS) :
1. Macroéconomie US & Banques centrales (taux Fed, inflation CPI/PCE, liquidité mondiale, US10Y).
2. Flux institutionnels vérifiés (entrées/sorties nettes des ETF Spot, réserves de trésorerie).
3. Régulation contraignante (lois votées, poursuites judiciaires majeures SEC/CFTC).
4. Dérivés & liquidations (Funding Rate anormal, cascades de liquidations).

FORMAT DE RÉPONSE OBLIGATOIRE :
📊 BRIEFING BITCOIN — SESSION DE MARCHÉ
--------------------------------------------------
🏷️ CATALYSEURS : #MotClé1 #MotClé2 #MotClé3

⚡ EN BREF :
[2 phrases claires expliquant la tendance majeure et l'impact directionnel sur la liquidité du BTC].

🔍 FAITS MAJEURS RETENUS :
• [Catégorie] : [Explication factuelle et conséquence mécanique sur l'offre ou la demande].
• [Catégorie] : [Deuxième point majeur, si pertinent].
• [Catégorie] : [Troisième point majeur, si pertinent].

⚙️ INDICATEUR TECHNIQUE CLÉ :
• Taux de Financement Binance (Funding Rate) : [Interprétation du chiffre fourni : Neutre, Surchauffe haussière, ou Pression vendeuse].

🔗 SOURCES PRINCIPALES :
1. [Nom du média] : [Lien URL direct]

Si absolument aucun fait matériel n'est détecté dans le lot, réponds exactement : "AUCUN SIGNAL MAJEUR DÉTECTÉ POUR CE CRÉNEAU."
"""

# ==============================================================================
# 2. COLLECTE DES DONNÉES DE MARCHÉ (BINANCE ENDPOINT PUBLIC)
# ==============================================================================
def fetch_funding_rate():
    """Récupère le dernier taux de financement BTCUSDT via Bybit (ouvert et non bloqué sur GitHub)."""
    url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rate = float(data["result"]["list"][0]["fundingRate"]) * 100
        return f"{rate:+.4f}% sur 8 heures (Bybit)"
    except Exception as e:
        print(f"[!] Erreur récupération Funding Rate : {e}")
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

            # Télécharge les sous-titres (FR ou EN)
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
# 5. SYNTHÈSE & ANALYSE AVEC GEMINI 2.5 FLASH
# ==============================================================================
def analyze_with_gemini(raw_context):
    """Envoie l'agrégat d'informations au modèle pour filtrage et mise en forme."""
    if not GEMINI_API_KEY:
        raise ValueError("La variable GEMINI_API_KEY est manquante.")
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    full_prompt = f"{SYSTEM_PROMPT}\n\n=== INFORMATIONS BRUTES À TRAITER ===\n{raw_context}"
    
    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=full_prompt
    )
    return response.text

# ==============================================================================
# 6. ENVOI DE L'ALERTE SUR TELEGRAM
# ==============================================================================
def send_telegram(message_text):
    """Expédie le message formaté sur votre canal ou chat privé Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Identifiants Telegram manquants. Message non envoyé.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
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
# 7. EXÉCUTION DU PIPELINE
# ==============================================================================
def main():
    print("--> 1. Collecte des métriques et des actualités...")
    funding = fetch_funding_rate()
    rss_news = fetch_rss_news()
    yt_news = fetch_youtube_transcripts()

    raw_payload = f"""
[MÉTRIQUES TECHNIQUES]
- Funding Rate BTCUSDT (Binance) : {funding}

[FLUX ACTUALITÉS RSS]
{rss_news}

[ANALYSES VIDÉOS YOUTUBE]
{yt_news}
"""

    print("--> 2. Analyse par Gemini 2.5 Flash...")
    report = analyze_with_gemini(raw_payload)
    print("\n--- RÉSULTAT DU RAPPORT ---\n")
    print(report)

    print("\n--> 3. Envoi du briefing...")
    if "AUCUN SIGNAL MAJEUR DÉTECTÉ" in report:
        print("[i] Aucun événement matériel détecté. Envoi ignoré.")
    else:
        send_telegram(report)

if __name__ == "__main__":
    main()
