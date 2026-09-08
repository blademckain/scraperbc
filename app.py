import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from supabase import create_client


# ============================================================
# CONFIGURAZIONE
# ============================================================

SOURCES = {
    "Forlì": "https://forli.bakecaincontrii.com/donna-cerca-uomo/",
    "Rimini": "https://rimini.bakecaincontrii.com/donna-cerca-uomo/",
    "Ravenna": "https://ravenna.bakecaincontrii.com/donna-cerca-uomo/",
}

TABLE_NAME = "annunci"
PHONE_TABLE = "telefoni_registry"

REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
}


# ============================================================
# UTILITÀ
# ============================================================

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


# ============================================================
# TELEFONO
# ============================================================

def normalize_phone(value):
    if not value:
        return ""

    value = value.replace(
        "tel:",
        ""
    )

    digits = re.sub(
        r"\D",
        "",
        value
    )

    if digits.startswith("0039"):
        digits = digits[4:]

    if (
        digits.startswith("39")
        and len(digits) >= 12
    ):
        digits = digits[2:]

    if len(digits) < 8:
        return ""

    if len(digits) > 11:
        return ""

    return digits


def make_phone_hash(phone):
    normalized = normalize_phone(
        phone
    )

    if not normalized:
        return ""

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


def extract_phone_from_detail(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []

    # --------------------------------------------------------
    # Link tel:
    # --------------------------------------------------------

    for a in soup.select(
        'a[href^="tel:"]'
    ):
        candidates.append(
            a.get("href", "")
        )

    # --------------------------------------------------------
    # Attributi HTML
    # --------------------------------------------------------

    phone_attrs = (
        "data-phone",
        "data-telephone",
        "data-tel",
        "data-number",
        "data-phone-number",
        "phone",
        "telephone",
    )

    for tag in soup.find_all(True):
        for attr in phone_attrs:
            if tag.has_attr(attr):
                candidates.append(
                    str(tag.get(attr))
                )

    # --------------------------------------------------------
    # Titolo pagina
    # --------------------------------------------------------

    if soup.title:
        candidates.append(
            soup.title.get_text(
                " ",
                strip=True
            )
        )

    # --------------------------------------------------------
    # Meta
    # --------------------------------------------------------

    for meta in soup.find_all(
        "meta"
    ):
        content = meta.get(
            "content"
        )

        if content:
            candidates.append(
                content
            )

    # --------------------------------------------------------
    # Script
    # --------------------------------------------------------

    for script in soup.find_all(
        "script"
    ):
        text = (
            script.string
            or script.get_text(
                " ",
                strip=True
            )
        )

        if text:
            candidates.append(
                text
            )

    # --------------------------------------------------------
    # Testo visibile
    # --------------------------------------------------------

    candidates.append(
        soup.get_text(
            " ",
            strip=True
        )
    )

    # --------------------------------------------------------
    # Cellulari italiani
    # --------------------------------------------------------

    mobile_pattern = re.compile(
        r"(?<!\d)"
        r"(?:\+?39[\s.\-]*)?"
        r"(3(?:[\s.\-]*\d){8,9})"
        r"(?!\d)"
    )

    for candidate in candidates:
        if not candidate:
            continue

        match = mobile_pattern.search(
            candidate
        )

        if match:
            phone = normalize_phone(
                match.group(0)
            )

            if phone:
                return phone

    return ""


# ============================================================
# LINK ANNUNCI
# ============================================================

def looks_like_ad_url(href):
    if not href:
        return False

    absolute = urljoin(
        "https://www.bakecaincontrii.com/",
        href
    )

    parsed = urlparse(
        absolute
    )

    path = parsed.path.lower()

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
        and len(
            path.strip("/")
        ) > 20
    )


