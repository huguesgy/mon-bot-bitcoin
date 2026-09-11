import os
import re
import html
import time
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

# Flux RSS réputés et institutionnels (100% gratuits), avec un niveau de fiabilité par source
# (logique inspirée de World Monitor) :
#   Tier 1 = source officielle/primaire (gouvernement, banque centrale) — la plus haute confiance
#   Tier 2 = média crypto établi, réputation solide, souvent premier à rapporter
#   Tier 3 = média crypto spécialisé/niche — fiable mais à corroborer si isolé
RSS_FEEDS = [
    {"name": "Federal Reserve", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "tier": 1},
    {"name": "SEC", "url": "https://www.sec.gov/news/pressreleases.rss", "tier": 1},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss/tag/bitcoin", "tier": 2},
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml", "tier": 2},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/.rss/full/", "tier": 3},
    {"name": "CryptoSlate", "url": "https://cryptoslate.com/feed/", "tier": 3},
]

# ID des chaînes YouTube ciblées — Tier 4 : analyse/opinion indépendante, pas du journalisme,
# à traiter comme un point de vue à évaluer, jamais comme un fait établi en soi.
YOUTUBE_CHANNELS = [
    {"name": "Grand Angle Crypto", "channel_id": "UCqK_m6k_gq3_bO7J45c7xzg", "tier": 4},
    {"name": "Hasheur", "channel_id": "UChlTcWDE8gd4tsl_L727NrQ", "tier": 4},
]

TIER_LABELS = {
    1: "Tier 1 — source officielle/primaire",
    2: "Tier 2 — média établi",
    3: "Tier 3 — média spécialisé/niche",
    4: "Tier 4 — analyse indépendante (opinion, pas du journalisme)",
}

# Prompt système : anti-bruit + couche pédagogique
# NB : le format utilise **mot** pour le gras -> converti en <b>mot</b> pour Telegram (voir to_telegram_html)
SYSTEM_PROMPT = """
Tu es un analyste qui explique le marché du Bitcoin (BTC) à quelqu'un qui s'intéresse à la finance
mais n'est pas encore à l'aise avec le vocabulaire et les mécanismes du secteur. Ta mission n'est pas
seulement de rapporter des faits : c'est de les rendre compréhensibles.

RÈGLES D'EXCLUSION STRICTES (à ignorer totalement) :
- Avis de traders anonymes, spéculations court terme, titres clickbait ("x100", "krach imminent").
- Redondances : si plusieurs sources traitent du même évènement, synthétise-le en un seul point.

PONDÉRATION PAR FIABILITÉ DE SOURCE (chaque article/vidéo est étiqueté avec son tier) :
- Tier 1 (officiel/primaire) : la plus haute confiance, un seul Tier 1 suffit pour retenir un fait.
- Tier 2 (média établi) : fiable, traite comme un fait sauf signe contraire.
- Tier 3 (média spécialisé/niche) : traite avec un peu plus de prudence. Si un fait Tier 3 n'est
  corroboré par aucune source Tier 1 ou 2, tu peux quand même le retenir s'il est important, mais
  garde un ton légèrement plus prudent dans sa formulation ("selon [source]..." plutôt qu'affirmatif).
- Tier 4 (analyse YouTube indépendante) : ce n'est PAS une source factuelle vérifiée, c'est un point
  de vue individuel. Ne présente JAMAIS son contenu comme un fait établi. Utilise-le uniquement pour
  illustrer un raisonnement (notamment dans "Réflexe du trader fondamental"), jamais comme preuve
  d'un évènement de marché.
En cas de conflit entre sources sur un même fait, fais confiance au tier le plus élevé.

CRITÈRES DE SÉLECTION (ne retenir que les faits matériels) :
1. Macroéconomie US & banques centrales (taux Fed, inflation CPI/PCE, liquidité mondiale, US10Y).
2. Flux institutionnels vérifiés (entrées/sorties nettes des ETF Spot, réserves de trésorerie).
3. Régulation contraignante (lois votées, poursuites judiciaires majeures SEC/CFTC).
4. Dérivés & liquidations (Funding Rate anormal, cascades de liquidations).

LONGUEUR (contrainte technique importante) :
Telegram refuse tout message de plus de 4096 caractères, et le briefing est découpé en plusieurs
messages quand il dépasse ce seuil — ce qui casse la lecture. Vise environ 3000 à 3500 caractères
au total pour l'ensemble du briefing. Pour tenir cet objectif sans perdre en clarté : retiens au
maximum 2 faits dans "Ce qu'il s'est passé" (3 seulement si un troisième est vraiment indispensable),
et formule chaque explication de façon dense et directe plutôt que développée sur plusieurs phrases.
La clarté prime sur l'exhaustivité : mieux vaut 2 faits bien expliqués que 3 faits expédiés.

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

```
════════════════════════════
  BRIEFING BITCOIN
  [date fournie] · UTC
════════════════════════════
```

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
1. [Nom du média] (Tier [n]) : [lien]

Si absolument aucun fait matériel n'est détecté dans le lot, réponds exactement :
"AUCUN SIGNAL MAJEUR DÉTECTÉ POUR CE CRÉNEAU."
"""

