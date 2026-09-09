import os
import re
import html
import time
import random
import sys
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

# Cascade de modèles Gemini (free tier) : si le premier est saturé, on passe au suivant.
# Ordre : du plus récent (souvent moins sollicité) au plus éprouvé.
GEMINI_MODELS = [
    "gemini-3.8-flash",   # sorti le 02/09/2026, le plus récent → souvent moins de trafic
    "gemini-3.7-flash",   # workhorse d'août 2026
    "gemini-3.6-flash",   # ton modèle actuel, le plus éprouvé
]

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

# Prompt système : anti-bruit + couche pédagogique + format enrichi
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

LE RÉFLEXE DU TRADER FONDAMENTAL (à appliquer sur le fait le plus significatif du jour) :
Un trader fondamental sérieux ne s'arrête jamais à "ce mouvement/cette nouvelle a l'air intéressant".
Il vérifie systématiquement s'il y a une VRAIE DIVERGENCE exploitable : est-ce que ce fait change
réellement l'équilibre du marché (nouveau catalyseur, rupture avec la tendance), ou ne fait-il que
confirmer une direction déjà connue et déjà intégrée par tout le monde — auquel cas il n'y a pas
de réel edge, même si ça semble intéressant en surface ? Sois honnête même quand la conclusion est
"il n'y a pas de signal fort ici", c'est aussi précieux à savoir que l'inverse. Ne donne JAMAIS de
verdict d'action (achat/vente/entrée en position) : le but est d'apprendre à raisonner ainsi, pas
de dire quoi faire.

FORMAT DE RÉPONSE OBLIGATOIRE :
Utilise **mot** pour le gras, `mot` pour un style "code" à chasse fixe (réservé aux chiffres clés et
aux termes techniques du glossaire), et un bloc ```...``` uniquement pour l'en-tête tout en haut.
N'utilise aucun autre symbole de mise en forme.
Reproduis EXACTEMENT les lignes de séparation (━━━━━━━━━━━━━━━━━━━━) telles quelles, sans les modifier
ni les résumer.
Utilise la date fournie dans les informations brutes telle quelle, ne la déduis jamais toi-même.
Pour le bloc Dashboard, recopie les données fournies (prix, Fear & Greed, Funding Rate) telles
quelles — ne les invente pas, ne les arrondis pas autrement.

```
════════════════════════════
  📊 BRIEFING BITCOIN
  [date fournie] · UTC
════════════════════════════
```

**💰 BTC** `[prix fourni]` **·** `[variation 24h fournie]`
**📊 F&G** `[valeur F&G fournie]` [emoji F&G fourni]
**📈 Funding** `[funding rate fourni]` **·** [neutre / surchauffe / pression vendeuse]

━━━━━━━━━━━━━━━━━━━━

**🏷️ Catalyseurs :** #MotClé1 #MotClé2 #MotClé3

━━━━━━━━━━━━━━━━━━━━

**⚡ En bref**
[2-3 phrases simples expliquant la tendance majeure et son impact sur le BTC]

━━━━━━━━━━━━━━━━━━━━

**🔍 Ce qu'il s'est passé**
• **[Catégorie]** — [le fait], ce qui [le mécanisme : comment ça bouge le cours, en langage clair]
• **[Catégorie]** — [le fait], ce qui [le mécanisme]

━━━━━━━━━━━━━━━━━━━━

