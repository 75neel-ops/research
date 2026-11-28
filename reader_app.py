import html
import os
import re
from urllib.parse import quote

import requests
import streamlit as st


# ---------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="World Reading Room",
    page_icon="📖",
    layout="wide",
)

HEADERS = {"User-Agent": "WorldReadingRoom/1.0"}
CATEGORIES = ["Books", "Magazines", "Newspapers", "Little magazines"]


def get_secret(name):
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""
    return value or os.getenv(name, "")


def clean_text(value):
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def as_text(value):
    """Convert API fields that might be strings, lists, or dictionaries."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(as_text(item) for item in value if item)
    if isinstance(value, dict):
        parts = []
        for item in value.values():
            if isinstance(item, list):
                parts.extend(str(part) for part in item if part)
            elif item:
                parts.append(str(item))
        return "; ".join(parts)
    return ""


def make_item(
    title,
    creator="",
    date="",
    language="",
    source="",
    record_url="",
    reading_url="",
    description="",
    access="Catalogue record only",
):
    return {
        "title": clean_text(title) or "Untitled",
        "creator": clean_text(creator),
        "date": clean_text(date),
        "language": clean_text(language),
        "source": source,
        "record_url": record_url or "",
        "reading_url": reading_url or "",
        "description": clean_text(description),
        "access": access,
    }


# ---------------------------------------------------------------------
# Search services
# ---------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def search_openlibrary(query, limit):
    response = requests.get(
        "https://openlibrary.org/search.json",
        params={
            "q": query,
            "limit": limit,
            "fields": (
                "key,title,author_name,first_publish_year,"
                "language,ebook_access"
            ),
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    items = []
    for result in response.json().get("docs", []):
        key = result.get("key", "")
        record_url = f"https://openlibrary.org{key}" if key else ""
        ebook_access = result.get("ebook_access", "")

        items.append(
            make_item(
                title=result.get("title"),
                creator=", ".join(result.get("author_name", [])[:5]),
                date=result.get("first_publish_year", ""),
                language=", ".join(result.get("language", [])[:3]),
                source="Open Library",
                record_url=record_url,
                reading_url=record_url if ebook_access in {
                    "public", "borrowable"
                } else "",
                access=(
                    "Provider lists a reading or borrowing option; availability varies"
                    if ebook_access in {"public", "borrowable"}
                    else "Catalogue record; reading access not confirmed"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_gutenberg(query, limit):
    response = requests.get(
        "https://gutendex.com/books/",
        params={"search": query},
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    items = []
    for result in response.json().get("results", [])[:limit]:
        authors = ", ".join(
            person.get("name", "")
            for person in result.get("authors", [])
            if person.get("name")
        )
        formats = result.get("formats", {})
        text_url = (
            formats.get("text/plain; charset=utf-8")
            or formats.get("text/plain; charset=us-ascii")
            or formats.get("text/plain")
            or ""
        )
        book_id = result.get("id")
        record_url = (
            f"https://www.gutenberg.org/ebooks/{book_id}"
            if book_id else ""
        )

        items.append(
            make_item(
                title=result.get("title"),
                creator=authors,
                language=", ".join(result.get("languages", [])),
                source="Project Gutenberg",
                record_url=record_url,
                reading_url=text_url,
                description="Project Gutenberg ebook record.",
                access=(
                    "Plain-text reading link listed"
                    if text_url
                    else "Gutenberg catalogue record"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_internet_archive(query, limit):
    params = [
        ("q", f"({query}) AND mediatype:texts"),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "date"),
        ("fl[]", "creator"),
        ("fl[]", "description"),
        ("rows", str(limit)),
        ("page", "1"),
        ("output", "json"),
        ("sort[]", "score desc"),
    ]

    response = requests.get(
        "https://archive.org/advancedsearch.php",
        params=params,
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    docs = response.json().get("response", {}).get("docs", [])
    items = []

    for result in docs:
        identifier = as_text(result.get("identifier"))
        record_url = (
            f"https://archive.org/details/{identifier}"
            if identifier else "https://archive.org/"
        )

        items.append(
            make_item(
                title=as_text(result.get("title")),
                creator=as_text(result.get("creator")),
                date=as_text(result.get("date")),
                description=as_text(result.get("description")),
                source="Internet Archive",
                record_url=record_url,
                access=(
                    "Item page listed; check its reader and access conditions"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_library_of_congress(query, limit, newspapers=False):
    endpoint = (
        "https://www.loc.gov/newspapers/"
        if newspapers
        else "https://www.loc.gov/search/"
    )

    response = requests.get(
        endpoint,
        params={"fo": "json", "q": query, "c": limit},
        headers=HEADERS,
        timeout=40,
    )
    response.raise_for_status()

    items = []
    for result in response.json().get("results", []):
        descriptions = result.get("description") or []
        if isinstance(descriptions, str):
            descriptions = [descriptions]

        reading_url = ""
        online_formats = result.get("online_format") or []
        resources = result.get("resources") or []

        if online_formats:
            first = online_formats[0]
            if isinstance(first, dict):
                reading_url = first.get("url", "")
            elif isinstance(first, str):
                reading_url = first
        elif resources and isinstance(resources[0], dict):
            reading_url = resources[0].get("url", "")

        items.append(
            make_item(
                title=result.get("title"),
                date=result.get("date", ""),
                description=" ".join(str(x) for x in descriptions),
                source=(
                    "Library of Congress Newspapers"
                    if newspapers else "Library of Congress"
                ),
                record_url=result.get("url", ""),
                reading_url=reading_url,
                access=(
                    "Online resource listed; access not verified"
                    if reading_url
                    else "Catalogue record only"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_europeana(query, limit, api_key):
    if not api_key:
        raise ValueError(
            "EUROPEANA_API_KEY is missing from Streamlit Secrets."
        )

    response = requests.get(
        "https://api.europeana.eu/record/v2/search.json",
        params={
            "wskey": api_key,
            "query": query,
            "rows": limit,
            "profile": "rich",
        },
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    items = []
    for result in response.json().get("items", []):
        record_url = (
            as_text(result.get("edmIsShownAt"))
            or result.get("guid", "")
        )
        reading_url = as_text(result.get("edmIsShownBy"))
        provider = as_text(result.get("dataProvider"))

        items.append(
            make_item(
                title=as_text(result.get("title")),
                creator=as_text(result.get("dcCreator")),
                date=as_text(result.get("year")),
                language=as_text(result.get("language")),
                source=f"Europeana — {provider}" if provider else "Europeana",
                record_url=record_url,
                reading_url=reading_url,
                description=as_text(result.get("dcDescription")),
                access=(
                    "Digital item link listed; not verified"
                    if reading_url
                    else "Europeana catalogue record"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_trove(query, limit, api_key):
    if not api_key:
        raise ValueError(
            "TROVE_API_KEY is missing from Streamlit Secrets."
        )

    response = requests.get(
        "https://api.trove.nla.gov.au/v3/result",
        params={
            "key": api_key,
            "q": query,
            "zone": "newspaper",
            "encoding": "json",
            "n": limit,
        },
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    zones = response.json().get("response", {}).get("zone", [])
    items = []

    for zone in zones:
        articles = zone.get("records", {}).get("article", [])
        if isinstance(articles, dict):
            articles = [articles]

        for result in articles:
            items.append(
                make_item(
                    title=(
                        result.get("heading")
                        or result.get("title")
                        or "Trove newspaper article"
                    ),
                    date=result.get("date", ""),
                    description=result.get("snippet", ""),
                    source="Trove",
                    record_url=result.get("troveUrl", ""),
                    access=(
                        "Trove record; OCR snippets may contain errors"
                    ),
                )
            )

    return items


# ---------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------

def available_sources(category):
    if category == "Books":
        return [
            "Open Library",
            "Project Gutenberg",
            "Internet Archive",
            "Library of Congress",
            "Europeana",
        ]

    if category == "Newspapers":
        return [
            "Internet Archive",
            "Library of Congress",
            "Europeana",
            "Trove",
        ]

    if category == "Magazines":
        return [
            "Internet Archive",
            "Library of Congress",
            "Europeana",
        ]

    return [
        "Internet Archive",
        "Library of Congress",
        "Europeana",
    ]


def search_source(source, query, limit):
    if source == "Open Library":
        return search_openlibrary(query, limit)
    if source == "Project Gutenberg":
        return search_gutenberg(query, limit)
    if source == "Internet Archive":
        return search_internet_archive(query, limit)
    if source == "Library of Congress":
        return search_library_of_congress(query, limit)
    if source == "Library of Congress Newspapers":
        return search_library_of_congress(
            query, limit, newspapers=True
        )
    if source == "Europeana":
        return search_europeana(
            query,
            limit,
            get_secret("EUROPEANA_API_KEY"),
        )
    if source == "Trove":
        return search_trove(
            query,
            limit,
            get_secret("TROVE_API_KEY"),
        )
    return []


# ---------------------------------------------------------------------
# Publication websites and archive links
# ---------------------------------------------------------------------

def publication_links(category, query):
    encoded = quote((query or "").strip(), safe="")
    normalized = (query or "").strip().lower()

    st.markdown("#### Find this publication’s website or archive")

    # Curated direct link. Add more known publication sites here if desired.
    known_sites = {
        "anandabazar patrika": (
            "Anandabazar Patrika official website",
            "https://www.anandabazar.com/",
        ),
    }

    if normalized in known_sites:
        name, url = known_sites[normalized]
        st.markdown(f"**{name}:** [Open website]({url})")

    if category == "Newspapers":
        searches = [
            (
                "Search for the newspaper’s official website",
                "https://www.google.com/search?q="
                + quote(f'"{query}" official newspaper website', safe=""),
            ),
            (
                "Search for historical issues and archives",
                "https://www.google.com/search?q="
                + quote(f'"{query}" newspaper archive historical issues', safe=""),
            ),
            (
                "Search Internet Archive",
                f"https://archive.org/search?query={quote(query + ' newspaper', safe='')}",
            ),
            (
                "Search Europeana",
                f"https://www.europeana.eu/en/search?query={encoded}",
            ),
        ]
    elif category in {"Magazines", "Little magazines"}:
        searches = [
            (
                "Search for the magazine’s official website",
                "https://www.google.com/search?q="
                + quote(f'"{query}" magazine official website', safe=""),
            ),
            (
                "Search for digitized issues",
                "https://www.google.com/search?q="
                + quote(f'"{query}" magazine archive digitized issues', safe=""),
            ),
            (
                "Search Internet Archive",
                f"https://archive.org/search?query={quote(query + ' magazine', safe='')}",
            ),
            (
                "Search Google Books",
                f"https://books.google.com/books?q={encoded}",
            ),
            (
                "Search Europeana",
                f"https://www.europeana.eu/en/search?query={encoded}",
            ),
            (
                "Search Modernist Journals Project",
                "https://www.google.com/search?q="
                + quote(f'site:modjourn.org "{query}"', safe=""),
            ),
            (
                "Search Media History Digital Library",
                "https://lantern.mediahist.org/",
            ),
        ]
    else:
        searches = [
            (
                "Search for an official publication website",
                "https://www.google.com/search?q="
                + quote(f'"{query}" official website', safe=""),
            ),
            (
                "Search Internet Archive",
                f"https://archive.org/search?query={encoded}",
            ),
            (
                "Search Europeana",
                f"https://www.europeana.eu/en/search?query={encoded}",
            ),
        ]

    for label, url in searches:
        st.markdown(f"- [{label}]({url})")

    st.caption(
        "Except for the specifically listed Anandabazar Patrika website, "
        "these are search links—not verified publisher websites. Confirm "
        "the publication’s identity on the site you open."
    )


def regional_catalogue_links():
    links = [
        (
            "National Digital Library of India",
            "India",
            "https://ndl.iitkgp.ac.in/",
        ),
        (
            "National Library of India",
            "India",
            "https://www.nationallibrary.gov.in/",
        ),
        (
            "NewspaperSG — National Library Board",
            "Singapore",
            "https://eresources.nlb.gov.sg/newspapers/",
        ),
        (
            "National Library of Malaysia",
            "Malaysia",
            "https://www.pnm.gov.my/",
        ),
        (
            "Khastara Digital Collections",
            "Indonesia",
            "https://khastara.perpusnas.go.id/",
        ),
        (
            "National Library of the Philippines",
            "Philippines",
            "https://web.nlp.gov.ph/",
        ),
        (
            "National Library of Thailand",
            "Thailand",
            "https://www.nlt.go.th/",
        ),
        (
            "National Library of Vietnam",
            "Vietnam",
            "https://nlv.gov.vn/",
        ),
        (
            "National Library of Cambodia",
            "Cambodia",
            "https://www.nlc.gov.kh/",
        ),
        (
            "National Library of Laos",
            "Laos",
            "https://www.lao-nlp.org/",
        ),
        (
            "National Library of Myanmar",
            "Myanmar",
            "https://www.nlm.gov.mm/",
        ),
        (
            "National Archives of Brunei",
            "Brunei",
            "https://www.arkib.gov.bn/",
        ),
    ]

    with st.expander("India and ASEAN library/catalogue links"):
        st.caption(
            "These are official-site links, not direct searches by this app. "
            "Online newspaper holdings and access vary by institution."
        )
        for name, country, url in links:
            st.markdown(f"- **{name}** ({country}): [Open site]({url})")


# ---------------------------------------------------------------------
# Results and in-app reader
# ---------------------------------------------------------------------

def show_results(items, category):
    if not items:
        st.info("No results yet. Search above or try broader keywords.")
        return

    rows = []
    for item in items:
        rows.append({
            "Title": item.get("title", ""),
            "Creator/editor": item.get("creator", ""),
            "Date": item.get("date", ""),
            "Language": item.get("language", ""),
            "Source": item.get("source", ""),
            "Access information": item.get("access", ""),
            "Catalogue record": item.get("record_url", ""),
            "Listed reading link": item.get("reading_url", ""),
        })

    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Catalogue record": st.column_config.LinkColumn(
                "Catalogue record"
            ),
            "Listed reading link": st.column_config.LinkColumn(
                "Listed reading link"
            ),
        },
    )

    st.caption(
        "A listed link is not verified. Access may require an account, "
        "borrowing, payment, permission, or a visit to the holding library."
    )

    for index, item in enumerate(items):
        with st.expander(f"{index + 1}. {item['title']}"):
            st.write(
                f"**Creator/editor:** {item.get('creator') or 'Not listed'}  \n"
                f"**Date:** {item.get('date') or 'Not listed'}  \n"
                f"**Language:** {item.get('language') or 'Not listed'}  \n"
                f"**Source:** {item.get('source') or 'Not listed'}  \n"
                f"**Access:** {item.get('access') or 'Not listed'}"
            )

            if item.get("record_url"):
                st.markdown(
                    f"[Open catalogue/item page]({item['record_url']})"
                )
            if item.get("reading_url"):
                st.markdown(
                    f"[Open listed reading link]({item['reading_url']})"
                )
            if item.get("description"):
                st.write(item["description"])

            if (
                item.get("source") == "Project Gutenberg"
                and item.get("reading_url")
            ):
                if st.button(
                    "Read text in app",
                    key=f"read_{category}_{index}",
                ):
                    try:
                        with st.spinner("Loading text..."):
                            response = requests.get(
                                item["reading_url"],
                                headers=HEADERS,
                                timeout=45,
                            )
                            response.raise_for_status()

                        st.session_state["reader_text"] = response.text[:120000]
                        st.session_state["reader_title"] = item["title"]
                    except Exception as exc:
                        st.error(f"Could not load the text: {exc}")

    if st.session_state.get("reader_text"):
        st.divider()
        st.subheader(
            f"Reader: {st.session_state.get('reader_title', 'Selected text')}"
        )
        st.caption(
            "This reader loads Project Gutenberg plain text. Check the "
            "provider’s terms and your country’s copyright rules."
        )
        st.text_area(
            "Text",
            value=st.session_state["reader_text"],
            height=500,
            key="reader_text_area",
        )


# ---------------------------------------------------------------------
# Main interface
# ---------------------------------------------------------------------

st.title("📖 World Reading Room")
st.write(
    "Discover books, magazines, newspapers, and little magazines in "
    "selected library catalogues and open collections."
)

st.info(
    "No single service covers every library worldwide. Results and reading "
    "access depend on each provider, country, and item."
)

tabs = st.tabs(CATEGORIES)

for category, tab in zip(CATEGORIES, tabs):
    with tab:
        st.subheader(category)

        if category == "Little magazines":
            st.write(
                "Little magazines are held across specialist, university, "
                "national, and independent collections. Coverage is incomplete."
            )

        sources = available_sources(category)

        with st.form(f"search_form_{category}"):
            query = st.text_input(
                "Title, author/editor, place, language, date, or topic",
                key=f"query_{category}",
                placeholder=(
                    "For example: Anandabazar Patrika, 1910 Bombay, "
                    "or a little magazine title"
                ),
            )

            limit = st.slider(
                "Maximum results per source",
                min_value=5,
                max_value=30,
                value=10,
                key=f"limit_{category}",
            )

            selected_sources = st.multiselect(
                "Sources",
                options=sources,
                default=sources,
                key=f"sources_{category}",
            )

            submitted = st.form_submit_button(
                f"Search {category.lower()}",
                type="primary",
            )

        if submitted:
            if not query.strip():
                st.error("Enter a title, name, place, date, or topic.")
            elif not selected_sources:
                st.error("Select at least one source.")
            else:
                found = []

                for source in selected_sources:
                    try:
                        with st.spinner(f"Searching {source}..."):
                            found.extend(search_source(source, query, int(limit)))
                    except Exception as exc:
                        st.warning(f"{source} search failed: {exc}")

                # The LOC has a newspaper-specific endpoint, so search it too.
                if (
                    category == "Newspapers"
                    and "Library of Congress" in selected_sources
                ):
                    try:
                        with st.spinner(
                            "Searching Library of Congress newspaper records..."
                        ):
                            found.extend(
                                search_library_of_congress(
                                    query,
                                    int(limit),
                                    newspapers=True,
                                )
                            )
                    except Exception as exc:
                        st.warning(
                            f"Library of Congress newspaper search failed: {exc}"
                        )

                st.session_state[f"results_{category}"] = found
                st.session_state[f"saved_query_{category}"] = query

        results = st.session_state.get(f"results_{category}", [])
        saved_query = st.session_state.get(
            f"saved_query_{category}",
            st.session_state.get(f"query_{category}", ""),
        )

        show_results(results, category)

        if saved_query.strip():
            publication_links(category, saved_query)

        if category in {"Newspapers", "Magazines", "Little magazines"}:
            regional_catalogue_links()

st.divider()
st.caption(
    "Respect each provider’s copyright, lending, and access conditions. "
    "This app does not bypass subscriptions or access controls."
)
