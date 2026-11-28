def regional_links(category, query):
    encoded = quote(query or "", safe="")

    st.markdown("#### Find this publication")

    # Curated direct links for publications whose official websites are known.
    known_publications = {
        "anandabazar patrika": (
            "Anandabazar Patrika — official website",
            "https://www.anandabazar.com/",
        ),
    }

    normalized_query = (query or "").strip().lower()
    match = known_publications.get(normalized_query)

    if match:
        name, url = match
        st.markdown(f"**{name}:** [Open website]({url})")

    # These links search the web or collection catalogues for the exact title.
    if category == "Newspapers":
        searches = [
            (
                "Search for the newspaper’s official website",
                f"https://www.google.com/search?q={quote('"' + query + '" official newspaper website', safe='')}",
            ),
            (
                "Search for newspaper archives",
                f"https://www.google.com/search?q={quote('"' + query + '" newspaper archive historical issues', safe='')}",
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
                "Search for the publication’s official website",
                f"https://www.google.com/search?q={quote('"' + query + '" magazine official website', safe='')}",
            ),
            (
                "Search for digitized issues",
                f"https://www.google.com/search?q={quote('"' + query + '" magazine archive digitized issues', safe='')}",
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
                f"https://www.google.com/search?q={quote('site:modjourn.org "' + query + '"', safe='')}",
            ),
        ]

    else:
        searches = [
            (
                "Search Google for this title",
                f"https://www.google.com/search?q={encoded}",
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
        "These links open external searches. Search results may include "
        "unofficial or incomplete records; confirm a publication’s identity "
        "and access terms on the relevant site."
    )