# ==============================================================================
# 2. COLLECTE DES DONNÉES DE MARCHÉ
# ==============================================================================
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
    """Extrait les derniers articles publiés depuis les flux RSS, étiquetés par tier de fiabilité."""
    articles = []
    for feed in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:limit_per_feed]:
                title = getattr(entry, "title", "Sans titre").strip()
                summary = getattr(entry, "summary", "").strip()
                link = getattr(entry, "link", "").strip()
                articles.append(
                    f"- [{feed['name']} — {TIER_LABELS[feed['tier']]}]\n"
                    f"  Titre: {title}\n  Résumé: {summary[:250]}...\n  Lien: {link}"
                )
        except Exception as e:
            print(f"[!] Erreur lecture flux {feed['url']} : {e}")
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
                f"- [Chaîne: {ch['name']} — {TIER_LABELS[ch['tier']]}]\n"
                f"  Titre: {video_title}\n  Lien: {video_link}\n"
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
def send_telegram(message_text, reply_to_message_id=None):
    """
    Expédie un message formaté (HTML) sur votre canal ou chat privé Telegram.
    Si reply_to_message_id est fourni, le message est envoyé comme réponse à ce message
    (utilisé pour "enfiler" les morceaux d'un briefing découpé, au lieu de deux bulles
    déconnectées l'une de l'autre).
    Retourne l'ID du message envoyé (int) si succès, None sinon — pour que main() puisse
    faire échouer le job GitHub Actions de façon visible en cas de problème.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] Identifiants Telegram manquants. Message non envoyé.")
        return None

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": to_telegram_html(message_text),
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True  # envoie quand même si le message d'origine a disparu

    try:
        res = requests.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            print("[✓] Morceau envoyé avec succès sur Telegram !")
            return res.json()["result"]["message_id"]
        else:
            print(f"[!] Erreur Telegram ({res.status_code}) : {res.text}")
            return None
    except Exception as e:
        print(f"[!] Exception lors de l'envoi Telegram : {e}")
        return None

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
    Chaque morceau est envoyé en réponse au précédent (thread Telegram), pour que
    plusieurs messages restent visuellement liés au lieu de deux bulles déconnectées.
    Retourne True seulement si TOUS les morceaux ont été livrés avec succès.
    """
    chunks = split_report_into_chunks(report_text)
    all_ok = True
    previous_id = None
    for i, chunk in enumerate(chunks):
        if len(chunks) > 1 and i < len(chunks) - 1:
            chunk += "\n\n(suite ci-dessous ⤵️)"
        msg_id = send_telegram(chunk, reply_to_message_id=previous_id)
        all_ok = all_ok and (msg_id is not None)
        if msg_id is not None:
            previous_id = msg_id
    return all_ok

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
        sys.exit(1)  # Run visible en échec (croix rouge) : Gemini a posé problème

    print("\n--- RÉSULTAT DU RAPPORT ---\n")
    print(report)

    print("\n--> 3. Envoi du briefing...")
    if "AUCUN SIGNAL MAJEUR DÉTECTÉ" in report:
        print("[i] Aucun événement matériel détecté. Envoi ignoré (comportement normal, pas une erreur).")
        return

    if not send_telegram_report(report):
        print("[!] Le briefing a été généré mais n'a pas pu être livré (entièrement) sur Telegram.")
        sys.exit(1)  # Run visible en échec (croix rouge) : Telegram a refusé/échoué l'envoi

if __name__ == "__main__":
    main()
