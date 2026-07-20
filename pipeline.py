"""The four-call sequence and the assembly of one JSON object.

    1. parties   -- 12 fields, from the contract text
    2. terms     -- 12 fields, from the contract text
    3. tax       -- a forced clause-by-clause checklist over Article 104
    4. translate -- the five genuinely free-text fields, English -> Georgian

Python then writes every Georgian value that is not free prose, mapping the
model's English codes through georgian.py so 22 of the 27 fields -- and every
citation -- cannot carry a spelling error.
"""

from __future__ import annotations

import json

import georgian as ka
from georgian import NOT_SPECIFIED

import diagnostics as diag
from errors import AnalysisFailure, ModelError
from extraction import Page
from ollama_client import ask_json
from prompts import (
    GLOSSARY,
    PARTIES_PROMPT,
    TAX_PROMPT,
    TERMS_PROMPT,
    TRANSLATE_PROMPT,
)
from schemas import (
    CONTRACT_FIELDS,
    PARTIES_SCHEMA,
    SENTINEL,
    TAX_FIELDS,
    TAX_SCHEMA,
    TERMS_SCHEMA,
    translation_schema,
)
from validation import (
    is_non_answer,
    parse_date,
    validate_tax,
    validate_terms,
    validate_translation,
)


# --------------------------------------------------------------------------- #
# The four calls
# --------------------------------------------------------------------------- #


def call_parties(contract: str, **kw) -> dict:
    return ask_json(
        PARTIES_PROMPT.format(contract=contract),
        PARTIES_SCHEMA,
        lambda d: None,
        label="parties",
        **kw,
    )


def call_terms(contract: str, **kw) -> dict:
    return ask_json(
        TERMS_PROMPT.format(contract=contract),
        TERMS_SCHEMA,
        validate_terms,
        label="terms",
        **kw,
    )


def build_facts(parties: dict, terms: dict) -> str:
    """The tax call sees extracted facts, not the contract.

    Re-reading the contract is what let the failed run reach for the
    cargo-transport clause: the words were in front of it. The facts it needs are
    residency, roles, and where the work happens.
    """
    lines = []
    for tag in ("a", "b"):
        p = parties[f"party_{tag}"]
        lines.append(
            f"- Party {tag.upper()}: {p['name']} | role={p['role']} | "
            f"residency={p['residency']} | country={p['country']}"
        )
    lines.append(f"- Service type: {terms['service_type']}")
    lines.append(f"- Scope: {terms['service_description_en']}")
    lines.append(f"- Place of performance: {terms['place_of_service_en']}")
    value = terms["contract_value"] if terms["contract_value_stated"] == "yes" else "not stated"
    lines.append(f"- Contract value: {value} {terms['currency']}")
    lines.append(f"- Payment: {terms['payment_frequency']} -- {terms['payment_terms_en']}")
    return "\n".join(lines)


def build_checklist() -> str:
    return "\n".join(f'  {key:5} -- {ka.CLAUSE_GLOSS[key]}' for key in ka.CLAUSE_KEYS)


def call_tax(parties: dict, terms: dict, article: str, **kw) -> dict:
    prompt = TAX_PROMPT.format(
        facts=build_facts(parties, terms),
        checklist=build_checklist(),
        article=article,
    )
    try:
        return ask_json(prompt, TAX_SCHEMA, validate_tax, label="tax", **kw)
    except ModelError as exc:
        # A missing verdict is a failure, not a missing fact. Never fill it in.
        raise AnalysisFailure(str(exc)) from None


def call_translate(english: dict, **kw) -> dict:
    keys = list(english)
    payload = json.dumps(english, ensure_ascii=False, indent=2)
    glossary = "\n".join(f"  {en}  ->  {ge}" for en, ge in GLOSSARY.items())
    return ask_json(
        TRANSLATE_PROMPT.format(glossary=glossary, payload=payload),
        translation_schema(keys),
        validate_translation(keys, english),
        label="translate",
        **kw,
    )


# --------------------------------------------------------------------------- #
# Assembly -- Python writes every Georgian value that is not free prose
# --------------------------------------------------------------------------- #


def fmt_date(value: str) -> str:
    parsed = parse_date(value)
    return parsed.strftime("%d/%m/%Y") if parsed else NOT_SPECIFIED