def extract_listing_links(
    html,
    page_url
):
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
        href = a.get(
            "href"
        )

        if not looks_like_ad_url(
            href
        ):
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

        if len(title) < 5:
            img = a.find(
                "img",
                alt=True
            )

            if img:
                title = clean_text(
                    img.get(
                        "alt"
                    )
                )

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

        seen_urls.add(
            url
        )

        results.append(
            {
                "titolo": title,
                "url": url,
            }
        )

    return results


# ============================================================
# HTTP
# ============================================================

def get_page(
    session,
    url
):
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    return response.text


# ============================================================
# SUPABASE
# ============================================================

@st.cache_resource
def get_supabase():
    url = st.secrets[
        "SUPABASE_URL"
    ]

    key = st.secrets[
        "SUPABASE_KEY"
    ]

    return create_client(
        url,
        key
    )


# ============================================================
# DATABASE ANNUNCI
# ============================================================

def load_all_ads():
    db = get_supabase()

    response = (
        db
        .table(TABLE_NAME)
        .select("*")
        .order(
            "first_seen",
            desc=True
        )
        .execute()
    )

    return response.data or []


def load_known_ads():
    db = get_supabase()

    response = (
        db
        .table(TABLE_NAME)
        .select(
            "url,telefono,phone_hash"
        )
        .execute()
    )

    result = {}

    for row in (
        response.data
        or []
    ):
        url = row.get(
            "url"
        )

        if url:
            result[url] = {
                "telefono":
                    row.get(
                        "telefono"
                    ) or "",

                "phone_hash":
                    row.get(
                        "phone_hash"
                    ) or "",
            }

    return result


def insert_new_ad(row):
    db = get_supabase()

    (
        db
        .table(TABLE_NAME)
        .insert(row)
        .execute()
    )


def update_last_seen(
    url,
    seen_at
):
    db = get_supabase()

    (
        db
        .table(TABLE_NAME)
        .update(
            {
                "last_seen":
                    seen_at,

                "active":
                    True,
            }
        )
        .eq(
            "url",
            url
        )
        .execute()
    )


def mark_missing_as_inactive(
    current_urls
):
    db = get_supabase()

    existing = load_all_ads()

    for row in existing:
        url = row.get(
            "url"
        )

        if (
            url
            and url not in current_urls
            and row.get(
                "active",
                True
            )
        ):
            (
                db
                .table(TABLE_NAME)
                .update(
                    {
                        "active":
                            False
                    }
                )
                .eq(
                    "url",
                    url
                )
                .execute()
            )


# ============================================================
# REGISTRO TELEFONI
# ============================================================

def load_known_phone_hashes():
    db = get_supabase()

    response = (
        db
        .table(PHONE_TABLE)
        .select(
            "phone_hash,processed"
        )
        .execute()
    )

    result = {}

    for row in (
        response.data
        or []
    ):
        phone_hash = row.get(
            "phone_hash"
        )

        if phone_hash:
            result[
                phone_hash
            ] = {
                "processed":
                    bool(
                        row.get(
                            "processed",
                            False
                        )
                    )
            }

    return result


def insert_phone_hash(
    phone_hash,
    seen_at
):
    if not phone_hash:
        return

    db = get_supabase()

    (
        db
        .table(PHONE_TABLE)
        .insert(
            {
                "phone_hash":
                    phone_hash,

                "first_seen":
                    seen_at,

                "last_seen":
                    seen_at,

                "processed":
                    False,
            }
        )
        .execute()
    )


def update_phone_last_seen(
    phone_hash,
    seen_at
):
    if not phone_hash:
        return

    db = get_supabase()

    (
        db
        .table(PHONE_TABLE)
        .update(
            {
                "last_seen":
                    seen_at
            }
        )
        .eq(
            "phone_hash",
            phone_hash
        )
        .execute()
    )


# ============================================================
# SCRAPING + SYNC
# ============================================================

