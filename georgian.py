"""Georgian vocabulary for contract analysis output.

The local model never writes Georgian. It emits English codes from a closed set
(enforced by the `enum` in each call's schema) and this module maps them to
Georgian. Every string here was written by a human and is spelled correctly by
construction -- the model cannot introduce a typo into any field routed through
these tables.

Only genuinely open-ended prose (see FREE_TEXT_FIELDS in pdf_analyze.py) is
translated by the model, in a separate dedicated call.
"""

from __future__ import annotations

NOT_SPECIFIED = "არ არის მითითებული"

ROLE = {
    "provider": "შემსრულებელი",
    "client": "შემკვეთი",
}

RESIDENCY = {
    "resident": "რეზიდენტი",
    "non_resident": "არარეზიდენტი",
}

LEGAL_FORM = {
    "llc": "შეზღუდული პასუხისმგებლობის საზოგადოება",
    "ltd": "შეზღუდული პასუხისმგებლობის საზოგადოება",
    "gmbh": "შეზღუდული პასუხისმგებლობის საზოგადოება",
    "jsc": "სააქციო საზოგადოება",
    "corporation": "კორპორაცია",
    "sole_trader": "ინდივიდუალური მეწარმე",
    "partnership": "სოლიდარული პასუხისმგებლობის საზოგადოება",
    "limited_partnership": "კომანდიტური საზოგადოება",
    "cooperative": "კოოპერატივი",
    "branch": "ფილიალი",
    "nonprofit": "არასამეწარმეო (არაკომერციული) იურიდიული პირი",
    "other": "სხვა სამართლებრივი ფორმა",
    "not_stated": NOT_SPECIFIED,
}

SERVICE_TYPE = {
    "it": "IT მომსახურება",
    "consulting": "საკონსულტაციო მომსახურება",
    "marketing": "მარკეტინგული მომსახურება",
    "legal": "იურიდიული მომსახურება",
    "logistics": "ლოგისტიკური მომსახურება",
    # `other` is resolved from the model's free-text description, not from here.
    "other": None,
}

PAYMENT_FREQUENCY = {
    "one_time": "ერთჯერადი",
    "monthly": "ყოველთვიური",
    "quarterly": "კვარტალური",
    "annual": "წლიური",
    "milestone": "ეტაპობრივი",
    "on_demand": "მოთხოვნისამებრ",
    "not_stated": NOT_SPECIFIED,
}

YES_NO = {
    "yes": "დიახ",
    "no": "არა",
    "partial": "ნაწილობრივ",
}

DURATION_UNIT = {
    "days": "დღე",
    "months": "თვე",
    "years": "წელი",
}

INDEFINITE = "უვადო"

COUNTRY = {
    "GE": "საქართველო",
    "US": "აშშ",
    "GB": "გაერთიანებული სამეფო",
    "DE": "გერმანია",
    "FR": "საფრანგეთი",
    "IT": "იტალია",
    "ES": "ესპანეთი",
    "PT": "პორტუგალია",
    "NL": "ნიდერლანდები",
    "BE": "ბელგია",
    "AT": "ავსტრია",
    "CH": "შვეიცარია",
    "IE": "ირლანდია",
    "SE": "შვედეთი",
    "NO": "ნორვეგია",
    "DK": "დანია",
    "FI": "ფინეთი",
    "PL": "პოლონეთი",
    "CZ": "ჩეხეთი",
    "SK": "სლოვაკეთი",
    "HU": "უნგრეთი",
    "RO": "რუმინეთი",
    "BG": "ბულგარეთი",
    "GR": "საბერძნეთი",
    "CY": "კიპროსი",
    "EE": "ესტონეთი",
    "LV": "ლატვია",
    "LT": "ლიტვა",
    "TR": "თურქეთი",
    "UA": "უკრაინა",
    "AM": "სომხეთი",
    "AZ": "აზერბაიჯანი",
    "RU": "რუსეთი",
    "BY": "ბელარუსი",
    "MD": "მოლდოვა",
    "KZ": "ყაზახეთი",
    "IL": "ისრაელი",
    "AE": "არაბთა გაერთიანებული საამიროები",
    "CN": "ჩინეთი",
    "JP": "იაპონია",
    "IN": "ინდოეთი",
    "SG": "სინგაპური",
    "HK": "ჰონგკონგი",
    "CA": "კანადა",
    "AU": "ავსტრალია",
    "NZ": "ახალი ზელანდია",
    "BR": "ბრაზილია",
    "MX": "მექსიკა",
    "ZA": "სამხრეთ აფრიკა",
    "KR": "სამხრეთ კორეა",
}


