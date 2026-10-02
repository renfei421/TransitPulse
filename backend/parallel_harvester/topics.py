"""Query generation, topic matching and region inference."""

import re



TOPIC_KEYWORDS = {
    "public_transport": [
        "public transport",
        "free public transport",
        "free pt",
        "ptv",
        "myki",
        "tram",
        "trams",
        "train",
        "trains",
        "bus",
        "buses",
        "metro",
        "vline",
        "v/line",
        "station",
        "commute",
        "commuter",
        "fare",
        "fares",
        "replacement bus",
        "transport victoria"
    ],
    "fuel_price": [
        "fuel price",
        "fuel prices",
        "petrol price",
        "petrol prices",
        "diesel price",
        "diesel prices",
        "petrol",
        "diesel",
        "unleaded",
        "servo",
        "bowser",
        "gas price",
        "gas prices",
        "fuel cost",
        "fuel costs",
        "oil price",
        "oil prices",
        "fuel excise",
        "petrol station"
    ],
    "ev": [
        "ev",
        "evs",
        "electric vehicle",
        "electric vehicles",
        "electric car",
        "electric cars",
        "tesla",
        "byd",
        "zeekr",
        "polestar",
        "hybrid",
        "battery",
        "charging",
        "charger",
        "supercharger",
        "plug-in",
        "phev"
    ],
    "oil_vehicle": [
        "petrol car",
        "petrol cars",
        "diesel car",
        "diesel cars",
        "ice vehicle",
        "ice vehicles",
        "internal combustion",
        "combustion engine",
        "driving",
        "drive",
        "driver",
        "drivers",
        "vehicle fuel cost",
        "car fuel cost"
    ],
}


REGION_KEYWORDS = {
    "Victoria_or_Melbourne": [
        "melbourne",
        "victoria",
        "vic",
        "ptv",
        "myki",
        "v/line",
        "vline"
    ],
    "New_South_Wales_or_Sydney": [
        "sydney",
        "nsw",
        "new south wales",
        "opal"
    ],
    "Queensland_or_Brisbane": [
        "brisbane",
        "queensland",
        "qld",
        "go card"
    ],
    "South_Australia_or_Adelaide": [
        "adelaide",
        "south australia",
        "sa metro"
    ],
    "Western_Australia_or_Perth": [
        "perth",
        "western australia",
        "transperth"
    ],
    "ACT_or_Canberra": [
        "canberra",
        "act"
    ],
    "Tasmania_or_Hobart": [
        "hobart",
        "tasmania",
        "tas"
    ],
    "Northern_Territory_or_Darwin": [
        "darwin",
        "northern territory",
        "nt"
    ],
    "Australia": [
        "australia",
        "australian",
        "aussie"
    ],
}


SEARCH_QUERIES = [
    # Fuel price and oil shock
    "fuel price Australia",
    "fuel prices Australia",
    "petrol price Australia",
    "petrol prices Australia",
    "diesel price Australia",
    "diesel prices Australia",
    "fuel cost Australia",
    "oil price Australia",
    "oil prices Australia",
    "fuel excise Australia",
    "petrol station Australia",
    "Iran fuel price Australia",
    "Iran petrol Australia",
    "Iran diesel Australia",
    "Hormuz fuel price Australia",
    "Hormuz oil price Australia",
    "Middle East oil price Australia",

    # Public transport
    "public transport Australia",
    "public transport Victoria",
    "public transport Melbourne",
    "free public transport Victoria",
    "free public transport Melbourne",
    "free PT Victoria",
    "PTV Melbourne",
    "myki Melbourne",
    "tram Melbourne",
    "train Melbourne",
    "bus Melbourne",
    "buses Melbourne",
    "replacement bus Melbourne",
    "Metro Melbourne",
    "VLine Victoria",
    "transport Victoria",

    # EV
    "EV Australia",
    "EV Victoria",
    "EV Melbourne",
    "electric vehicle Australia",
    "electric vehicles Australia",
    "electric car Australia",
    "electric car Melbourne",
    "Tesla Australia",
    "Tesla Melbourne",
    "BYD Australia",
    "BYD Melbourne",
    "hybrid Australia",
    "EV charging Australia",

    # Oil vehicle and driving cost
    "petrol car Australia",
    "diesel car Australia",
    "driving fuel price Australia",
    "driving petrol price Australia",
    "car fuel cost Australia",
    "vehicle fuel cost Australia",

    # Cross-topic comparison
    "public transport fuel price Australia",
    "public transport fuel prices Australia",
    "EV fuel price Australia",
    "electric vehicle fuel prices Australia",
    "petrol car fuel price Australia",
    "diesel car fuel price Australia"
]