def scrape_and_sync():
    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    known_ads = (
        load_known_ads()
    )

    known_phone_hashes = (
        load_known_phone_hashes()
    )

    current_urls = set()

    new_ads_count = 0
    known_ads_count = 0
    new_phones_count = 0
    duplicate_phone_count = 0

    errors = []

    now = utc_now_iso()

    for city, source_url in (
        SOURCES.items()
    ):
        try:
            listing_html = get_page(
                session,
                source_url
            )

            listings = (
                extract_listing_links(
                    listing_html,
                    source_url
                )
            )

        except requests.RequestException as exc:
            errors.append(
                f"{city}: {exc}"
            )

            continue

        for item in listings:
            url = item[
                "url"
            ]

            current_urls.add(
                url
            )

            # ------------------------------------------------
            # URL già conosciuto
            # ------------------------------------------------

            if url in known_ads:
                known_ads_count += 1

                try:
                    update_last_seen(
                        url,
                        now
                    )

                except Exception as exc:
                    errors.append(
                        f"Aggiornamento {url}: {exc}"
                    )

                existing_hash = (
                    known_ads[url]
                    .get(
                        "phone_hash",
                        ""
                    )
                )

                if existing_hash:
                    try:
                        update_phone_last_seen(
                            existing_hash,
                            now
                        )

                    except Exception as exc:
                        errors.append(
                            f"Aggiornamento telefono: {exc}"
                        )

                continue

            # ------------------------------------------------
            # Nuovo URL
            # ------------------------------------------------

            phone = ""
            phone_hash = ""

            try:
                detail_html = get_page(
                    session,
                    url
                )

                phone = (
                    extract_phone_from_detail(
                        detail_html
                    )
                )

                phone_hash = (
                    make_phone_hash(
                        phone
                    )
                )

            except requests.RequestException as exc:
                errors.append(
                    f"Dettaglio {url}: {exc}"
                )

            # ------------------------------------------------
            # Registro telefono
            # ------------------------------------------------

            if phone_hash:
                if (
                    phone_hash
                    in known_phone_hashes
                ):
                    duplicate_phone_count += 1

                    try:
                        update_phone_last_seen(
                            phone_hash,
                            now
                        )

                    except Exception as exc:
                        errors.append(
                            f"Telefono esistente: {exc}"
                        )

                else:
                    try:
                        insert_phone_hash(
                            phone_hash,
                            now
                        )

                        known_phone_hashes[
                            phone_hash
                        ] = {
                            "processed":
                                False
                        }

                        new_phones_count += 1

                    except Exception as exc:
                        errors.append(
                            f"Nuovo telefono: {exc}"
                        )

            # ------------------------------------------------
            # Salva annuncio
            # ------------------------------------------------

            row = {
                "url":
                    url,

                "titolo":
                    item["titolo"],

                "citta":
                    city,

                "telefono":
                    phone or "",

                "phone_hash":
                    phone_hash or None,

                "first_seen":
                    now,

                "last_seen":
                    now,

                "active":
                    True,

                "note":
                    "",

                "external_check_done":
                    False,

                "external_match_count":
                    None,
            }

            try:
                insert_new_ad(
                    row
                )

                known_ads[
                    url
                ] = {
                    "telefono":
                        phone,

                    "phone_hash":
                        phone_hash,
                }

                new_ads_count += 1

            except Exception as exc:
                errors.append(
                    f"Salvataggio annuncio {url}: {exc}"
                )

            time.sleep(
                0.25
            )

    if current_urls:
        try:
            mark_missing_as_inactive(
                current_urls
            )

        except Exception as exc:
            errors.append(
                f"Aggiornamento inattivi: {exc}"
            )

    return {
        "new_ads":
            new_ads_count,

        "known_ads":
            known_ads_count,

        "new_phones":
            new_phones_count,

        "duplicate_phones":
            duplicate_phone_count,

        "current":
            len(current_urls),

        "errors":
            errors,
    }


# ============================================================
# DATAFRAME BASE
# ============================================================

def dataframe_from_rows(
    rows
):
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows
    )

    columns = [
        "id",
        "titolo",
        "citta",
        "telefono",
        "phone_hash",
        "url",
        "note",
        "active",
        "first_seen",
        "last_seen",
    ]

    for col in columns:
        if col not in df.columns:
            df[col] = None

    return df[
        columns
    ]


