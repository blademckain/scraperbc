import re
import time
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
# FUNZIONI GENERALI
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
    ).strip()

    has_plus = value.startswith("+")

    digits = re.sub(
        r"\D",
        "",
        value
    )

    if len(digits) < 8:
        return ""

    if len(digits) > 13:
        return ""

    return (
        ("+" if has_plus else "")
        + digits
    )


def extract_phone_from_detail(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []

    # -----------------------------------------
    # 1. Link tel:
    # -----------------------------------------

    for a in soup.select(
        'a[href^="tel:"]'
    ):
        candidates.append(
            a.get("href", "")
        )

    # -----------------------------------------
    # 2. Attributi HTML
    # -----------------------------------------

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

    # -----------------------------------------
    # 3. Titolo pagina
    # -----------------------------------------

    if soup.title:

        candidates.append(
            soup.title.get_text(
                " ",
                strip=True
            )
        )

    # -----------------------------------------
    # 4. Meta tag
    # -----------------------------------------

    for meta in soup.find_all("meta"):

        content = meta.get("content")

        if content:

            candidates.append(
                content
            )

    # -----------------------------------------
    # 5. Script / JSON
    # -----------------------------------------

    for script in soup.find_all("script"):

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

    # -----------------------------------------
    # 6. Testo visibile
    # -----------------------------------------

    candidates.append(
        soup.get_text(
            " ",
            strip=True
        )
    )

    # -----------------------------------------
    # Cerca cellulari italiani
    # -----------------------------------------

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
# RICONOSCIMENTO LINK ANNUNCI
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

    # Normalmente gli annunci hanno /annuncio/
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


# ============================================================
# ESTRAZIONE ANNUNCI DALLA PAGINA PRINCIPALE
# ============================================================

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

        href = a.get("href")

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

        # -------------------------------------
        # Se manca il titolo cerca ALT immagine
        # -------------------------------------

        if len(title) < 5:

            img = a.find(
                "img",
                alt=True
            )

            if img:

                title = clean_text(
                    img.get("alt")
                )

        # -------------------------------------
        # Cerca heading vicino
        # -------------------------------------

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
# DOWNLOAD PAGINE
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

    return (
        response.data
        or []
    )


def load_known_urls():

    db = get_supabase()

    response = (
        db
        .table(TABLE_NAME)
        .select("url")
        .execute()
    )

    return {
        row["url"]
        for row in (
            response.data
            or []
        )
    }


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
                "last_seen": seen_at,
                "active": True,
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
                        "active": False
                    }
                )
                .eq(
                    "url",
                    url
                )
                .execute()
            )


def save_note(
    row_id,
    note
):

    db = get_supabase()

    (
        db
        .table(TABLE_NAME)
        .update(
            {
                "note": note or ""
            }
        )
        .eq(
            "id",
            int(row_id)
        )
        .execute()
    )


# ============================================================
# SCRAPING + SINCRONIZZAZIONE DATABASE
# ============================================================

def scrape_and_sync():

    session = requests.Session()

    session.headers.update(
        HEADERS
    )

    # URL già presenti nel database
    known_urls = load_known_urls()

    # URL trovati nello scraping corrente
    current_urls = set()

    new_count = 0
    known_count = 0

    errors = []

    now = utc_now_iso()

    # --------------------------------------------------------
    # Analizza Forlì, Rimini e Ravenna
    # --------------------------------------------------------

    for city, source_url in SOURCES.items():

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

        # ----------------------------------------------------
        # Analizza gli annunci trovati
        # ----------------------------------------------------

        for item in listings:

            url = item["url"]

            current_urls.add(
                url
            )

            # =================================================
            # ANNUNCIO GIÀ CONOSCIUTO
            # =================================================

            if url in known_urls:

                known_count += 1

                try:

                    update_last_seen(
                        url,
                        now
                    )

                except Exception as exc:

                    errors.append(
                        f"Aggiornamento {url}: {exc}"
                    )

                # IMPORTANTE:
                # non apre nuovamente il dettaglio
                continue

            # =================================================
            # NUOVO ANNUNCIO
            # =================================================

            phone = ""

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

            except requests.RequestException as exc:

                errors.append(
                    f"Dettaglio {url}: {exc}"
                )

            # -------------------------------------------------
            # Record da salvare
            # -------------------------------------------------

            row = {

                "url":
                    url,

                "titolo":
                    item["titolo"],

                "citta":
                    city,

                "telefono":
                    phone or "",

                "first_seen":
                    now,

                "last_seen":
                    now,

                "active":
                    True,

                "note":
                    "",

                # ---------------------------------------------
                # Campi predisposti per elaborazioni future
                # ---------------------------------------------

                "external_check_done":
                    False,

                "external_match_count":
                    None,
            }

            # -------------------------------------------------
            # Salvataggio Supabase
            # -------------------------------------------------

            try:

                insert_new_ad(
                    row
                )

                known_urls.add(
                    url
                )

                new_count += 1

            except Exception as exc:

                errors.append(
                    f"Salvataggio {url}: {exc}"
                )

            # Piccola pausa per non sovraccaricare il sito
            time.sleep(
                0.25
            )

    # --------------------------------------------------------
    # Annunci non più presenti
    # --------------------------------------------------------

    if current_urls:

        try:

            mark_missing_as_inactive(
                current_urls
            )

        except Exception as exc:

            errors.append(
                "Aggiornamento stato annunci: "
                f"{exc}"
            )

    return {

        "new":
            new_count,

        "known":
            known_count,

        "current":
            len(current_urls),

        "errors":
            errors,
    }