**🎓 Réflexe du trader fondamental**
[Pour le fait le plus significatif : y a-t-il une vraie divergence exploitable, ou ce fait ne fait-il
que confirmer une tendance déjà connue et déjà intégrée par le marché ? Explique le raisonnement,
sans donner de verdict d'action.]

━━━━━━━━━━━━━━━━━━━━

**🧠 Pour comprendre**
• `[Terme]` : [définition simple]
[1 à 3 termes techniques utilisés plus haut, chacun expliqué en une phrase simple, terme en style code]

━━━━━━━━━━━━━━━━━━━━

**⚙️ Indicateur clé**
• Funding Rate : `[chiffre]` → [neutre / surchauffe haussière / pression vendeuse] — [ce que ça signifie concrètement pour quelqu'un qui débute]

━━━━━━━━━━━━━━━━━━━━

**🔗 Sources**
1. [Nom du média] : [lien]

━━━━━━━━━━━━━━━━━━━━

⚠️ Ce briefing est éducatif et ne constitue pas un conseil financier. Faites vos propres recherches (DYOR) avant toute décision.

Si absolument aucun fait matériel n'est détecté dans le lot, réponds exactement :
"AUCUN SIGNAL MAJEUR DÉTECTÉ POUR CE CRÉNEAU."
"""

# ==============================================================================
# 2. COLLECTE DES DONNÉES DE MARCHÉ
# ==============================================================================
def fetch_btc_price():
    """
    Récupère le prix actuel du BTC en USD et sa variation sur 24h via CoinGecko.
    API 100% gratuite, sans clé, sans inscription.
    """
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {
            "ids": "bitcoin",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()["bitcoin"]
        price = data["usd"]
        change_24h = data["usd_24h_change"]
        return f"${price:,.0f}", f"{change_24h:+.1f}%"
    except Exception as e:
        print(f"[!] Prix BTC indisponible : {e}")
        return "Indisponible", "N/A"


def fetch_fear_greed():
    """
    Récupère l'indice Fear & Greed crypto (0 = peur extrême, 100 = euphorie).
    API 100% gratuite via alternative.me, sans clé.
    Retourne (valeur_str, label, emoji) ex: ("72", "Greed", "🟢")
    """
    EMOJI_MAP = {
        (0, 25): "🔴",    # Extreme Fear
        (25, 45): "🟠",   # Fear
        (45, 55): "🟡",   # Neutral
        (55, 75): "🟢",   # Greed
        (75, 101): "🟣",  # Extreme Greed
    }
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()["data"][0]
        value = int(data["value"])
        label = data["value_classification"]
        emoji = "⚪"
        for (low, high), em in EMOJI_MAP.items():
            if low <= value < high:
                emoji = em
                break
        return f"{value}/100", label, emoji
    except Exception as e:
        print(f"[!] Fear & Greed indisponible : {e}")
        return "N/A", "Indisponible", "⚪"


def fetch_funding_rate():
    """
    Récupère le dernier funding rate BTCUSDT.
    OKX en premier : Bybit bloque systématiquement (403) les IP des serveurs GitHub
    Actions (confirmé en usage réel), donc OKX évite une attente inutile à chaque run.
    Bybit reste en second recours au cas où ce blocage soit levé un jour.
    """
    # --- Tentative 1 : OKX ---
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rate = float(data["data"][0]["fundingRate"]) * 100
        return f"{rate:+.4f}% sur 8 heures (OKX)"
    except Exception as e:
        print(f"[!] OKX indisponible ({e}), tentative via Bybit...")

    # --- Tentative 2 (secours) : Bybit ---
    try:
        url = "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        rate = float(data["result"]["list"][0]["fundingRate"]) * 100
        return f"{rate:+.4f}% sur 8 heures (Bybit)"
    except Exception as e:
        print(f"[!] Erreur récupération Funding Rate (Bybit) : {e}")
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
    yt_api = YouTubeTranscriptApi()  # API >= 1.0 : instance, plus de méthode de classe get_transcript()
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

            fetched = yt_api.fetch(video_id, languages=['fr', 'en'])
            full_text = " ".join(snippet.text for snippet in fetched)

            transcripts.append(
                f"- Chaîne: {ch['name']}\n  Titre: {video_title}\n  Lien: {video_link}\n"
                f"  Extrait Transcription: {full_text[:2000]}..."
            )
        except Exception as e:
            print(f"[i] Transcription non disponible pour {ch['name']} : {e}")

    return "\n".join(transcripts) if transcripts else "Aucune vidéo récente exploitable."

# ==============================================================================
# 5. SYNTHÈSE & ANALYSE AVEC GEMINI (cascade multi-modèle + retry agressif)
# ==============================================================================
def _retry_with_backoff(client, model_name, prompt, max_retries=3, base_delay=20):
    """
    Tente d'appeler un modèle Gemini donné avec backoff exponentiel + jitter.
    Retourne le texte généré, ou lève l'exception si toutes les tentatives échouent.

    Délais approximatifs : 20s → 60s → 120s (+ jitter ±30% à chaque fois).
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            return response.text
        except genai_errors.ServerError as e:
            last_error = e
            if attempt < max_retries:
                # Backoff exponentiel : base * 2^(attempt-1) → 20, 60, 120
                raw_delay = base_delay * (2 ** (attempt - 1))
                # Plafonner à 5 minutes pour ne pas attendre une éternité
                capped_delay = min(raw_delay, 300)
                # Jitter ±30% pour désynchroniser les requêtes concurrentes
                jitter = capped_delay * random.uniform(-0.3, 0.3)
                wait = max(5, capped_delay + jitter)  # au moins 5 secondes
                print(f"[!] {model_name} indisponible (tentative {attempt}/{max_retries}) : {e}")
                print(f"    Nouvel essai dans {wait:.0f}s...")
                time.sleep(wait)
            else:
                print(f"[!] {model_name} indisponible (tentative {attempt}/{max_retries}) : {e}")
        except genai_errors.ClientError:
            # Erreur côté requête (clé invalide, quota, prompt trop long...) : inutile de réessayer
            raise

    raise last_error  # type: ignore[misc]


def analyze_with_gemini(raw_context):
    """
    Envoie l'agrégat d'informations au modèle pour filtrage, explication et mise en forme.

    Stratégie de résilience (cascade multi-modèle) :
    Pour chaque modèle dans GEMINI_MODELS, on tente 3 appels avec backoff exponentiel.
    Si un modèle échoue après 3 tentatives (saturation, maintenance...), on passe au suivant.

    Chaîne : gemini-3.8-flash → gemini-3.7-flash → gemini-3.6-flash
    Temps total max : ~18 minutes (largement acceptable pour un cron espacé de plusieurs heures).

    N'insiste pas sur les erreurs côté requête (clé invalide, quota dépassé, etc.), qui ne se
    résoudront pas en réessayant — celles-ci remontent immédiatement.
    """
    if not GEMINI_API_KEY:
        raise ValueError("La variable GEMINI_API_KEY est manquante.")

    client = genai.Client(api_key=GEMINI_API_KEY)
    full_prompt = f"{SYSTEM_PROMPT}\n\n=== INFORMATIONS BRUTES À TRAITER ===\n{raw_context}"

    last_error = None
    for model_name in GEMINI_MODELS:
        print(f"[→] Tentative avec {model_name}...")
        try:
            result = _retry_with_backoff(client, model_name, full_prompt)
            print(f"[✓] Réponse obtenue via {model_name}")
            return result
        except genai_errors.ClientError:
            # Erreur non-récupérable (clé API, quota, etc.) : on remonte tout de suite
            raise
        except Exception as e:
            last_error = e
            print(f"[✗] {model_name} épuisé après 3 tentatives. Passage au modèle suivant...")

    raise RuntimeError(
        f"Tous les modèles Gemini sont indisponibles après cascade complète "
        f"({' → '.join(GEMINI_MODELS)}). Dernière erreur : {last_error}"
    )

# ==============================================================================
# 6. MISE EN FORME TELEGRAM (HTML)
# ==============================================================================
SECTION_HEADERS = [
    "⚡ En bref",
    "🔍 Ce qu'il s'est passé",
    "🎓 Réflexe du trader fondamental",
    "🧠 Pour comprendre",
    "⚙️ Indicateur clé",
    "🔗 Sources",
]

def _wrap_header_block(text):
    """
    Garantit que le bandeau d'en-tête (════/titre/date/════) est bien encadré par
    des ``` pour être rendu en police fixe, même si le modèle a oublié cette fois-ci.
    """
    if text.lstrip().startswith("```"):
        return text  # déjà encadré, on ne touche à rien
    match = re.match(r"(═{5,}(?:\n.*)*?\n═{5,})", text.lstrip())
    if not match:
        return text
    block = match.group(1)
    return text.replace(block, f"```{block}```", 1)

def _ensure_bold_headers(text):
    """
    Garantit que les titres de section sont en gras, même si le modèle a oublié
    les ** autour cette fois-ci (la fidélité au format n'est jamais garantie à 100%).
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in SECTION_HEADERS:
            lines[i] = f"**{stripped}**"
        elif stripped.startswith("🏷️ Catalyseurs") and not stripped.startswith("**") and ":" in stripped:
            label, rest = stripped.split(":", 1)
            lines[i] = f"**{label.strip()} :**{rest}"
    return "\n".join(lines)

def to_telegram_html(text):
    """
    Convertit la mise en forme légère demandée au modèle en HTML Telegram :
    - ```bloc``` -> <pre>bloc</pre>   (en-tête façon terminal)
    - **mot**    -> <b>mot</b>       (gras)
    - `mot`      -> <code>mot</code> (chasse fixe, pour les chiffres/termes clés)
    Les titres de section et le bandeau d'en-tête sont fiabilisés en amont (voir
    _wrap_header_block / _ensure_bold_headers) car on ne peut pas garantir que le
    modèle respecte le balisage à 100% à chaque génération.
    Le texte est d'abord échappé pour un envoi sûr avec parse_mode='HTML'.
    """
    text = _wrap_header_block(text)
    text = _ensure_bold_headers(text)
    escaped = html.escape(text)
    escaped = re.sub(r"```(.+?)```", r"<pre>\1</pre>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"`(.+?)`", r"<code>\1</code>", escaped)
    return escaped

# ==============================================================================
# 7. ENVOI DE L'ALERTE SUR TELEGRAM
# ==============================================================================
def send_telegram(message_text):
    """
    Expédie un message formaté (HTML) sur votre canal ou chat privé Telegram.
    Retourne True si Telegram a confirmé la réception, False sinon — pour que main()
    puisse faire échouer le job GitHub Actions de façon visible en cas de problème.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Identifiants Telegram manquants. Message non envoyé.")
        return False

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
            print("[✓] Morceau envoyé avec succès sur Telegram !")
            return True
        else:
            print(f"[!] Erreur Telegram ({res.status_code}) : {res.text}")
            return False
    except Exception as e:
        print(f"[!] Exception lors de l'envoi Telegram : {e}")
        return False

SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━"
TELEGRAM_SAFE_CHUNK_LEN = 3200  # marge sous la limite dure de 4096 caractères de Telegram,
                                # pour absorber l'inflation due aux balises HTML (<b>, <code>, <pre>)

def split_report_into_chunks(raw_text, max_len=TELEGRAM_SAFE_CHUNK_LEN):
    """
    Découpe le rapport brut (avant conversion HTML) en plusieurs morceaux qui tiennent
    chacun sous la limite Telegram, en coupant UNIQUEMENT au niveau des séparateurs de
    section (━━━) pour ne jamais couper une balise ** / ` / ``` en deux morceaux.
    """
    sections = raw_text.split(SECTION_DIVIDER)
    chunks, current = [], ""
    for i, section in enumerate(sections):
        piece = section if i == 0 else SECTION_DIVIDER + section
        if current and len(current) + len(piece) > max_len:
            chunks.append(current)
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current)
    return chunks