# ============================================================
# COSTRUZIONE VISTA RAGGRUPPATA PER TELEFONO
# ============================================================

def build_grouped_phone_view(df):
    """
    Una riga per ogni phone_hash.

    Colonne:
    - Telefono ID
    - Città
    - Numero annunci
    - elenco annunci
    """

    if df.empty:
        return []

    working = df.copy()

    # Escludiamo record senza telefono/hash
    working = working[
        working["phone_hash"]
        .fillna("")
        .astype(str)
        .str.len()
        > 0
    ]

    groups = []

    for phone_hash, group in (
        working.groupby(
            "phone_hash"
        )
    ):
        cities = sorted(
            {
                str(city)
                for city in group[
                    "citta"
                ].dropna()
                if str(city).strip()
            }
        )

        ads = []

        for _, row in group.iterrows():
            ads.append(
                {
                    "titolo":
                        row.get(
                            "titolo"
                        ) or "Annuncio",

                    "url":
                        row.get(
                            "url"
                        ) or "",

                    "citta":
                        row.get(
                            "citta"
                        ) or "",
                }
            )

        groups.append(
            {
                "phone_hash":
                    phone_hash,

                "telefono_id":
                    str(
                        phone_hash
                    )[:8],

                "citta":
                    ", ".join(
                        cities
                    ),

                "numero_annunci":
                    len(ads),

                "annunci":
                    ads,
            }
        )

    # Prima i telefoni con più annunci
    groups.sort(
        key=lambda x: (
            x[
                "numero_annunci"
            ],
            x[
                "telefono_id"
            ]
        ),
        reverse=True
    )

    return groups


# ============================================================
# STREAMLIT
# ============================================================

st.set_page_config(
    page_title=(
        "Archivio annunci Romagna"
    ),
    page_icon="🔎",
    layout="wide",
)


st.title(
    "🔎 Archivio annunci "
    "Forlì, Rimini e Ravenna"
)

st.caption(
    "Gli annunci che condividono lo stesso "
    "telefono vengono raggruppati utilizzando "
    "un identificatore hash."
)


# ============================================================
# CONTROLLO SECRETS
# ============================================================

missing_secrets = [
    name
    for name in (
        "SUPABASE_URL",
        "SUPABASE_KEY"
    )
    if name not in st.secrets
]

if missing_secrets:
    st.error(
        "Mancano i Secrets Streamlit: "
        + ", ".join(
            missing_secrets
        )
    )

    st.stop()


# ============================================================
# AGGIORNAMENTO
# ============================================================

if st.button(
    "🔄 Cerca nuovi annunci",
    type="primary"
):
    try:
        with st.spinner(
            "Controllo Forlì, Rimini e Ravenna..."
        ):
            result = (
                scrape_and_sync()
            )

        st.success(
            f"Nuovi annunci: "
            f"{result['new_ads']} · "
            f"Già conosciuti: "
            f"{result['known_ads']} · "
            f"Nuovi telefoni: "
            f"{result['new_phones']} · "
            f"Telefoni già conosciuti: "
            f"{result['duplicate_phones']} · "
            f"Annunci attualmente trovati: "
            f"{result['current']}"
        )

        if result["errors"]:
            with st.expander(
                f"Dettagli errori "
                f"({len(result['errors'])})"
            ):
                for error in (
                    result["errors"]
                ):
                    st.write(
                        error
                    )

    except Exception as exc:
        st.error(
            "Errore durante "
            "l'aggiornamento: "
            f"{exc}"
        )


# ============================================================
# CARICAMENTO DATABASE
# ============================================================

try:
    rows = (
        load_all_ads()
    )

except Exception as exc:
    st.error(
        "Impossibile leggere "
        "il database Supabase. "
        f"Dettaglio: {exc}"
    )

    st.stop()


