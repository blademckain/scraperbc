import re
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


BASE_URL = "https://forli.bakecaincontrii.com/donna-cerca-uomo/"
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
}


def clean_text(value):
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_age(text):
    match = re.search(r"\b(\d{2})\s*anni\b", text or "", flags=re.I)
    return int(match.group(1)) if match else None


def parse_location(text):
    """
    Esempi osservati:
      '25 anni Forlì / Cesenatico'
      '21 anni Forlì'
      '33 anni Forlì / CESENA Russa'
    """
    if not text:
        return ""

    text = clean_text(text)
    text = re.sub(r"^\s*\d{2}\s*anni\s*", "", text, flags=re.I)
    return text.strip(" -/")


def looks_like_ad_url(href):
    if not href:
        return False

    absolute = urljoin(BASE_URL, href)
    parsed = urlparse(absolute)

    # Esclude navigazione, categorie e pagine di servizio.
    blocked_parts = (
        "/donna-cerca-uomo/",
        "/uomo-cerca-uomo/",
        "/trans/",
        "/incontri/",
        "/pubblica",
        "/privacy",
        "/cookie",
        "/termin",
        "/contatt",
        "/assistenza",
    )

    path = parsed.path.lower()

    if any(part in path for part in blocked_parts):
        return False

    # Gli annunci sono link interni con uno slug abbastanza lungo.
    return (
        parsed.netloc.endswith("bakecaincontrii.com")
        and len(path.strip("/")) > 20
    )


def extract_from_card(card, page_url):
    text = clean_text(card.get_text(" ", strip=True))
    if not text or "anni" not in text.lower():
        return None

    links = [
        a for a in card.find_all("a", href=True)
        if looks_like_ad_url(a.get("href"))
    ]
    if not links:
        return None

    link_el = max(
        links,
        key=lambda a: len(clean_text(a.get_text(" ", strip=True)))
    )

    title = clean_text(link_el.get_text(" ", strip=True))
    url = urljoin(page_url, link_el.get("href"))

    if len(title) < 8:
        img = link_el.find("img", alt=True) or card.find("img", alt=True)
        if img:
            title = clean_text(img.get("alt"))

    age = parse_age(text)

    # Cerca un piccolo blocco testuale che contiene "anni", tipicamente metadati.
    meta_text = ""
    for node in card.find_all(["div", "span", "p", "li"]):
        candidate = clean_text(node.get_text(" ", strip=True))
        if re.search(r"\b\d{2}\s*anni\b", candidate, flags=re.I):
            if not meta_text or len(candidate) < len(meta_text):
                meta_text = candidate

    location = parse_location(meta_text) if meta_text else ""

    # Descrizione: seleziona il testo più lungo che non coincida col titolo/metadati.
    candidates = []
    for node in card.find_all(["p", "div"]):
        candidate = clean_text(node.get_text(" ", strip=True))
        if (
            len(candidate) >= 40
            and candidate != title
            and "anni" not in candidate.lower()
        ):
            candidates.append(candidate)

    description = max(candidates, key=len) if candidates else ""
    if description == text:
        description = ""

    return {
        "titolo": title,
        "eta": age,
        "localita": location,
        "descrizione": description,
        "url": url,
    }


def extract_ads(html, page_url):
    soup = BeautifulSoup(html, "html.parser")

    # Primo tentativo: classi/elementi che normalmente rappresentano card.
    selectors = [
        "article",
        "[class*='card']",
        "[class*='listing']",
        "[class*='advert']",
        "[class*='annunc']",
        "[class*='result']",
    ]

    candidate_cards = []
    seen_nodes = set()

    for selector in selectors:
        for node in soup.select(selector):
            node_id = id(node)
            if node_id not in seen_nodes:
                seen_nodes.add(node_id)
                candidate_cards.append(node)

    ads = []
    seen_urls = set()

    for card in candidate_cards:
        item = extract_from_card(card, page_url)
        if item and item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            ads.append(item)

    # Fallback: risale dall'anchor al contenitore che include età + descrizione.
    if len(ads) < 3:
        for a in soup.find_all("a", href=True):
            href = a.get("href")
            if not looks_like_ad_url(href):
                continue

            container = a
            selected = None

            for _ in range(6):
                container = container.parent
                if not container:
                    break
                txt = clean_text(container.get_text(" ", strip=True))
                if re.search(r"\b\d{2}\s*anni\b", txt, flags=re.I):
                    selected = container
                    break

            if selected:
                item = extract_from_card(selected, page_url)
                if item and item["url"] not in seen_urls:
                    seen_urls.add(item["url"])
                    ads.append(item)

    return ads


def find_next_page(html, current_url):
    soup = BeautifulSoup(html, "html.parser")

    # Cerca rel="next".
    nxt = soup.find("a", attrs={"rel": lambda x: x and "next" in x})
    if nxt and nxt.get("href"):
        return urljoin(current_url, nxt["href"])

    # Fallback su etichette italiane.
    for a in soup.find_all("a", href=True):
        label = clean_text(a.get_text(" ", strip=True)).lower()
        aria = clean_text(a.get("aria-label", "")).lower()
        if label in {"seguente", "prossima", "next", "›", "»"} or any(
            word in aria for word in ("seguente", "prossima", "next")
        ):
            return urljoin(current_url, a["href"])

    return None