def derive_duration(terms: dict) -> str:
    """Prefer arithmetic over the model.

    The plan's field split leaves contract_duration homeless -- it is Georgian, but
    it is neither a fixed vocabulary nor free prose. When both dates are known
    Python computes the term outright; the model is only consulted when the
    contract states a term in words and gives no dates.
    """
    start, end = parse_date(terms["effective_date"]), parse_date(terms["end_date"])
    if start and end and end >= start:
        days = (end - start).days
        # Nearest month, not completed months: a 10 Feb -> 05 May term is 2 months
        # and 24 days, and calling that "2 თვე" understates it as badly as the
        # model's "4 თვე" overstated it. Under a month, days are the honest unit.
        months = round(days / 30.44)
        if months >= 12 and months % 12 == 0:
            return ka.duration_phrase(months // 12, "years")
        if months >= 1:
            return ka.duration_phrase(months, "months")
        return ka.duration_phrase(days, "days")

    unit = terms["duration_unit"]
    if unit == "indefinite":
        return ka.INDEFINITE
    if unit == SENTINEL:
        return NOT_SPECIFIED
    return ka.duration_phrase(terms["duration_value"] or None, unit)


def assemble(parties: dict, terms: dict, tax: dict, translated: dict) -> dict:
    contract_data: dict[str, str] = {}

    for tag in ("a", "b"):
        p = parties[f"party_{tag}"]
        tax_id = str(p["tax_id"]).strip()
        contract_data[f"party_{tag}_name"] = p["name"].strip()
        contract_data[f"party_{tag}_inn"] = (
            NOT_SPECIFIED if is_non_answer(tax_id) else tax_id
        )
        contract_data[f"party_{tag}_legal_form"] = ka.LEGAL_FORM[p["legal_form"]]
        contract_data[f"party_{tag}_address"] = p["address"].strip()
        contract_data[f"party_{tag}_role"] = ka.ROLE[p["role"]]
        contract_data[f"party_{tag}_residency"] = ka.residency_phrase(
            p["residency"], p["country"]
        )

    service_type = ka.SERVICE_TYPE[terms["service_type"]]
    if service_type is None:  # "other" -- resolved from the translated description
        service_type = translated.get("service_type_other", NOT_SPECIFIED)
    contract_data["service_type"] = service_type
    contract_data["service_description"] = translated["service_description"]
    contract_data["contract_value"] = (
        str(terms["contract_value"]) if terms["contract_value_stated"] == "yes"
        else NOT_SPECIFIED
    )
    contract_data["payment_frequency"] = ka.PAYMENT_FREQUENCY[terms["payment_frequency"]]
    contract_data["currency"] = (
        NOT_SPECIFIED if terms["currency"] == SENTINEL else terms["currency"]
    )
    contract_data["payment_terms"] = translated["payment_terms"]
    contract_data["signing_date"] = fmt_date(terms["signing_date"])
    contract_data["effective_date"] = fmt_date(terms["effective_date"])
    contract_data["contract_duration"] = derive_duration(terms)
    contract_data["end_date"] = fmt_date(terms["end_date"])
    contract_data["place_of_service"] = translated["place_of_service"]

    cite = ka.citation(tax["cited_clauses"])
    justification = translated["is_georgian_source_income_justification"]
    tax_analysis = {
        "is_georgian_source_income": ka.YES_NO[tax["is_georgian_source_income"]],
        "is_georgian_source_income_justification": f"{justification} {cite}".strip(),
        "withholding_or_reverse_vat_obligation": ka.YES_NO[
            tax["withholding_or_reverse_vat_obligation"]
        ],
        "withholding_or_reverse_vat_explanation": translated[
            "withholding_or_reverse_vat_explanation"
        ],
    }

    missing = [f for f in CONTRACT_FIELDS if f not in contract_data]
    if missing:
        raise ModelError(f"internal: assembled object is missing {', '.join(missing)}")

    return {
        "contract_data": {k: contract_data[k] for k in CONTRACT_FIELDS},
        "tax_analysis": {k: tax_analysis[k] for k in TAX_FIELDS},
        "_audit": {
            "checklist": tax["checklist"],
            "cited_clauses": [ka.CLAUSE_LABEL[c] for c in tax["cited_clauses"]],
        },
    }


def analyse_contract(pages: list[Page], article: str, *, think: bool, show_input: bool) -> dict:
    contract = "\n\n".join(f"[page {p.number}]\n{p.text}" for p in pages)
    kw = {"think": think, "show_input": show_input}

    diag.progress("\n[1/4] parties")
    parties = call_parties(contract, **kw)

    diag.progress("\n[2/4] terms")
    terms = call_terms(contract, **kw)

    diag.progress("\n[3/4] tax analysis")
    tax = call_tax(parties, terms, article, **kw)

    diag.progress("\n[4/4] translation")
    english = {
        "service_description": terms["service_description_en"],
        "payment_terms": terms["payment_terms_en"],
        "place_of_service": terms["place_of_service_en"],
        "is_georgian_source_income_justification": tax[
            "is_georgian_source_income_justification_en"
        ],
        "withholding_or_reverse_vat_explanation": tax[
            "withholding_or_reverse_vat_explanation_en"
        ],
    }
    if terms["service_type"] == "other":
        english["service_type_other"] = terms["service_type_other_en"]
    translated = call_translate(english, **kw)

    return assemble(parties, terms, tax, translated)
