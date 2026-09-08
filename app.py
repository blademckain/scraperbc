import os
import re
import time
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup


SOURCES = {
    "Forlì": "https://forli.bakecaincontrii.com/donna-cerca-uomo/",
    "Rimini": "https://rimini.bakecaincontrii.com/donna-cerca-uomo/",
    "Ravenna": "https://ravenna.bakecaincontrii.com/donna-cerca-uomo/",
}

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

def count_google_domain_matches(
    value,
    target_domain="community.punterforum.com",
    max_results=20,
):
    """
    Cerca un valore tramite Serper/Google e conta
    quanti dei primi risultati appartengono al dominio indicato.

    La funzione resta dormiente finché non viene chiamata.
    """

    if not value:
        return 0

    if "SERPER_API_KEY" not in st.secrets:
        raise RuntimeError(
            "SERPER_API_KEY non configurata nei Secrets di Streamlit."
        )

    api_key = st.secrets["SERPER_API_KEY"]

    search_url = "https://google.serper.dev/search"

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "q": str(value),
        "num": min(int(max_results), 20),
        "gl": "it",
        "hl": "it",
    }

    response = requests.post(
        search_url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    data = response.json()

    organic_results = data.get(
        "organic",
        []
    )

    matches = 0

    for result in organic_results:

        link = result.get(
            "link",
            ""
        )

        try:
            hostname = (
                urlparse(link).hostname
                or ""
            ).lower()

        except Exception:
            hostname = ""

        if (
            hostname == target_domain
            or hostname.endswith(
                "." + target_domain
            )
        ):
            matches += 1

    return matches

def normalize_phone(value):
    if not value:
        return ""

    value = value.replace("tel:", "").strip()

    has_plus = value.startswith("+")
    digits = re.sub(r"\D", "", value)

    if not digits:
        return ""

    normalized = ("+" if has_plus else "") + digits

    if len(digits) < 8 or len(digits) > 13:
        return ""

    return normalized


def extract_phone_from_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    # 1. Cerca link del tipo tel:3331234567
    for a in soup.select('a[href^="tel:"]'):
        candidates.append(a.get("href", ""))

    # 2. Cerca il telefono negli attributi HTML
    phone_attr_names = (
        "data-phone",
        "data-telephone",
        "data-tel",
        "data-number",
        "data-phone-number",
        "phone",
        "telephone",
    )

    for tag in soup.find_all(True):
        for attr_name in phone_attr_names:
            if tag.has_attr(attr_name):
                candidates.append(str(tag.get(attr_name)))

    # 3. Cerca nel titolo della pagina
    if soup.title:
        candidates.append(
            soup.title.get_text(" ", strip=True)
        )

    # 4. Cerca nei meta tag
    for meta in soup.find_all("meta"):
        content = meta.get("content")
        if content:
            candidates.append(content)

    # 5. Cerca negli script / JSON incorporati
    for script in soup.find_all("script"):
        script_text = (
            script.string
            or script.get_text(" ", strip=True)
        )

        if script_text:
            candidates.append(script_text)

    # 6. Cerca nel testo visibile
    candidates.append(
        soup.get_text(" ", strip=True)
    )

    # Cellulari italiani
    mobile_pattern = re.compile(
        r"(?<!\d)(?:\+?39[\s.\-]*)?"
        r"(3(?:[\s.\-]*\d){8,9})(?!\d)"
    )

    for candidate in candidates:
        if not candidate:
            continue

        match = mobile_pattern.search(candidate)

        if match:
            phone = normalize_phone(match.group(0))

            if phone:
                return phone

    # Ricerca generica come fallback
    generic_pattern = re.compile(
        r"(?<!\d)(\+?\d(?:[\s.\-]*\d){7,12})(?!\d)"
    )

    for candidate in candidates:
        if not candidate:
            continue

        for match in generic_pattern.finditer(candidate):

            phone = normalize_phone(
                match.group(1)
            )

            if phone:
                digits = re.sub(
                    r"\D",
                    "",
                    phone
                )

                if (
                    digits.startswith("20")
                    and len(digits) == 8
                ):
                    continue

                return phone

    return ""


def looks_like_ad_url(href):
    if not href:
        return False

    absolute = urljoin(
        "https://www.bakecaincontrii.com/",
        href
    )

    parsed = urlparse(absolute)
    path = parsed.path.lower()

    # Gli annunci hanno normalmente /annuncio/
    if "/annuncio/" in path:
        return True

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

    return (
        parsed.netloc.endswith(
            "bakecaincontrii.com"
        )
        and not any(
            part in path
            for part in blocked_parts
        )
        and len(path.strip("/")) > 20
    )


def extract_listing_links(html, page_url):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []
    seen_urls = set()

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get("href")

        if not looks_like_ad_url(href):
            continue

        url = urljoin(
            page_url,
            href
        )

        if url in seen_urls:
            continue

        title = clean_text(
            a.get_text(
                " ",
                strip=True
            )
        )

        # Cerca ALT immagine
        if len(title) < 5:

            img = a.find(
                "img",
                alt=True
            )

            if img:
                title = clean_text(
                    img.get("alt")
                )

        # Cerca titolo vicino
        if len(title) < 5:

            parent = a.parent

            if parent:

                heading = parent.find(
                    [
                        "h1",
                        "h2",
                        "h3",
                        "h4"
                    ]
                )

                if heading:
                    title = clean_text(
                        heading.get_text(
                            " ",
                            strip=True
                        )
                    )

        if len(title) < 5:
            continue

        seen_urls.add(url)

        results.append(
            {
                "nome_annuncio": title,
                "link": url,
            }
        )

    return results


def get_page(session, url):

    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    return response.text


@st.cache_data(
    ttl=900,
    show_spinner=False
)
def scrape_all_sources():

    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    rows = []

    for city, source_url in SOURCES.items():

        listing_html = get_page(
            session,
            source_url
        )

        listings = extract_listing_links(
            listing_html,
            source_url
        )

        for item in listings:

            phone = ""

            try:

                detail_html = get_page(
                    session,
                    item["link"]
                )

                phone = (
                    extract_phone_from_detail(
                        detail_html
                    )
                )

            except requests.RequestException:

                phone = ""

            rows.append(
                {
                    "Annuncio":
                        item["nome_annuncio"],

                    "Città":
                        city,

                    "Telefono":
                        phone
                        if phone
                        else "Non trovato",

                    "Link":
                        item["link"],
                }
            )

            # Piccola pausa tra gli annunci
            time.sleep(0.25)

    # Elimina duplicati
    unique_rows = []
    seen = set()

    for row in rows:

        key = (
            row["Link"],
            row["Città"]
        )

        if key not in seen:

            seen.add(key)

            unique_rows.append(row)

    return unique_rows


# =========================
# INTERFACCIA STREAMLIT
# =========================

st.set_page_config(
    page_title="Scraper annunci Romagna",
    page_icon="🔎",
    layout="wide",
)

st.title(
    "🔎 Annunci Forlì, Rimini e Ravenna"
)

st.caption(
    "Elenco degli annunci pubblicamente "
    "visibili trovati nelle tre città."
)


refresh = st.button(
    "🔄 Aggiorna dati",
    type="primary"
)

if refresh:
    scrape_all_sources.clear()


try:

    with st.spinner(
        "Sto leggendo gli annunci "
        "e cercando i numeri di telefono..."
    ):

        records = scrape_all_sources()


except requests.HTTPError as exc:

    st.error(
        f"Errore HTTP: {exc}"
    )

    st.stop()


except requests.RequestException as exc:

    st.error(
        f"Errore di connessione: {exc}"
    )

    st.stop()


except Exception as exc:

    st.error(
        f"Errore inatteso: {exc}"
    )

    st.stop()


if not records:

    st.warning(
        "Non è stato trovato "
        "alcun annuncio."
    )

    st.stop()


df = pd.DataFrame(records)


# =========================
# CONTATORI
# =========================

city_counts = (
    df.groupby("Città")
    .size()
    .to_dict()
)


st.write(
    f"**{len(df)} annunci trovati**"
)

st.write(
    f"Forlì: "
    f"**{city_counts.get('Forlì', 0)}**"
    f" — Rimini: "
    f"**{city_counts.get('Rimini', 0)}**"
    f" — Ravenna: "
    f"**{city_counts.get('Ravenna', 0)}**"
)


# =========================
# TABELLA
# =========================

display_df = df[
    [
        "Annuncio",
        "Città",
        "Telefono",
        "Link"
    ]
].copy()


st.dataframe(
    display_df,
    use_container_width=True,
    hide_index=True,

    column_config={

        "Annuncio":
            st.column_config.TextColumn(
                "Nome annuncio",
                width="large"
            ),

        "Città":
            st.column_config.TextColumn(
                "Città",
                width="small"
            ),

        "Telefono":
            st.column_config.TextColumn(
                "Telefono",
                width="medium"
            ),

        "Link":
            st.column_config.LinkColumn(
                "Annuncio",
                display_text="Apri",
                width="small"
            ),
    },
)


# =========================
# DOWNLOAD CSV
# =========================

csv_data = (
    df.to_csv(
        index=False
    )
    .encode("utf-8-sig")
)


st.download_button(
    "📥 Scarica elenco CSV",
    data=csv_data,
    file_name=(
        "annunci_forli_"
        "rimini_ravenna.csv"
    ),
    mime="text/csv",
)