def clean_spaces(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def dedupe_keep_order(items):
    seen = set()
    result = []

    for item in items:
        item = clean_spaces(item)

        if not item:
            continue

        key = item.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def sanitise_search_query(query):
    """
    Keep search queries stable.
    Short exact phrase tokens such as "EV", "BYD", "VIC", "QLD" may trigger
    Bluesky 400 Bad Request errors, so this function removes quotes around
    short acronym-like tokens.
    """
    query = clean_spaces(query)
    query = re.sub(r'"([A-Za-z0-9]{1,4})"', r"\1", query)
    query = clean_spaces(query)

    return query


GLOBAL_ALIASES = {
    "public_transport": ["public transit", "mass transit", "transit fare", "transit fares", "bus fare", "rail fare"],
    "fuel_price": ["gasoline price", "gasoline prices", "gasoline cost", "gasoline costs", "pump prices"],
    "ev": ["ev charging", "charging cost", "charging costs"],
    "oil_vehicle": ["gasoline car", "gasoline cars"],
}
QUERY_PROFILES = ("legacy_au", "global_en", "regional_supplement")
LOCAL_QUERY_PREFIXES = ("free pt ", "ptv ", "myki ", "vline ", "transport victoria")


def keyword_bank(profile="legacy_au"):
    if profile not in QUERY_PROFILES:
        raise ValueError(f"Unknown query profile: {profile}")
    return {topic: words + (GLOBAL_ALIASES.get(topic, []) if profile == "global_en" else [])
            for topic, words in TOPIC_KEYWORDS.items()}


def generate_queries(profile="legacy_au"):
    """Keep the original study reproducible; global queries retain topic intent.

    These are search inputs, not a claim that every API supports full-text search.
    Local products remain in an optional regional supplement.
    """
    keyword_bank(profile)  # Reject typos rather than silently changing scope.
    queries = SEARCH_QUERIES
    if profile == "global_en":
        queries = [re.sub(r" (Australia|Victoria|Melbourne)$", "", query)
                   for query in SEARCH_QUERIES if not query.lower().startswith(LOCAL_QUERY_PREFIXES)]
        queries = ["metro transit" if q == "Metro" else q for q in queries]
        queries += [word for words in GLOBAL_ALIASES.values() for word in words]
        queries += ["gas price", "gas prices"]
    elif profile == "regional_supplement":
        queries = [query for query in SEARCH_QUERIES if query.lower().startswith(LOCAL_QUERY_PREFIXES)]
    queries = [sanitise_search_query(query) for query in queries]
    queries = dedupe_keep_order(queries)
    return queries


def match_topics(text, hashtags=(), profile="legacy_au"):
    """Reuse the keyword bank for text and explicit API hashtag metadata.

    Compact aliases apply only to hashtags, never arbitrary plain-text substrings.
    A candidate match is a recall-oriented signal, not verified relevance.
    """
    text_lower = clean_spaces(text).lower()
    tags = {re.sub(r"[^\w]", "", tag.lower()) for tag in hashtags if isinstance(tag, str)}
    matched_keywords = []
    candidate_topics = []

    for topic, keywords in keyword_bank(profile).items():
        topic_hits = []
        for keyword in keywords:
            if (re.search(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?!\w)", text_lower)
                    or re.sub(r"[^\w]", "", keyword.lower()) in tags):
                topic_hits.append(keyword)
        if topic_hits:
            candidate_topics.append(topic)
            matched_keywords.extend(topic_hits)

    return sorted(set(candidate_topics)), sorted(set(matched_keywords))


AMBIGUOUS_REGION_TERMS = frozenset({"act", "nt", "tas", "vic", "victoria", "darwin", "perth", "opal"})


def region_evidence(text):
    """Describe geographic text hints; never infer the author's residence.

    Ambiguous place names and short words remain visible but cannot establish an
    Australian context on their own. The categories are not calibrated scores.
    """
    text_lower = clean_spaces(text).lower()
    explicit, ambiguous = [], []
    for region, keywords in REGION_KEYWORDS.items():
        for keyword in keywords:
            if re.search(r"\b" + re.escape(keyword.lower()) + r"\b", text_lower):
                item = {"region": region, "keyword": keyword}
                (ambiguous if keyword in AMBIGUOUS_REGION_TERMS else explicit).append(item)
    return {"kind": "explicit_text_hint" if explicit else "ambiguous_text_hint" if ambiguous else "unknown",
            "explicit": explicit, "ambiguous": ambiguous}


def infer_region(text):
    evidence = region_evidence(text)
    if evidence["explicit"]:
        region = evidence["explicit"][0]["region"]
        # Preserve the legacy output contract. These numbers are heuristic, not probabilities.
        return {"inferred_region": region, "region_confidence": 0.7 if region != "Australia" else 0.5,
                "region_source": "text_keyword"}
    return {"inferred_region": None, "region_confidence": 0.0, "region_source": None}


def public_hashtag_seeds(profile="legacy_au"):
    """Public-timeline adapter for a small subset of the existing keyword bank.

    Removing spaces yields a hashtag, not an equivalent full-text search query.
    SEARCH_QUERIES remains unchanged for authenticated search/backfill.
    """
    bank = keyword_bank(profile)
    if profile == "regional_supplement":
        preferred = ("myki", "ptv", "vline")
    elif profile == "global_en":
        preferred = ("public transit", "mass transit", "gas prices", "fuel prices", "oil prices",
                     "ev charging", "electric cars", "driving")
    else:
        preferred = ("public transport", "myki", "tram", "petrol", "fuel prices", "electric vehicles", "ev", "driving")
    return [{"topic": topic, "keyword": keyword, "hashtag": re.sub(r"[^\w]", "", keyword)}
            for keyword in preferred for topic, words in bank.items() if keyword in words]


COUNTRY_TERMS = {
    "AU": ("australia", "australian"), "US": ("united states", "usa"),
    "GB": ("united kingdom", "britain", "uk"), "CA": ("canada", "canadian"),
    "NZ": ("new zealand",), "IN": ("india", "indian"), "DE": ("germany", "german"),
    "FR": ("france", "french"), "CN": ("china", "chinese"), "JP": ("japan", "japanese"),
    "SG": ("singapore",), "MY": ("malaysia", "malaysian"), "IR": ("iran", "iranian"),
}


def country_text_hints(text):
    """Non-exhaustive mentions, not post origin, residence or verified geography.

    Do not match ambiguous two-letter tokens such as 'us' and 'in'. Multiple
    mentions may denote a comparison or a news story rather than local context.
    """
    lower = clean_spaces(text).lower()
    return sorted(country for country, words in COUNTRY_TERMS.items()
                  if any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", lower) for word in words))


def analysis_language_route(lang):
    """Provider language metadata is a routing hint, never a verified label."""
    if not lang or lang in {"und", "unknown"}:
        return "needs_language_review"
    return "english_candidate" if lang.lower().split("-")[0] == "en" else "other_language"