df = dataframe_from_rows(
    rows
)


if df.empty:
    st.info(
        "Il database è vuoto. "
        "Premi “Cerca nuovi annunci”."
    )

    st.stop()


# ============================================================
# SOLO ANNUNCI ATTIVI
# ============================================================

active_df = df[
    df["active"] == True
].copy()


# ============================================================
# METRICHE
# ============================================================

grouped_active = (
    build_grouped_phone_view(
        active_df
    )
)

c1, c2, c3, c4 = (
    st.columns(4)
)

c1.metric(
    "Annunci attivi",
    len(active_df)
)

c2.metric(
    "Telefoni distinti",
    len(grouped_active)
)

c3.metric(
    "Forlì",
    int(
        (
            active_df["citta"]
            == "Forlì"
        ).sum()
    )
)

c4.metric(
    "Rimini + Ravenna",
    int(
        active_df[
            "citta"
        ]
        .isin(
            [
                "Rimini",
                "Ravenna"
            ]
        )
        .sum()
    )
)


# ============================================================
# INATTIVI
# ============================================================

show_inactive = st.checkbox(
    "Mostra anche annunci non più presenti",
    value=False
)

if show_inactive:
    view_df = (
        df.copy()
    )

else:
    view_df = (
        active_df.copy()
    )


# ============================================================
# VISTA RAGGRUPPATA
# ============================================================

groups = (
    build_grouped_phone_view(
        view_df
    )
)


st.subheader(
    "Risultati raggruppati per telefono"
)


if not groups:
    st.warning(
        "Non ci sono annunci con "
        "un telefono riconosciuto."
    )

else:
    # --------------------------------------------------------
    # Intestazione
    # --------------------------------------------------------

    header1, header2, header3 = (
        st.columns(
            [
                1.2,
                1.5,
                5
            ]
        )
    )

    header1.markdown(
        "**Telefono ID**"
    )

    header2.markdown(
        "**Città**"
    )

    header3.markdown(
        "**Annunci**"
    )

    st.divider()

    # --------------------------------------------------------
    # Una riga per telefono
    # --------------------------------------------------------

    for group in groups:
        col1, col2, col3 = (
            st.columns(
                [
                    1.2,
                    1.5,
                    5
                ]
            )
        )

        with col1:
            st.code(
                group[
                    "telefono_id"
                ],
                language=None
            )

            st.caption(
                f"{group['numero_annunci']} "
                f"annunci"
            )

        with col2:
            st.write(
                group[
                    "citta"
                ]
                or "-"
            )

        with col3:
            for index, ad in enumerate(
                group[
                    "annunci"
                ],
                start=1
            ):
                title = (
                    ad[
                        "titolo"
                    ]
                    or f"Annuncio {index}"
                )

                city = (
                    ad[
                        "citta"
                    ]
                    or ""
                )

                url = (
                    ad[
                        "url"
                    ]
                )

                if url:
                    st.markdown(
                        f"**{index}. "
                        f"[{title}]({url})** "
                        f"— {city}"
                    )

        st.divider()


# ============================================================
# CSV
# ============================================================

csv_rows = []

for group in groups:
    csv_rows.append(
        {
            "telefono_id":
                group[
                    "telefono_id"
                ],

            "citta":
                group[
                    "citta"
                ],

            "numero_annunci":
                group[
                    "numero_annunci"
                ],

            "annunci":
                " | ".join(
                    [
                        (
                            f"{ad['titolo']} "
                            f"({ad['url']})"
                        )
                        for ad in group[
                            "annunci"
                        ]
                    ]
                ),
        }
    )


csv_df = pd.DataFrame(
    csv_rows
)

csv_data = (
    csv_df
    .to_csv(
        index=False
    )
    .encode(
        "utf-8-sig"
    )
)


st.download_button(
    "📥 Scarica CSV raggruppato",
    data=csv_data,
    file_name=(
        "annunci_raggruppati_per_telefono.csv"
    ),
    mime="text/csv",
)
