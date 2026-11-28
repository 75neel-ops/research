import os
from urllib.parse import quote

import requests
import streamlit as st


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

st.set_page_config(
    page_title="Movie Research",
    page_icon="🎬",
    layout="wide",
)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
HEADERS = {
    "User-Agent": "MovieResearchApp/1.0 (Streamlit)"
}

# TMDb region codes used for country-specific provider searches.
COUNTRIES = {
    "India": "IN",
    "United States": "US",
    "United Kingdom": "GB",
    "Australia": "AU",
    "Canada": "CA",
    "Singapore": "SG",
    "Malaysia": "MY",
    "Indonesia": "ID",
    "Philippines": "PH",
    "Thailand": "TH",
    "Vietnam": "VN",
    "Japan": "JP",
    "South Korea": "KR",
    "China": "CN",
    "France": "FR",
    "Germany": "DE",
    "Italy": "IT",
    "Spain": "ES",
    "Netherlands": "NL",
    "Sweden": "SE",
    "South Africa": "ZA",
    "Nigeria": "NG",
    "Kenya": "KE",
    "Egypt": "EG",
    "Brazil": "BR",
    "Mexico": "MX",
    "New Zealand": "NZ",
}


# ---------------------------------------------------------------------
# Secrets and API helpers
# ---------------------------------------------------------------------

def get_secret(name):
    """Read a Streamlit secret or an environment variable."""
    try:
        value = st.secrets.get(name, "")
    except Exception:
        value = ""

    return value or os.getenv(name, "")


TMDB_API_KEY = get_secret("TMDB_API_KEY")

if not TMDB_API_KEY:
    st.error(
        "TMDB_API_KEY is missing. Add it in this app's "
        "Streamlit → Manage app → Settings → Secrets."
    )
    st.stop()