# ============================================================
# DATAFRAME
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
        "url",
        "note",
        "active",
        "first_seen",
        "last_seen",
        "external_check_done",
        "external_match_count",

    ]

    for col in columns:

        if col not in df.columns:

            df[col] = None

    return df[
        columns
    ]


# ============================================================
# INTERFACCIA STREAMLIT
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
    "Gli annunci vengono salvati "
    "nel database Supabase. "
    "Durante gli aggiornamenti le pagine "
    "di dettaglio vengono aperte soltanto "
    "per gli URL mai processati prima."
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
# PULSANTE AGGIORNAMENTO
# ============================================================

refresh = st.button(

    "🔄 Cerca nuovi annunci",

    type="primary",

)


if refresh:

    try:

        with st.spinner(
            "Controllo Forlì, Rimini e Ravenna "
            "e salvo soltanto i nuovi annunci..."
        ):

            result = (
                scrape_and_sync()
            )

        st.success(

            f"Controllo completato. "

            f"Nuovi: "
            f"{result['new']} · "

            f"Già conosciuti: "
            f"{result['known']} · "

            f"Attualmente trovati: "
            f"{result['current']}"

        )

        if result["errors"]:

            with st.expander(
                f"Dettagli errori "
                f"({len(result['errors'])})"
            ):

                for error in result[
                    "errors"
                ]:

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

    rows = load_all_ads()

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


# ============================================================
# DATABASE VUOTO
# ============================================================

if df.empty:

    st.info(
        "Il database è ancora vuoto. "
        "Premi “Cerca nuovi annunci” "
        "per effettuare il primo caricamento."
    )

    st.stop()


# ============================================================
# ANNUNCI ATTIVI
# ============================================================

active_df = df[
    df["active"] == True
].copy()


# ============================================================
# RIEPILOGO
# ============================================================

c1, c2, c3, c4 = st.columns(
    4
)


c1.metric(
    "Totale archivio",
    len(df)
)


c2.metric(
    "Attivi",
    len(active_df)
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
# MOSTRA INATTIVI
# ============================================================

show_inactive = st.checkbox(

    "Mostra anche annunci "
    "non più presenti nelle "
    "pagine correnti",

    value=False,

)


if show_inactive:

    view_df = df.copy()

else:

    view_df = (
        active_df.copy()
    )


# ============================================================
# ORDINAMENTO
# ============================================================

view_df = view_df.sort_values(

    by=[
        "last_seen",
        "first_seen"
    ],

    ascending=[
        False,
        False
    ],

    na_position="last",

)


# ============================================================
# TABELLA MODIFICABILE
# ============================================================

st.subheader(
    "Annunci"
)


editor_df = view_df[
    [
        "id",
        "titolo",
        "citta",
        "telefono",
        "url",
        "note",
        "active",
    ]
].copy()


edited_df = st.data_editor(

    editor_df,

    use_container_width=True,

    hide_index=True,

    key="ads_editor",

    # Tutto bloccato tranne Note
    disabled=[
        "id",
        "titolo",
        "citta",
        "telefono",
        "url",
        "active",
    ],

    column_config={

        "id":
            None,

        "titolo":
            st.column_config.TextColumn(
                "Nome annuncio",
                width="large",
            ),

        "citta":
            st.column_config.TextColumn(
                "Città",
                width="small",
            ),

        "telefono":
            st.column_config.TextColumn(
                "Telefono",
                width="medium",
            ),

        "url":
            st.column_config.LinkColumn(
                "Annuncio",
                display_text="Apri",
                width="small",
            ),

        "note":
            st.column_config.TextColumn(
                "Note",
                help=(
                    "Campo modificabile "
                    "manualmente"
                ),
                width="large",
            ),

        "active":
            st.column_config.CheckboxColumn(
                "Attivo",
                width="small",
            ),

    },

)


# ============================================================
# SALVATAGGIO NOTE
# ============================================================

if st.button(
    "💾 Salva note"
):

    original_notes = {

        int(row["id"]):
            (
                row.get("note")
                or ""
            )

        for _, row
        in editor_df.iterrows()

    }


    changed = 0

    errors = []


    with st.spinner(
        "Salvataggio note..."
    ):

        for _, row in (
            edited_df.iterrows()
        ):

            row_id = int(
                row["id"]
            )

            new_note = (
                row.get("note")
                or ""
            )

            old_note = (
                original_notes.get(
                    row_id,
                    ""
                )
            )

            if new_note != old_note:

                try:

                    save_note(
                        row_id,
                        new_note
                    )

                    changed += 1

                except Exception as exc:

                    errors.append(
                        f"ID {row_id}: "
                        f"{exc}"
                    )


    if errors:

        st.error(
            f"Salvate {changed} note, "
            f"ma si sono verificati "
            f"{len(errors)} errori."
        )

        with st.expander(
            "Dettagli errori"
        ):

            for error in errors:

                st.write(
                    error
                )

    else:

        st.success(
            f"Note salvate: "
            f"{changed}"
        )


# ============================================================
# DOWNLOAD CSV
# ============================================================

export_df = view_df[
    [
        "titolo",
        "citta",
        "telefono",
        "url",
        "note",
        "active",
        "first_seen",
        "last_seen",
    ]
].copy()


csv_data = (

    export_df
    .to_csv(
        index=False
    )
    .encode(
        "utf-8-sig"
    )

)


st.download_button(

    "📥 Scarica CSV",

    data=csv_data,

    file_name=(
        "archivio_annunci_romagna.csv"
    ),

    mime="text/csv",

)
