"""JSON Schemas for the four calls, plus the field lists that shape the output.

Ollama compiles `format` into a GBNF grammar, and its converter does NOT support
every JSON Schema keyword. Probed against Ollama 0.32.0 / qwen3.5:4b:

  enum                        enforced
  minLength                   enforced
  additionalProperties:false  enforced
  type:integer                enforced
  pattern                     HTTP 400, "failed to parse grammar"
  anyOf                       HTTP 400, "failed to parse grammar"

So there is no `pattern` on dates: a date regex would not be ignored, it would
hard-fail every run. Dates are requested as ISO 8601 (a format the model is
heavily trained on, and unambiguous -- unlike the 2026/02/10 vs 10/02/2026
confusion a DD/MM/YYYY instruction invites) and are validated and reformatted
in Python (see validation.py), which also gives a retry hook.

minLength is set to 1, never higher. A high floor does not buy reasoning: when
the model has only a stub answer it pads to length with garbage. Substantive
length floors are enforced in Python, where failing means retrying rather than
rambling.
"""

from __future__ import annotations

import georgian as ka

CURRENCIES = ["USD", "EUR", "GEL", "GBP", "CHF", "TRY", "RUB", "AED", "CNY", "JPY", "not_stated"]

SENTINEL = "not_stated"

CONTRACT_FIELDS = [
    "party_a_name",
    "party_a_inn",
    "party_a_legal_form",
    "party_a_address",
    "party_a_role",
    "party_a_residency",
    "party_b_name",
    "party_b_inn",
    "party_b_legal_form",
    "party_b_address",
    "party_b_role",
    "party_b_residency",
    "service_type",
    "service_description",
    "contract_value",
    "payment_frequency",
    "currency",
    "payment_terms",
    "signing_date",
    "effective_date",
    "contract_duration",
    "end_date",
    "place_of_service",
]

TAX_FIELDS = [
    "is_georgian_source_income",
    "is_georgian_source_income_justification",
    "withholding_or_reverse_vat_obligation",
    "withholding_or_reverse_vat_explanation",
]

# The only fields whose Georgian the model writes. Everything else is either
# copied verbatim, a number, or mapped from a code by georgian.py.
FREE_TEXT_FIELDS = [
    "service_description",
    "payment_terms",
    "place_of_service",
    "is_georgian_source_income_justification",
    "withholding_or_reverse_vat_explanation",
]


PARTY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "tax_id": {"type": "string", "minLength": 1},
        "legal_form": {"type": "string", "enum": sorted(ka.LEGAL_FORM)},
        "address": {"type": "string", "minLength": 1},
        "role": {"type": "string", "enum": ["provider", "client"]},
        "residency": {"type": "string", "enum": ["resident", "non_resident"]},
        "country": {"type": "string", "enum": sorted(ka.COUNTRY) + ["unknown"]},
    },
    "required": ["name", "tax_id", "legal_form", "address", "role", "residency", "country"],
    "additionalProperties": False,
}

PARTIES_SCHEMA = {
    "type": "object",
    "properties": {"party_a": PARTY_SCHEMA, "party_b": PARTY_SCHEMA},
    "required": ["party_a", "party_b"],
    "additionalProperties": False,
}

TERMS_SCHEMA = {
    "type": "object",
    "properties": {
        "service_type": {"type": "string", "enum": sorted(ka.SERVICE_TYPE)},
        "service_type_other_en": {"type": "string", "minLength": 1},
        "service_description_en": {"type": "string", "minLength": 1},
        # Split so an unknown total never has to be invented: `integer` cannot
        # hold "not_stated", and forcing a number out of silence is a hallucination.
        "contract_value_stated": {"type": "string", "enum": ["yes", "no"]},
        "contract_value": {"type": "integer"},
        "currency": {"type": "string", "enum": CURRENCIES},
        "payment_frequency": {"type": "string", "enum": sorted(ka.PAYMENT_FREQUENCY)},
        "payment_terms_en": {"type": "string", "minLength": 1},
        "signing_date": {"type": "string", "minLength": 1},
        "effective_date": {"type": "string", "minLength": 1},
        "end_date": {"type": "string", "minLength": 1},
        "duration_unit": {
            "type": "string",
            "enum": ["days", "months", "years", "indefinite", "not_stated"],
        },
        "duration_value": {"type": "integer"},
        "place_of_service_en": {"type": "string", "minLength": 1},
    },
    "required": [
        "service_type", "service_type_other_en", "service_description_en",
        "contract_value_stated", "contract_value", "currency", "payment_frequency",
        "payment_terms_en", "signing_date", "effective_date", "end_date",
        "duration_unit", "duration_value", "place_of_service_en",
    ],
    "additionalProperties": False,
}

# Forced coverage: `required` over one key per clause means the model cannot skip
# a clause, and cannot answer only on whichever clause looked familiar. An array
# would not do this -- `minItems` is not reliably compiled, and nothing would tie
# an entry to a specific clause.
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "applies": {"type": "string", "enum": ["yes", "no"]},
        "reason": {"type": "string", "minLength": 1},
    },
    "required": ["applies", "reason"],
    "additionalProperties": False,
}

TAX_SCHEMA = {
    "type": "object",
    "properties": {
        "checklist": {
            "type": "object",
            "properties": {k: CHECK_SCHEMA for k in ka.CLAUSE_KEYS},
            "required": ka.CLAUSE_KEYS,
            "additionalProperties": False,
        },
        "is_georgian_source_income": {"type": "string", "enum": ["yes", "no"]},
        "cited_clauses": {
            "type": "array",
            "items": {"type": "string", "enum": ka.CLAUSE_KEYS},
        },
        "is_georgian_source_income_justification_en": {"type": "string", "minLength": 1},
        "withholding_or_reverse_vat_obligation": {
            "type": "string",
            "enum": ["yes", "no", "partial"],
        },
        "withholding_or_reverse_vat_explanation_en": {"type": "string", "minLength": 1},
    },
    "required": [
        "checklist", "is_georgian_source_income", "cited_clauses",
        "is_georgian_source_income_justification_en",
        "withholding_or_reverse_vat_obligation",
        "withholding_or_reverse_vat_explanation_en",
    ],
    "additionalProperties": False,
}


def translation_schema(keys: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": "string", "minLength": 1} for k in keys},
        "required": keys,
        "additionalProperties": False,
    }