def country_name(code: str) -> str:
    """Georgian country name; falls back to the ISO code itself if unmapped.

    Falling back to the code is deliberate: an unmapped country should surface as
    a visible `(BW)` for a human to fix, not be silently guessed at by the model.
    """
    return COUNTRY.get((code or "").strip().upper(), (code or "").strip().upper())


def residency_phrase(residency_code: str, country_code: str) -> str:
    """e.g. ("non_resident", "US") -> "არარეზიდენტი (აშშ)"."""
    base = RESIDENCY.get(residency_code)
    if base is None:
        return NOT_SPECIFIED
    country = country_name(country_code)
    return f"{base} ({country})" if country else base


def duration_phrase(value: int | None, unit: str) -> str:
    """e.g. (4, "months") -> "4 თვე"; (None, "indefinite") -> "უვადო"."""
    if unit == "indefinite":
        return INDEFINITE
    noun = DURATION_UNIT.get(unit)
    if noun is None or value is None:
        return NOT_SPECIFIED
    return f"{value} {noun}"


# Article 104 clause identifiers. The model addresses clauses by these ASCII keys
# so it never has to reproduce Georgian script; Python renders the citation.
CLAUSE_LABEL = {
    "a": "ა",
    "b": "ბ",
    "g_a": "გ.ა",
    "g_b": "გ.ბ",
    "g_g": "გ.გ",
    "g_d": "გ.დ",
    "g_e": "გ.ე",
    "g_v": "გ.ვ",
    "g_z": "გ.ზ",
    "g_t": "გ.თ",
    "d": "დ",
    "e": "ე",
    "o": "ო",
    "zh": "ჟ",
    "r": "რ",
}

# One-line English gloss per clause, used to build the forced checklist prompt.
CLAUSE_GLOSS = {
    "a": "employment in Georgia",
    "b": "supply of goods in the territory of Georgia",
    "g_a": "service actually rendered in the territory of Georgia",
    "g_b": "service directly connected with immovable property in Georgia",
    "g_g": "service directly connected with movable property in Georgia",
    "g_d": "service connected with securities issued by a Georgian resident",
    "g_e": "service rendered in Georgia in culture/art/education/tourism/recreation/sport",
    "g_v": "carriage of cargo or passengers starting AND ending in Georgia",
    "g_z": "provider and recipient in different states AND the provider is a Georgian resident",
    "g_t": "provider and recipient in different states AND the provider renders the service in "
           "Georgia via a permanent establishment, an employee, or costs incurred in Georgia",
    "d": "economic activity of a non-resident's permanent establishment in Georgia",
    "e": "write-off of bad debts / sale of fixed assets / compensation, tied to activity in Georgia",
    "o": "management, financial or insurance services paid by a resident enterprise or a "
         "non-resident's permanent establishment in Georgia",
    "zh": "international transport between Georgia and abroad, or international telecommunications",
    "r": "other income from activity in Georgia",
}

CLAUSE_KEYS = list(CLAUSE_LABEL)


def citation(clause_keys: list[str]) -> str:
    """Render a Georgian citation sentence from clause keys.

    ["g_z", "g_t"] -> 'საფუძველი: 104-ე მუხლის პირველი ნაწილის „გ.ზ“ და „გ.თ“ ქვეპუნქტები.'

    Python owns this string so the citation is always well-formed Georgian and
    always matches the clauses the model actually selected.
    """
    labels = [CLAUSE_LABEL[k] for k in clause_keys if k in CLAUSE_LABEL]
    if not labels:
        return ""
    quoted = [f"„{label}“" for label in labels]
    if len(quoted) == 1:
        joined, noun = quoted[0], "ქვეპუნქტი"
    else:
        joined = f"{', '.join(quoted[:-1])} და {quoted[-1]}"
        noun = "ქვეპუნქტები"
    return f"საფუძველი: 104-ე მუხლის პირველი ნაწილის {joined} {noun}."
