"""Constraints Ollama's grammar cannot express, enforced in Python.

Each `validate_*` is handed to `ask_json` and raises ValueError on a bad answer;
the message becomes the complaint fed back to the model on retry. Date formats
matter most here -- a regex in the schema would make Ollama reject the request
outright (see schemas.py), so ISO 8601 is checked and reformatted here instead.
"""

from __future__ import annotations

import re
from datetime import date

import georgian as ka
from prompts import CONFUSIONS
from schemas import SENTINEL

ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

# The model's ways of saying "nothing here". minLength:1 forbids "" but the model
# just reaches for the next-cheapest non-answer, so they are all treated alike.
NON_ANSWERS = {"", "''", '""', "-", "--", "n/a", "na", "none", "null", "not stated",
               "not_stated", "unknown", "unspecified", "not specified", "not applicable"}


def is_non_answer(value: str) -> bool:
    return value.strip().strip("\"'").lower() in NON_ANSWERS


def parse_date(value: str) -> date | None:
    m = ISO_DATE.match(value.strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def validate_dates(data: dict) -> None:
    for field in ("signing_date", "effective_date", "end_date"):
        value = str(data.get(field, "")).strip()
        if value == SENTINEL:
            continue
        if parse_date(value) is None:
            raise ValueError(
                f'{field} must be ISO 8601 "YYYY-MM-DD" or exactly "not_stated"; '
                f"got {value!r}"
            )


def validate_terms(data: dict) -> None:
    validate_dates(data)
    if data.get("contract_value_stated") == "yes" and int(data.get("contract_value", 0)) <= 0:
        raise ValueError(
            "contract_value_stated is 'yes' but contract_value is not a positive "
            "number. Either give the total (summing the phases if necessary) or set "
            "contract_value_stated to 'no'."
        )
    for field in ("service_description_en", "place_of_service_en", "payment_terms_en"):
        if len(str(data.get(field, "")).strip()) < 15:
            raise ValueError(f"{field} is too short to be a real answer; expand it.")


def validate_tax(data: dict) -> None:
    checklist = data.get("checklist") or {}
    missing = [k for k in ka.CLAUSE_KEYS if k not in checklist]
    if missing:
        raise ValueError(f"checklist is missing clauses: {', '.join(missing)}")

    for key, entry in checklist.items():
        if len(str((entry or {}).get("reason", "")).strip()) < 5:
            raise ValueError(f"checklist.{key}.reason is empty; give a one-line reason.")

    verdict = data.get("is_georgian_source_income")
    any_yes = any((entry or {}).get("applies") == "yes" for entry in checklist.values())
    if verdict == "no" and any_yes:
        hits = [k for k, v in checklist.items() if (v or {}).get("applies") == "yes"]
        raise ValueError(
            f"is_georgian_source_income is 'no' but you marked {', '.join(hits)} as "
            "applying. Reconcile the checklist with the conclusion."
        )
    if verdict == "yes" and not any_yes:
        raise ValueError(
            "is_georgian_source_income is 'yes' but every clause is marked 'no'. "
            "Reconcile the checklist with the conclusion."
        )

    cited = data.get("cited_clauses") or []
    if not cited:
        raise ValueError("cited_clauses is empty; name the clauses the conclusion rests on.")
    unknown = [c for c in cited if c not in ka.CLAUSE_LABEL]
    if unknown:
        raise ValueError(f"cited_clauses contains unknown clauses: {', '.join(unknown)}")
    # Asked to cite what "would have had to apply and did not" on an all-no
    # checklist, the model cited all fifteen clauses -- which is the whole article,
    # and therefore no citation at all.
    if len(cited) > 3:
        raise ValueError(
            f"cited_clauses lists {len(cited)} clauses. Cite at most 3 -- only the ones "
            "your conclusion rests on, not every clause you examined."
        )
    if verdict == "yes":
        wrong = [c for c in cited if (checklist.get(c) or {}).get("applies") != "yes"]
        if wrong:
            raise ValueError(
                f"is_georgian_source_income is 'yes' but cited_clauses names "
                f"{', '.join(wrong)}, which you marked 'no'. Cite the clauses that apply."
            )

    for field in (
        "is_georgian_source_income_justification_en",
        "withholding_or_reverse_vat_explanation_en",
    ):
        value = str(data.get(field, "")).strip()
        if is_non_answer(value) or len(value) < 60:
            raise ValueError(f"{field} is not a real justification; explain your reasoning.")


def validate_translation(keys: list[str], english: dict):
    def check(data: dict) -> None:
        for key in keys:
            value = str(data.get(key, "")).strip()
            if is_non_answer(value):
                raise ValueError(f"{key} was not translated.")
            georgian = sum(1 for ch in value if "Ⴀ" <= ch <= "ჿ")
            if georgian < max(3, len(value) // 8):
                raise ValueError(
                    f"{key} is not in Georgian script -- translate it, do not copy the "
                    "English through."
                )
            if value.strip() == english[key].strip():
                raise ValueError(f"{key} is unchanged English; translate it.")

            # A mistranslated country is a wrong legal conclusion, not a typo, and
            # nothing else in the pipeline would catch it: the string is fluent
            # Georgian of a plausible length about a plausible country.
            for term, wrong, means in CONFUSIONS:
                source = english[key]
                if term.lower() in source.lower() and wrong in value \
                        and means.lower() not in source.lower():
                    raise ValueError(
                        f"{key}: the English says {term!r} but your Georgian says "
                        f"{wrong!r}, which means {means}. {term} is საქართველო. "
                        "Retranslate it."
                    )

    return check
