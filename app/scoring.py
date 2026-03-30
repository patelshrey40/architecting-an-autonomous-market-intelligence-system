from __future__ import annotations


CLASSIFICATION_MAP = {
    "1": "VACANT",
    "2": "RESIDENTIAL",
    "3A": "FARM",
    "3B": "FARM",
    "4A": "COMMERCIAL",
    "4B": "INDUSTRIAL",
    "4C": "MULTIFAMILY",
    "5A": "RAILROAD",
    "5B": "RAILROAD",
    "15A": "EXEMPT_PUBLIC",
    "15B": "EXEMPT_PUBLIC",
    "15C": "EXEMPT_PUBLIC",
    "15D": "EXEMPT_RELIGIOUS_CHARITABLE",
    "15E": "EXEMPT_CEMETERY",
    "15F": "EXEMPT_OTHER",
}

CLASS_RELEVANCE_SCORES = {
    "COMMERCIAL": 100.0,
    "MULTIFAMILY": 100.0,
    "INDUSTRIAL": 65.0,
    "EXEMPT_PUBLIC": 55.0,
    "EXEMPT_RELIGIOUS_CHARITABLE": 60.0,
    "EXEMPT_OTHER": 25.0,
    "RAILROAD": 15.0,
    "VACANT": 10.0,
    "FARM": 5.0,
    "EXEMPT_CEMETERY": 5.0,
    "RESIDENTIAL": 0.0,
    "UNKNOWN": 0.0,
}

CLASSIFICATION_COLORS = {
    "COMMERCIAL": "#d94841",
    "MULTIFAMILY": "#f08c00",
    "INDUSTRIAL": "#1d4e89",
    "EXEMPT_PUBLIC": "#5b3f8c",
    "EXEMPT_RELIGIOUS_CHARITABLE": "#2b8a3e",
    "EXEMPT_OTHER": "#8f5f15",
    "VACANT": "#adb5bd",
    "RESIDENTIAL": "#7aa874",
    "UNKNOWN": "#868e96",
}


def classify_property_code(property_class_code):
    code = (property_class_code or "").strip().upper()
    return CLASSIFICATION_MAP.get(code, "UNKNOWN")


def class_relevance_score(classification):
    return CLASS_RELEVANCE_SCORES.get(classification, 0.0)


def place_density_signal(place_count):
    if place_count <= 0:
        return 0.0
    return min(float(place_count), 10.0) / 10.0 * 100.0


def compute_priority_score(
    assessed_value_percentile,
    classification,
    building_sqft_percentile,
    place_count,
):
    return round(
        (assessed_value_percentile * 0.55)
        + (class_relevance_score(classification) * 0.30)
        + (building_sqft_percentile * 0.10)
        + (place_density_signal(place_count) * 0.05),
        2,
    )


def priority_tier_for_score(score):
    if score >= 80.0:
        return "Tier 1"
    if score >= 60.0:
        return "Tier 2"
    if score >= 40.0:
        return "Tier 3"
    return "Tier 4"