def send_telegram_report(report_text):
    """
    Envoie le briefing sur Telegram, en le découpant en plusieurs messages si besoin.
    Retourne True seulement si TOUS les morceaux ont été livrés avec succès.
    """
    chunks = split_report_into_chunks(report_text)
    all_ok = True
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1 and i < len(chunks) - 1:
            chunk += "\n\n(suite dans le message suivant...)"
        all_ok = send_telegram(chunk) and all_ok
    return all_ok

# ==============================================================================
# 8. EXÉCUTION DU PIPELINE
# ==============================================================================
def main():
    print("=" * 60)
    print("  BITCOIN BRIEFING BOT — Démarrage du pipeline")
    print("=" * 60)

    # --- Étape 1 : collecte de toutes les données ---
    print("\n--> 1. Collecte des métriques de marché...")
    date_str = datetime.now().strftime("%d/%m/%Y — %H:%M")

    btc_price, btc_change = fetch_btc_price()
    print(f"    Prix BTC : {btc_price} ({btc_change})")

    fg_value, fg_label, fg_emoji = fetch_fear_greed()
    print(f"    Fear & Greed : {fg_value} — {fg_label} {fg_emoji}")

    funding = fetch_funding_rate()
    print(f"    Funding Rate : {funding}")

    print("\n--> 2. Collecte des actualités (RSS + YouTube)...")
    rss_news = fetch_rss_news()
    yt_news = fetch_youtube_transcripts()

    # --- Étape 2 : assemblage du payload brut pour Gemini ---
    raw_payload = f"""
[DATE ET HEURE DU BRIEFING — à recopier telle quelle dans le titre]
{date_str}

[MÉTRIQUES TECHNIQUES — à recopier telles quelles dans le dashboard]
- Prix BTC : {btc_price} ({btc_change} sur 24h)
- Funding Rate BTCUSDT : {funding}
- Fear & Greed Index : {fg_value} — {fg_label} {fg_emoji}

[FLUX ACTUALITÉS RSS]
{rss_news}

[ANALYSES VIDÉOS YOUTUBE]
{yt_news}
"""

    # --- Étape 3 : analyse par Gemini (cascade multi-modèle) ---
    print("\n--> 3. Analyse par Gemini (cascade multi-modèle)...")
    try:
        report = analyze_with_gemini(raw_payload)
    except Exception as e:
        print(f"[!] Impossible de générer le briefing : {e}")
        send_telegram(
            "⚠️ Le briefing Bitcoin n'a pas pu être généré.\n\n"
            f"Modèles tentés : {', '.join(GEMINI_MODELS)}\n"
            "Raison probable : saturation temporaire du service Gemini (free tier).\n"
            "Nouvelle tentative au prochain cycle."
        )
        sys.exit(1)  # Run visible en échec (croix rouge) : Gemini a posé problème

    print("\n--- RÉSULTAT DU RAPPORT ---\n")
    print(report)

    # --- Étape 4 : envoi sur Telegram ---
    print("\n--> 4. Envoi du briefing sur Telegram...")
    if "AUCUN SIGNAL MAJEUR DÉTECTÉ" in report:
        print("[i] Aucun événement matériel détecté. Envoi ignoré (comportement normal, pas une erreur).")
        return

    if not send_telegram_report(report):
        print("[!] Le briefing a été généré mais n'a pas pu être livré (entièrement) sur Telegram.")
        sys.exit(1)  # Run visible en échec (croix rouge) : Telegram a refusé/échoué l'envoi

    print("\n[✓] Pipeline terminé avec succès !")

if __name__ == "__main__":
    main()