@st.cache_data(ttl=900, show_spinner=False)
def scrape_ads(start_url, max_pages=1):
    session = requests.Session()
    session.headers.update(HEADERS)

    all_ads = []
    seen_urls = set()
    current_url = start_url

    for page_num in range(1, max_pages + 1):
        response = session.get(current_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        page_ads = extract_ads(response.text, current_url)

        for ad in page_ads:
            if ad["url"] not in seen_urls:
                seen_urls.add(ad["url"])
                all_ads.append(ad)

        next_url = find_next_page(response.text, current_url)
        if not next_url or next_url == current_url:
            break

        current_url = next_url

        # Piccola pausa per non martellare il sito.
        if page_num < max_pages:
            time.sleep(0.8)

    return all_ads


def apply_filters(df, age_range, locations, keyword):
    filtered = df.copy()

    if not filtered.empty and filtered["eta"].notna().any():
        filtered = filtered[
            filtered["eta"].isna()
            | filtered["eta"].between(age_range[0], age_range[1])
        ]

    if locations:
        pattern = "|".join(re.escape(x) for x in locations)
        filtered = filtered[
            filtered["localita"].fillna("").str.contains(
                pattern, case=False, regex=True
            )
        ]

    if keyword:
        needle = keyword.strip()
        mask = (
            filtered["titolo"].fillna("").str.contains(
                needle, case=False, regex=False
            )
            | filtered["descrizione"].fillna("").str.contains(
                needle, case=False, regex=False
            )
            | filtered["localita"].fillna("").str.contains(
                needle, case=False, regex=False
            )
        )
        filtered = filtered[mask]

    return filtered


st.set_page_config(
    page_title="Scraper annunci Forlì",
    page_icon="🔎",
    layout="wide",
)

st.title("🔎 Scraper annunci — Forlì")
st.caption(
    "Estrae dati pubblicamente visibili dalla pagina indicata e consente "
    "di filtrarli. Verifica sempre Termini di Servizio e robots.txt del sito."
)

with st.sidebar:
    st.header("Impostazioni")

    url = st.text_input("Pagina da analizzare", value=BASE_URL)

    max_pages = st.number_input(
        "Numero massimo di pagine",
        min_value=1,
        max_value=20,
        value=1,
        step=1,
        help="Per prudenza il valore predefinito è 1.",
    )

    refresh = st.button("Aggiorna scraping", type="primary", use_container_width=True)

if refresh:
    scrape_ads.clear()

try:
    with st.spinner("Lettura della pagina in corso..."):
        records = scrape_ads(url, int(max_pages))

except requests.HTTPError as exc:
    st.error(f"Errore HTTP durante lo scraping: {exc}")
    st.stop()

except requests.RequestException as exc:
    st.error(f"Impossibile collegarsi al sito: {exc}")
    st.stop()

except Exception as exc:
    st.error(f"Errore inatteso: {exc}")
    st.stop()


df = pd.DataFrame(records)

if df.empty:
    st.warning(
        "Nessun annuncio riconosciuto. Il sito potrebbe aver cambiato struttura "
        "HTML oppure potrebbe bloccare le richieste provenienti dal server Streamlit."
    )
    st.stop()

df["eta"] = pd.to_numeric(df["eta"], errors="coerce")

# -------------------------
# Filtri
# -------------------------
st.subheader("Filtri")

col1, col2, col3 = st.columns([1, 1.4, 1.5])

valid_ages = df["eta"].dropna()

with col1:
    if not valid_ages.empty:
        age_min = int(valid_ages.min())
        age_max = int(valid_ages.max())

        if age_min == age_max:
            age_range = (age_min, age_max)
            st.info(f"Età disponibile: {age_min}")
        else:
            age_range = st.slider(
                "Età",
                min_value=age_min,
                max_value=age_max,
                value=(age_min, age_max),
            )
    else:
        age_range = (18, 99)
        st.info("Età non disponibile nei dati estratti.")

with col2:
    location_options = sorted(
        x for x in df["localita"].dropna().astype(str).unique()
        if x.strip()
    )
    selected_locations = st.multiselect(
        "Località / zona",
        options=location_options,
    )

with col3:
    keyword = st.text_input(
        "Cerca nel titolo, descrizione o località",
        placeholder="es. Cesena, nuova, centro...",
    )

filtered = apply_filters(
    df,
    age_range=age_range,
    locations=selected_locations,
    keyword=keyword,
)

st.write(f"**{len(filtered)} risultati** su {len(df)} annunci estratti")

# Vista a schede, senza immagini.
for _, row in filtered.iterrows():
    with st.container(border=True):
        title = row.get("titolo") or "Annuncio"
        st.markdown(f"### {title}")

        meta = []
        if pd.notna(row.get("eta")):
            meta.append(f"{int(row['eta'])} anni")
        if row.get("localita"):
            meta.append(str(row["localita"]))

        if meta:
            st.caption(" · ".join(meta))

        if row.get("descrizione"):
            st.write(row["descrizione"])

        st.link_button("Apri annuncio", row["url"])

# Download CSV dei soli risultati filtrati.
csv_data = filtered.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "Scarica risultati filtrati in CSV",
    data=csv_data,
    file_name="annunci_forli_filtrati.csv",
    mime="text/csv",
)