def tmdb_get(endpoint, params=None):
    """Call TMDb without displaying the API key in error messages."""
    request_params = dict(params or {})
    request_params["api_key"] = TMDB_API_KEY

    try:
        response = requests.get(
            f"{TMDB_BASE}/{endpoint.lstrip('/')}",
            params=request_params,
            headers=HEADERS,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise RuntimeError("Could not connect to TMDb.") from exc

    if not response.ok:
        # Do not show response.url; it contains the API key.
        raise RuntimeError(
            f"TMDb returned HTTP {response.status_code}. "
            "Check that your API key is valid and try again later."
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("TMDb returned an unreadable response.") from exc


@st.cache_data(ttl=86400, show_spinner=False)
def get_tmdb_languages(api_key):
    try:
        data = tmdb_get("configuration/languages")
        languages = {}

        for item in data:
            code = item.get("iso_639_1")
            name = item.get("english_name") or item.get("name")
            if code and name:
                languages[code] = name

        return languages
    except Exception:
        # Common language fallback if the language endpoint is unavailable.
        return {
            "en": "English",
            "hi": "Hindi",
            "bn": "Bengali",
            "ta": "Tamil",
            "te": "Telugu",
            "ml": "Malayalam",
            "kn": "Kannada",
            "mr": "Marathi",
            "ur": "Urdu",
            "fr": "French",
            "es": "Spanish",
            "de": "German",
            "it": "Italian",
            "ja": "Japanese",
            "ko": "Korean",
            "zh": "Chinese",
            "ar": "Arabic",
            "pt": "Portuguese",
            "ru": "Russian",
            "th": "Thai",
            "id": "Indonesian",
            "ms": "Malay",
            "vi": "Vietnamese",
        }


@st.cache_data(ttl=1800, show_spinner=False)
def search_tmdb_movies(query, api_key):
    return tmdb_get(
        "search/movie",
        {
            "query": query,
            "include_adult": "false",
            "page": 1,
        },
    ).get("results", [])


@st.cache_data(ttl=3600, show_spinner=False)
def get_movie_details(movie_id, api_key):
    return tmdb_get(f"movie/{movie_id}")


@st.cache_data(ttl=3600, show_spinner=False)
def get_movie_credits(movie_id, api_key):
    return tmdb_get(f"movie/{movie_id}/credits")


@st.cache_data(ttl=3600, show_spinner=False)
def get_movie_watch_providers(movie_id, api_key):
    return tmdb_get(f"movie/{movie_id}/watch/providers")


@st.cache_data(ttl=3600, show_spinner=False)
def get_movie_videos(movie_id, api_key):
    return tmdb_get(f"movie/{movie_id}/videos")


@st.cache_data(ttl=86400, show_spinner=False)
def search_wikidata_awards(tmdb_id):
    """
    Look for award items associated with this TMDb movie ID in Wikidata.
    This is supplementary and incomplete, not an authoritative awards record.
    """
    query = f"""
    SELECT DISTINCT ?awardLabel WHERE {{
      ?film wdt:P4947 "{int(tmdb_id)}".
      ?film wdt:P166 ?award.
      SERVICE wikibase:label {{
        bd:serviceParam wikibase:language "en".
      }}
    }}
    LIMIT 30
    """

    try:
        response = requests.get(
            "https://query.wikidata.org/sparql",
            params={"query": query, "format": "json"},
            headers={
                "User-Agent": "MovieResearchApp/1.0 (research tool)",
                "Accept": "application/sparql-results+json",
            },
            timeout=25,
        )

        if not response.ok:
            return []

        data = response.json()
        bindings = (
            data.get("results", {})
            .get("bindings", [])
        )

        awards = []
        for item in bindings:
            label = item.get("awardLabel", {}).get("value")
            if label and label not in awards:
                awards.append(label)

        return awards

    except Exception:
        return []


# ---------------------------------------------------------------------
# Archive search helpers
# ---------------------------------------------------------------------

@st.cache_data(ttl=1800, show_spinner=False)
def search_internet_archive_films(query, limit):
    params = [
        ("q", f"({query}) AND mediatype:movies"),
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

    try:
        response = requests.get(
            "https://archive.org/advancedsearch.php",
            params=params,
            headers=HEADERS,
            timeout=35,
        )
        response.raise_for_status()
        documents = response.json().get("response", {}).get("docs", [])
    except Exception:
        return []

    results = []

    for item in documents:
        identifier = item.get("identifier", "")
        if not identifier:
            continue

        title = item.get("title", "")
        creator = item.get("creator", "")
        description = item.get("description", "")

        if isinstance(title, list):
            title = "; ".join(str(x) for x in title)
        if isinstance(creator, list):
            creator = ", ".join(str(x) for x in creator)
        if isinstance(description, list):
            description = " ".join(str(x) for x in description)

        results.append({
            "title": title or "Untitled archive record",
            "date": item.get("date", ""),
            "creator": creator or "",
            "description": description or "",
            "record_url": f"https://archive.org/details/{identifier}",
            "source": "Internet Archive",
            "access_note": (
                "Archive item page found. Check its rights statement and "
                "available playback options."
            ),
        })

    return results


@st.cache_data(ttl=1800, show_spinner=False)
def search_europeana_films(query, limit, api_key):
    if not api_key:
        return []

    try:
        response = requests.get(
            "https://api.europeana.eu/record/v2/search.json",
            params={
                "wskey": api_key,
                "query": query,
                "rows": limit,
                "profile": "rich",
            },
            headers=HEADERS,
            timeout=35,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
    except Exception:
        return []

    def value_as_text(value):
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "; ".join(value_as_text(x) for x in value if x)
        if isinstance(value, dict):
            parts = []
            for item in value.values():
                if isinstance(item, list):
                    parts.extend(str(x) for x in item if x)
                elif item:
                    parts.append(str(item))
            return "; ".join(parts)
        return ""

    results = []

    for item in items:
        record_url = (
            value_as_text(item.get("edmIsShownAt"))
            or item.get("guid", "")
        )
        digital_url = value_as_text(item.get("edmIsShownBy"))
        provider = value_as_text(item.get("dataProvider"))

        results.append({
            "title": value_as_text(item.get("title")) or "Untitled record",
            "date": value_as_text(item.get("year")),
            "creator": value_as_text(item.get("dcCreator")),
            "description": value_as_text(item.get("dcDescription")),
            "record_url": record_url,
            "digital_url": digital_url,
            "source": f"Europeana — {provider}" if provider else "Europeana",
            "access_note": (
                "Europeana lists a digital item link; it has not been "
                "verified and may have access restrictions."
            ),
        })

    return results


# ---------------------------------------------------------------------
# Film presentation helpers
# ---------------------------------------------------------------------

def format_money(amount):
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return "Not reported by TMDb"

    if amount <= 0:
        return "Not reported by TMDb"

    return f"${amount:,.0f} USD (TMDb figure)"


def names_from_credit(credits, jobs):
    names = []

    for person in credits.get("crew", []):
        if person.get("job") in jobs and person.get("name"):
            if person["name"] not in names:
                names.append(person["name"])

    return names


def display_watch_providers(provider_data, region_code, country_name):
    region = (
        provider_data.get("results", {})
        .get(region_code)
    )

    st.markdown(f"#### Viewing options reported for {country_name}")

    if not region:
        st.info(
            "TMDb does not currently list viewing providers for this film "
            "in this country. This does not prove the film is unavailable."
        )
        return

    if region.get("link"):
        st.markdown(
            f"[Check current availability on the provider page]"
            f"({region['link']})"
        )

    categories = [
        ("free", "Free"),
        ("ads", "Free with ads"),
        ("flatrate", "Subscription"),
        ("rent", "Rent"),
        ("buy", "Buy"),
    ]

    found = False

    for api_name, label in categories:
        providers = region.get(api_name, [])
        if not providers:
            continue

        found = True
        names = []

        for provider in providers:
            name = provider.get("provider_name")
            if name:
                names.append(name)

        if names:
            st.write(f"**{label}:** {', '.join(names)}")

    if not found:
        st.info(
            "A regional provider record exists, but TMDb did not return "
            "specific viewing categories."
        )

    st.caption(
        "Provider information is supplied by TMDb and may change. Check the "
        "provider directly; availability can depend on your location and account."
    )


def show_movie_details(movie, selected_country):
    movie_id = movie.get("id")
    if not movie_id:
        st.error("This search result does not have a TMDb movie ID.")
        return

    try:
        details = get_movie_details(movie_id, TMDB_API_KEY)
        credits = get_movie_credits(movie_id, TMDB_API_KEY)
        providers = get_movie_watch_providers(movie_id, TMDB_API_KEY)
        videos = get_movie_videos(movie_id, TMDB_API_KEY)
    except Exception as exc:
        st.error(f"Could not load TMDb details: {exc}")
        return

    title = details.get("title") or movie.get("title") or "Untitled"
    original_title = details.get("original_title", "")
    overview = details.get("overview") or "No synopsis supplied by TMDb."
    release_date = details.get("release_date") or "Not reported"
    runtime = details.get("runtime")
    runtime_text = f"{runtime} minutes" if runtime else "Not reported"
    countries = ", ".join(
        item.get("name", "")
        for item in details.get("production_countries", [])
        if item.get("name")
    ) or "Not reported"

    languages = ", ".join(
        item.get("english_name") or item.get("name", "")
        for item in details.get("spoken_languages", [])
        if item.get("english_name") or item.get("name")
    ) or "Not reported"

    genres = ", ".join(
        item.get("name", "")
        for item in details.get("genres", [])
        if item.get("name")
    ) or "Not reported"

    production_companies = ", ".join(
        company.get("name", "")
        for company in details.get("production_companies", [])
        if company.get("name")
    ) or "Not reported by TMDb"

    directors = names_from_credit(credits, {"Director"})
    producers = names_from_credit(
        credits,
        {"Producer", "Executive Producer", "Co-Producer"},
    )
    writers = names_from_credit(
        credits,
        {"Writer", "Screenplay", "Story"},
    )

    cast = [
        person.get("name")
        for person in credits.get("cast", [])[:15]
        if person.get("name")
    ]

    poster_path = details.get("poster_path")
    if poster_path:
        st.image(
            f"{TMDB_IMAGE_BASE}{poster_path}",
            width=220,
            caption="Poster image provided through TMDb",
        )

    st.markdown(f"## {title}")

    if original_title and original_title != title:
        st.caption(f"Original title: {original_title}")

    if details.get("homepage"):
        st.markdown(f"[Official film website]({details['homepage']})")

    tmdb_url = f"https://www.themoviedb.org/movie/{movie_id}"
    st.markdown(f"[Open TMDb film page]({tmdb_url})")

    st.write(overview)

    left, right = st.columns(2)

    with left:
        st.write(f"**Release date:** {release_date}")
        st.write(f"**Runtime:** {runtime_text}")
        st.write(f"**Genres:** {genres}")
        st.write(f"**Production countries:** {countries}")
        st.write(f"**Spoken languages:** {languages}")

    with right:
        st.write(f"**Budget:** {format_money(details.get('budget'))}")
        st.write(f"**Worldwide box office:** {format_money(details.get('revenue'))}")
        st.write(f"**Production companies/banner:** {production_companies}")
        st.write(
            f"**Director(s):** "
            f"{', '.join(directors) if directors else 'Not reported by TMDb'}"
        )
        st.write(
            f"**Producer(s):** "
            f"{', '.join(producers) if producers else 'Not reported by TMDb'}"
        )
        st.write(
            f"**Writer(s):** "
            f"{', '.join(writers) if writers else 'Not reported by TMDb'}"
        )

    st.write(
        f"**Cast:** {', '.join(cast) if cast else 'Not reported by TMDb'}"
    )

    # Awards are supplementary and not complete.
    with st.expander("Awards and recognition"):
        awards = search_wikidata_awards(movie_id)
        if awards:
            st.write("Wikidata lists these award/recognition items:")
            for award in awards:
                st.write(f"- {award}")
            st.markdown("[Check Wikidata](https://www.wikidata.org/)")
        else:
            st.write(
                "No awards were found in the supplementary Wikidata lookup. "
                "This does not mean the film received no awards."
            )
            st.caption(
                "TMDb does not provide complete awards data. Verify awards "
                "with official festival, academy, or award-organization sources."
            )

    video_results = videos.get("results", [])
    trailer = next(
        (
            item
            for item in video_results
            if item.get("site") == "YouTube"
            and item.get("type") in {"Trailer", "Teaser"}
        ),
        None,
    )

    if trailer and trailer.get("key"):
        st.markdown(
            f"[Watch a listed trailer on YouTube]"
            f"(https://www.youtube.com/watch?v={trailer['key']})"
        )

    display_watch_providers(
        providers,
        selected_country,
        next(
            (
                name
                for name, code in COUNTRIES.items()
                if code == selected_country
            ),
            selected_country,
        ),
    )

    # Film archive discovery: catalogue records, not guaranteed streaming.
    st.markdown("#### Archive and library records")
    st.caption(
        "These are supplementary catalogue searches. A record does not "
        "guarantee that the film is digitized or playable."
    )

    archive_results = search_internet_archive_films(title, 8)

    if archive_results:
        for item in archive_results:
            st.markdown(
                f"- [{item['title']}]({item['record_url']}) "
                f"({item.get('date') or 'date not listed'}) — "
                f"{item.get('source')}"
            )
    else:
        st.write("No matching Internet Archive film records were returned.")

    europeana_key = get_secret("EUROPEANA_API_KEY")
    if europeana_key:
        europeana_items = search_europeana_films(
            title,
            8,
            europeana_key,
        )

        if europeana_items:
            for item in europeana_items:
                if item.get("record_url"):
                    st.markdown(
                        f"- [{item['title']}]({item['record_url']}) — "
                        f"{item['source']}"
                    )
        else:
            st.write("No matching Europeana film records were returned.")
    else:
        st.caption(
            "To search Europeana, add EUROPEANA_API_KEY to this app's "
            "Streamlit Secrets."
        )


# ---------------------------------------------------------------------
# User interface
# ---------------------------------------------------------------------

st.title("🎬 Movie Research")
st.write(
    "Search documentary and general films, review available metadata, "
    "and check reported viewing options by country."
)

st.info(
    "No service contains every film worldwide. TMDb provides much of the "
    "film metadata and regional provider data. Missing figures are shown "
    "as not reported rather than guessed."
)

mode = st.radio(
    "Choose a search section",
    ["Documentary Films", "General Films"],
    horizontal=True,
)

with st.form("movie_search_form"):
    query = st.text_input(
        "Film title, person, event, topic, or idea",
        placeholder=(
            "For example: films about the Bengal famine, or a film title"
        ),
    )

    available_languages = get_tmdb_languages(TMDB_API_KEY)
    language_options = ["Any language"] + sorted(
        available_languages.values(),
        key=str.lower,
    )
    language_name = st.selectbox(
        "Language (optional)",
        language_options,
    )

    country_name = st.selectbox(
        "Country for viewing availability",
        list(COUNTRIES.keys()),
        index=list(COUNTRIES.keys()).index("India"),
    )

    submitted = st.form_submit_button(
        "Search films",
        type="primary",
    )


if submitted:
    if not query.strip():
        st.error("Enter a film title, person, event, topic, or idea.")
    else:
        try:
            with st.spinner("Searching TMDb..."):
                raw_results = search_tmdb_movies(query, TMDB_API_KEY)

            language_code = None
            if language_name != "Any language":
                language_code = next(
                    (
                        code
                        for code, name in available_languages.items()
                        if name == language_name
                    ),
                    None,
                )

            filtered = []

            for film in raw_results:
                if language_code and film.get("original_language") != language_code:
                    continue

                genre_ids = film.get("genre_ids", [])

                if mode == "Documentary Films" and 99 not in genre_ids:
                    continue

                filtered.append(film)

            st.session_state["movie_results"] = filtered
            st.session_state["movie_query"] = query
            st.session_state["movie_mode"] = mode
            st.session_state["movie_country"] = COUNTRIES[country_name]

            if mode == "Documentary Films" and not filtered:
                st.info(
                    "TMDb did not return results tagged as documentaries "
                    "for this search. Try broader wording or search under "
                    "General Films; topic-based tagging is not complete."
                )

        except Exception as exc:
            st.error(f"Film search failed: {exc}")


results = st.session_state.get("movie_results", [])
selected_country_code = st.session_state.get(
    "movie_country",
    COUNTRIES["India"],
)

if results:
    labels = []

    for film in results:
        title = film.get("title") or "Untitled"
        year = (film.get("release_date") or "")[:4]
        original_language = film.get("original_language") or ""
        label_parts = [title]

        if year:
            label_parts.append(year)
        if original_language:
            label_parts.append(original_language.upper())

        labels.append(" — ".join(label_parts))

    selected_index = st.selectbox(
        "Choose a film to view its details",
        range(len(results)),
        format_func=lambda index: labels[index],
    )

    selected_film = results[selected_index]
    show_movie_details(selected_film, selected_country_code)

elif submitted and query.strip():
    st.info(
        "No matching films were returned. Try fewer words, a different title, "
        "or another language."
    )


# ---------------------------------------------------------------------
# TMDb attribution
# ---------------------------------------------------------------------

st.divider()
st.caption(
    "This product uses the TMDB API but is not endorsed or certified by TMDB."
)
st.caption(
    "Viewing-provider availability is supplied by TMDb, may change, and "
    "should be verified on the provider's own website."
)
