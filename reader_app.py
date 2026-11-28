import html
import os
import re
from urllib.parse import quote

import requests
import streamlit as st


st.set_page_config(
    page_title="World Reading Room",
    page_icon="📖",
    layout="wide",
)

HEADERS = {"User-Agent": "WorldReadingRoom/1.0"}
CATEGORIES = [
    "Books",
    "Magazines",
    "Newspapers",
    "Little magazines",
]


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
        "record_url": record_url,
        "reading_url": reading_url,
        "description": clean_text(description),
        "access": access,
    }


# -------------------------------------------------------------------
# Search providers
# -------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def search_openlibrary(query, limit):
    response = requests.get(
        "https://openlibrary.org/search.json",
        params={
            "q": query,
            "limit": limit,
            "fields": "key,title,author_name,first_publish_year,language,ebook_access",
        },
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    items = []
    for result in response.json().get("docs", []):
        key = result.get("key", "")
        record_url = f"https://openlibrary.org{key}" if key else ""
        access = result.get("ebook_access", "")
        reading_url = ""

        if access in {"public", "borrowable"} and key:
            reading_url = record_url

        items.append(
            make_item(
                title=result.get("title"),
                creator=", ".join(result.get("author_name", [])[:5]),
                date=result.get("first_publish_year", ""),
                language=", ".join(result.get("language", [])[:3]),
                source="Open Library",
                record_url=record_url,
                reading_url=reading_url,
                access=(
                    "Provider lists a reading/borrow option; availability varies"
                    if reading_url
                    else "Catalogue record; reading access not confirmed"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_gutenberg(query, limit):
    response = requests.get(
        "https://gutendex.com/books/",
        params={"search": query, "languages": "en"},
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

        items.append(
            make_item(
                title=result.get("title"),
                creator=authors,
                date=result.get("copyright"),
                language=", ".join(result.get("languages", [])),
                source="Project Gutenberg",
                record_url=f"https://www.gutenberg.org/ebooks/{result.get('id')}",
                reading_url=text_url,
                description="Public-domain ebook listing from Project Gutenberg.",
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
    response = requests.get(
        "https://archive.org/advancedsearch.php",
        params=[
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
        ],
        headers=HEADERS,
        timeout=45,
    )
    response.raise_for_status()

    docs = response.json().get("response", {}).get("docs", [])
    items = []

    for result in docs:
        identifier = result.get("identifier", "")
        record_url = (
            f"https://archive.org/details/{identifier}"
            if identifier else "https://archive.org/"
        )

        creator = result.get("creator", "")
        if isinstance(creator, list):
            creator = ", ".join(str(x) for x in creator)

        description = result.get("description", "")
        if isinstance(description, list):
            description = " ".join(str(x) for x in description)

        items.append(
            make_item(
                title=result.get("title"),
                creator=creator,
                date=result.get("date", ""),
                source="Internet Archive",
                record_url=record_url,
                description=description,
                access=(
                    "Item page listed; check its reader and access conditions"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_loc(query, limit, newspapers=False):
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

        online_formats = result.get("online_format") or []
        resources = result.get("resources") or []
        reading_url = ""

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
                source=(
                    "Library of Congress Newspapers"
                    if newspapers else "Library of Congress"
                ),
                record_url=result.get("url", ""),
                reading_url=reading_url,
                description=" ".join(str(x) for x in descriptions),
                access=(
                    "Online resource listed; access not verified"
                    if reading_url
                    else "Catalogue record only"
                ),
            )
        )

    return items


def europeana_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(europeana_value(x) for x in value if x)
    if isinstance(value, dict):
        parts = []
        for item in value.values():
            if isinstance(item, list):
                parts.extend(str(x) for x in item if x)
            elif item:
                parts.append(str(item))
        return "; ".join(parts)
    return ""


@st.cache_data(ttl=1800, show_spinner=False)
def search_europeana(query, limit, api_key):
    if not api_key:
        raise ValueError("Add EUROPEANA_API_KEY in Streamlit Secrets.")

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
        provider = europeana_value(result.get("dataProvider"))
        description = europeana_value(result.get("dcDescription"))
        digital_link = europeana_value(result.get("edmIsShownBy"))
        record_url = (
            europeana_value(result.get("edmIsShownAt"))
            or result.get("guid", "")
        )

        items.append(
            make_item(
                title=europeana_value(result.get("title")),
                creator=europeana_value(result.get("dcCreator")),
                date=europeana_value(result.get("year")),
                language=europeana_value(result.get("language")),
                source=f"Europeana — {provider}" if provider else "Europeana",
                record_url=record_url,
                reading_url=digital_link,
                description=description,
                access=(
                    "Digital item link listed; not verified"
                    if digital_link
                    else "Europeana catalogue record"
                ),
            )
        )

    return items


@st.cache_data(ttl=1800, show_spinner=False)
def search_trove(query, limit, api_key):
    if not api_key:
        raise ValueError("Add TROVE_API_KEY in Streamlit Secrets.")

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
                    source="Trove",
                    record_url=result.get("troveUrl", ""),
                    description=result.get("snippet", ""),
                    access="Trove record; OCR snippets may contain errors",
                )
            )

    return items


# -------------------------------------------------------------------
# Search routing and regional catalogue links
# -------------------------------------------------------------------

def sources_for_category(category):
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
        return search_loc(query, limit, newspapers=False)
    if source == "Library of Congress Newspapers":
        return search_loc(query, limit, newspapers=True)
    if source == "Europeana":
        return search_europeana(
            query, limit, get_secret("EUROPEANA_API_KEY")
        )
    if source == "Trove":
        return search_trove(query, limit, get_secret("TROVE_API_KEY"))
    return []


def regional_links(category, query):
    encoded = quote(query or "")

    links = [
        (
            "National Digital Library of India",
            "https://ndl.iitkgp.ac.in/",
            "India",
        ),
        (
            "National Library of India",
            "https://www.nationallibrary.gov.in/",
            "India",
        ),
        (
            "National Library Board Singapore — NewspaperSG",
            "https://eresources.nlb.gov.sg/newspapers/",
            "Singapore",
        ),
        (
            "National Library of Malaysia",
            "https://www.pnm.gov.my/",
            "Malaysia",
        ),
        (
            "Khastara Digital Collections",
            "https://khastara.perpusnas.go.id/",
            "Indonesia",
        ),
        (
            "National Library of the Philippines",
            "https://web.nlp.gov.ph/",
            "Philippines",
        ),
        (
            "National Library of Thailand",
            "https://www.nlt.go.th/",
            "Thailand",
        ),
        (
            "National Library of Vietnam",
            "https://nlv.gov.vn/",
            "Vietnam",
        ),
        (
            "National Library of Cambodia",
            "https://www.nlc.gov.kh/",
            "Cambodia",
        ),
        (
            "National Library of Laos",
            "https://www.lao-nlp.org/",
            "Laos",
        ),
        (
            "National Library of Myanmar",
            "https://www.nlm.gov.mm/",
            "Myanmar",
        ),
        (
            "National Archives of Brunei",
            "https://www.arkib.gov.bn/",
            "Brunei",
        ),
        (
            "Modernist Journals Project",
            "https://modjourn.org/",
            "Little magazines",
        ),
        (
            "Media History Digital Library — Lantern",
            "https://lantern.mediahist.org/",
            "Magazines and media history",
        ),
    ]

    st.markdown("#### More library and specialist collections")
    st.caption(
        "These are official-site links, not automatic searches by this app. "
        "Coverage and reading access vary by institution."
    )

    for name, url, region in links:
        st.markdown(f"- **{name}** ({region}): [Open site]({url})")

    st.markdown(
        f"General Internet Archive search: "
        f"[Search this term]("
        f"https://archive.org/search?query={encoded})"
    )


# -------------------------------------------------------------------
# Results display and reader
# -------------------------------------------------------------------

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
        "A link listed by a provider is not verified. Some items may require "
        "an account, borrowing, payment, or permission."
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

            # Offer in-app reading only for Project Gutenberg plain text.
            if item.get("source") == "Project Gutenberg" and item.get("reading_url"):
                if st.button(
                    "Read text in app",
                    key=f"read_{category}_{index}",
                ):
                    try:
                        with st.spinner("Loading public-domain text..."):
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
        st.markdown("---")
        st.subheader(
            f"Reader: {st.session_state.get('reader_title', 'Selected text')}"
        )
        st.caption(
            "This text is supplied by Project Gutenberg. Check the source "
            "page for its terms and regional availability."
        )
        st.text_area(
            "Text",
            value=st.session_state["reader_text"],
            height=500,
            key="gutenberg_reader",
        )


# -------------------------------------------------------------------
# Main app interface
# -------------------------------------------------------------------

st.title("📖 World Reading Room")
st.write(
    "Discover books, magazines, newspapers, and little magazines from "
    "participating open collections and library catalogues."
)

st.info(
    "There is no single service covering every library worldwide. This app "
    "searches selected catalogues and open collections; availability and "
    "copyright status vary by item and country."
)

tabs = st.tabs(CATEGORIES)

for category, tab in zip(CATEGORIES, tabs):
    with tab:
        st.subheader(category)

        if category == "Little magazines":
            st.write(
                "Little magazines are scattered across specialist, university, "
                "and national collections. Coverage will be incomplete."
            )

        with st.form(f"search_form_{category}"):
            query = st.text_input(
                "Title, author/editor, place, language, date, or topic",
                key=f"query_{category}",
            )

            limit = st.slider(
                "Maximum results per source",
                min_value=5,
                max_value=30,
                value=10,
                key=f"limit_{category}",
            )

            available_sources = sources_for_category(category)
            selected_sources = st.multiselect(
                "Sources",
                options=available_sources,
                default=available_sources,
                key=f"sources_{category}",
            )

            submitted = st.form_submit_button(
                f"Search {category.lower()}",
                type="primary",
            )

        if submitted:
            if not query.strip():
                st.error("Enter a search term.")
            elif not selected_sources:
                st.error("Select at least one source.")
            else:
                results = []
                errors = []

                for source in selected_sources:
                    try:
                        with st.spinner(f"Searching {source}..."):
                            results.extend(
                                search_source(source, query, int(limit))
                            )
                    except Exception as exc:
                        errors.append(f"{source}: {exc}")

                # LOC newspaper endpoint is useful specifically for newspapers.
                if category == "Newspapers" and "Library of Congress" in selected_sources:
                    try:
                        with st.spinner("Searching Library of Congress newspapers..."):
                            results.extend(
                                search_source(
                                    "Library of Congress Newspapers",
                                    query,
                                    int(limit),
                                )
                            )
                    except Exception as exc:
                        errors.append(f"Library of Congress newspapers: {exc}")

                st.session_state[f"results_{category}"] = results
                st.session_state[f"query_saved_{category}"] = query

                for error in errors:
                    st.warning(error)

        current_results = st.session_state.get(
            f"results_{category}",
            [],
        )
        current_query = st.session_state.get(
            f"query_saved_{category}",
            st.session_state.get(f"query_{category}", ""),
        )

        show_results(current_results, category)

        if current_query:
            regional_links(category, current_query)

st.divider()
st.caption(
    "Respect each library's copyright, borrowing, and access conditions. "
    "The app does not bypass subscriptions or access controls."
)
