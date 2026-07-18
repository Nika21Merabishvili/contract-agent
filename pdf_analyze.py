"""Analyse a service contract against Article 104 of the Georgian Tax Code.

Extracts text from a contract PDF and works through four small model calls
instead of one large one:

    1. parties   -- 12 fields, from the contract text
    2. terms     -- 12 fields, from the contract text
    3. tax       -- a forced clause-by-clause checklist over Article 104
    4. translate -- the five genuinely free-text fields, English -> Georgian

The model emits English codes from closed sets; `georgian.py` maps them to
Georgian. That keeps Georgian generation -- which a 4B model does badly -- out of
22 of the 27 output fields, and out of every citation.

Prints one JSON object with `contract_data` and `tax_analysis` to stdout. A
downstream script turns that into an Excel workbook; this program never formats
output itself. Saving to a file is currently disabled; uncomment the marked lines
at the end of main() to re-enable it (then --out chooses the destination).

Usage:
    python pdf_analyze.py                          # prompts for a contract PDF
    python pdf_analyze.py contract.pdf
    python pdf_analyze.py contract.pdf --article-lang ka   # reason over the Georgian statute
    python pdf_analyze.py --dump-article104        # verify Article 104 loads cleanly
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ollama import chat
from pypdf import PdfReader

import georgian as ka
from georgian import NOT_SPECIFIED

MODEL = "qwen3.5:4b"

# The model can address 262144 tokens, but a context that large allocates a huge
# KV cache -- and `ollama ps` shows this machine running the model ~97% on CPU,
# where that cache is pure latency. Size num_ctx to the actual content instead.
CTX_MAX = 32768
CTX_MIN = 4096

RESPONSE_HEADROOM = 2048

# Sampling. Three of these fight defaults that would otherwise corrupt extraction:
#   temperature      -- qwen3.5:4b ships with 1.0 (`ollama show`); noise on a
#                       task whose answers are copied out of a document.
#   presence_penalty -- ships with 1.5. Penalties are applied to logits *before*
#                       sampling, so this shifts the argmax even at temperature 0.
#                       On a task that copies names and addresses verbatim,
#                       penalising recently-seen tokens is entirely harmful.
#   top_k / top_p    -- irrelevant at temperature 0, pinned so a model default
#                       change cannot quietly reintroduce sampling.
SAMPLING = {
    "temperature": 0,
    "presence_penalty": 0,
    "frequency_penalty": 0,
    "repeat_penalty": 1.0,
    "top_k": 1,
    "top_p": 1.0,
    "seed": 0,
}

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"

ARTICLE_FILES = {"ka": "article_104.txt", "en": "article_104_en.txt"}

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

CURRENCIES = ["USD", "EUR", "GEL", "GBP", "CHF", "TRY", "RUB", "AED", "CNY", "JPY", "not_stated"]

SENTINEL = "not_stated"


# --------------------------------------------------------------------------- #
# Schemas
#
# Ollama compiles `format` into a GBNF grammar, and its converter does NOT
# support every JSON Schema keyword. Probed against Ollama 0.32.0 / qwen3.5:4b:
#
#   enum                        enforced
#   minLength                   enforced
#   additionalProperties:false  enforced
#   type:integer                enforced
#   pattern                     HTTP 400, "failed to parse grammar"
#   anyOf                       HTTP 400, "failed to parse grammar"
#
# So there is no `pattern` on dates: a date regex would not be ignored, it would
# hard-fail every run. Dates are requested as ISO 8601 (a format the model is
# heavily trained on, and unambiguous -- unlike the 2026/02/10 vs 10/02/2026
# confusion a DD/MM/YYYY instruction invites) and are validated and reformatted
# in Python, which also gives a retry hook.
#
# minLength is set to 1, never higher. A high floor does not buy reasoning: when
# the model has only a stub answer it pads to length with garbage. Substantive
# length floors are enforced in Python, where failing means retrying rather than
# rambling.
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Prompts
#
# Every field that cannot be read off the page verbatim says INFER explicitly.
# In the benchmarked failure the only inference-requiring field the model got
# right was residency -- the one field whose guide authorised inference. Fields
# without that authorisation came back blank.
# --------------------------------------------------------------------------- #

PARTIES_PROMPT = """Extract the two parties from the service contract below.

Party A is the first party named in the contract; Party B is the second.

For EACH party return these fields:

- "name": the party's full legal name, copied EXACTLY as written in the contract.
  Never translate or transliterate it.

- "tax_id": the party's TAX identification number, copied exactly as written.
  The label varies by country -- accept any of: INN, TIN, tax ID, EIN, VAT number,
  USt-IdNr, SIREN, SIRET, HRB, Companies House number, company number,
  registration number, identification number. It is usually near the party's
  address, often in parentheses.
  An SEC CIK number is a filer key, NOT a tax identification number -- never put a
  CIK in this field.
  If the party has no tax identification number anywhere in the contract, return
  exactly "not_stated".

- "legal_form": the party's form of incorporation, as one of the allowed codes.
  INFER it from the suffix of the party's name (Inc. -> corporation, LLC -> llc,
  Ltd -> ltd, GmbH -> gmbh, AG/JSC -> jsc, LP -> limited_partnership) or from any
  statement of incorporation. Only use "not_stated" if nothing in the contract
  indicates a form. Never combine two codes.

- "address": the party's registered or legal address, copied EXACTLY as written.

- "role": "provider" for the party that renders the service, "client" for the
  party that orders and pays for it. INFER from the obligations each party takes on.

- "residency": "resident" if the party is a tax resident of GEORGIA (the country),
  "non_resident" otherwise. If residency is not stated outright, INFER it from the
  party's address or place of incorporation.

- "country": ISO 3166 alpha-2 code of the party's own country (US, GE, DE, GB...).
  INFER it from the party's address if it is not stated. Use "unknown" only if the
  contract gives no location for the party at all.

Answer only from the contract text. Do not use outside knowledge about these companies.

--- BEGIN CONTRACT ---
{contract}
--- END CONTRACT ---"""

TERMS_PROMPT = """Extract the commercial terms from the service contract below.

- "service_type": the category of service, as one of the allowed codes:
    it         -- software development, ML/AI engineering, support, hosting, data
    consulting -- business consulting, financial or strategic advice
    marketing  -- advertising, SMM, branding
    legal      -- legal representation, document preparation
    logistics  -- cargo transportation, freight, warehousing
    other      -- none of the above
- "service_type_other_en": if service_type is "other", name the actual service in
  English in 2-4 words. Otherwise return exactly "n/a".

- "service_description_en": describe the specific scope of work of THIS contract,
  IN ENGLISH, in one or two sentences. Be concrete about what is delivered.

- "contract_value_stated": "yes" if a total contract price can be determined,
  otherwise "no".
- "contract_value": the TOTAL contract price as digits only, no symbols or
  separators. Rules, in order:
    1. If the contract states a total, total face value, or aggregate fee, use it.
    2. If it only lists instalments, phases, or milestones, ADD THEM ALL UP and
       use the sum. INFER the total this way -- do not return a single instalment.
    3. A cap, ceiling, or "not to exceed" figure for a SUBSET of the work (such as
       expenses) is NOT the contract value. Ignore it.
    4. If both a stated total and a sum of phases exist and they agree, use it.
       If they disagree, use the stated total.
  If no total can be determined, set contract_value_stated to "no" and
  contract_value to 0. Never invent a figure.

- "currency": ISO 4217 code of the contract currency.

- "payment_frequency": how often payment falls due, as one of the allowed codes.
  Payment tied to phases, milestones, or deliverables is "milestone". INFER from
  the payment schedule.

- "payment_terms_en": when and under what conditions payment is due, IN ENGLISH.
  Include the invoice period if stated (e.g. "within 30 days of invoice").

- "signing_date", "effective_date", "end_date": each as ISO 8601 "YYYY-MM-DD", or
  exactly "not_stated" if absent.
    signing_date   -- when the parties signed.
    effective_date -- when the contract enters into force. INFER from any
                      commencement clause; it may differ from the signing date.
    end_date       -- when the contract expires or terminates. INFER it from the
                      final phase or milestone date if no expiry is stated outright.

- "duration_unit" / "duration_value": how long the contract runs.
  Use "indefinite" (with duration_value 0) for an open-ended contract, or a unit
  with a whole number, e.g. days/months/years. If the contract gives an effective
  date and an end date, leave this as "not_stated" with 0 -- the duration is
  computed from the dates instead. Only fill it when the contract states a term
  in words, such as "for a period of twelve (12) months".

- "place_of_service_en": where the service is actually performed or delivered,
  IN ENGLISH. INFER this -- it is rarely labelled. Use, in order of weight:
  any clause restricting WHERE work may be performed or where data or personnel
  may be located; the location of the infrastructure or staff doing the work; the
  provider's principal place of business. State the territory plainly and cite the
  restriction if there is one. Only return "not stated" if the contract is
  completely silent on location.

Answer only from the contract text.

--- BEGIN CONTRACT ---
{contract}
--- END CONTRACT ---"""

TAX_PROMPT = """You are applying Article 104 of the Georgian Tax Code (income from a
source in Georgia) to one contract. Article 104 is reproduced in full below. Work
STRICTLY from that text. Do not use outside knowledge of the Tax Code.

THE FACTS (already extracted from the contract, treat as given):
{facts}

STEP 1 -- CHECKLIST. Article 104 defines a closed list of income types. Work
through EVERY clause below in order. For each one, answer "applies": "yes" or "no"
for THIS contract, and give a one-line "reason" in English.

Do not skip a clause because it looks irrelevant -- answering "no" with a reason IS
the required answer for an irrelevant clause. Do not stop when you find a clause
that fits.

{checklist}

Take special care with these three, which decide most cross-border service contracts:
  g_a -- is the service PHYSICALLY performed on Georgian territory?
  g_z -- are the parties in different states AND is the PROVIDER a Georgian
         resident? (If the provider is not a Georgian resident, this is "no".)
  g_t -- are the parties in different states AND does the provider render the
         service IN Georgia through a permanent establishment, an employee, or
         costs incurred in Georgia?
A "permanent establishment" is a specific legal status. A party's principal place
of business or head office is NOT by itself a permanent establishment. Do not
assert one unless the contract states it.

STEP 2 -- CONCLUDE.
- "is_georgian_source_income": "yes" only if at least one clause in your checklist
  is "yes". If every clause is "no", this must be "no".
- "cited_clauses": the 1 to 3 clauses your conclusion actually RESTS ON. Not every
  clause you examined -- a citation that names the whole article cites nothing.
  For a "yes": the clauses you marked "yes".
  For a "no": only the clauses that came CLOSEST to applying -- the ones a reader
  would have to see ruled out before accepting your conclusion. Leave out clauses
  that were never plausible for this contract.
- "is_georgian_source_income_justification_en": justify the conclusion IN ENGLISH,
  naming the clauses you rely on and why they do or do not apply. Two or three
  sentences. Do not pad.
- "withholding_or_reverse_vat_obligation": "yes", "no", or "partial" -- does a
  withholding obligation at the source of payment, or a reverse-charge VAT
  obligation, arise for either party? You must reach a verdict.
- "withholding_or_reverse_vat_explanation_en": explain IN ENGLISH, referencing the
  provision you rely on. Two or three sentences.

--- BEGIN ARTICLE 104 ---
{article}
--- END ARTICLE 104 ---"""

TRANSLATE_PROMPT = """Translate each value below from English into Georgian.

Rules:
- Return natural, professional legal Georgian. This is for a tax document.
- Translate the meaning, not word by word.
- Keep every clause identifier (ა, ბ, გ.ა, გ.ზ, გ.თ ...) exactly as it appears.
- Keep proper nouns -- company names, place names, city names -- in their original
  script. Do not transliterate them.
- Keep all numbers, dates, and currency codes exactly as they appear.
- Return one Georgian string per key. Do not add commentary.

GLOSSARY -- use these renderings exactly. They are the terms of art in this
document, and a wrong one changes the legal meaning:
{glossary}

Georgia is the country საქართველო. It is NOT Germany (გერმანია). This text is about
Georgian tax law: if you write გერმანია anywhere, the document is wrong.

{payload}"""

# Terms the model demonstrably gets wrong, pinned. On the benchmark contract it
# rendered "Georgia" as "გერმანია" (Germany) in all four places -- a tax analysis
# that reads "income does not arise in Germany" is not a translation error, it is a
# wrong legal conclusion -- and "virtual private clouds" as "ვირტუალურ პირად
# ქალაქებში" (virtual private *cities*).
GLOSSARY = {
    "Georgia (the country)": "საქართველო",
    "Georgian (adjective)": "ქართული / საქართველოს",
    "Germany": "გერმანია",
    "the United States": "აშშ (შეერთებული შტატები)",
    "permanent establishment": "მუდმივი დაწესებულება",
    "resident / non-resident": "რეზიდენტი / არარეზიდენტი",
    "service provider": "მომსახურების გამწევი",
    "service recipient": "მომსახურების მიმღები",
    "source of income": "შემოსავლის წყარო",
    "withholding tax at source": "წყაროსთან დაკავებული გადასახადი",
    "reverse-charge VAT": "უკუდაბეგვრის წესით დღგ",
    "virtual private cloud (VPC)": "ვირტუალური კერძო ღრუბელი",
    "payment": "გადახდა",
    "invoice": "ინვოისი",
    "milestone": "ეტაპი",
    "clause / sub-clause": "ქვეპუნქტი",
}

# (english term, Georgian rendering that would be a mistranslation of it). Checked
# only when the English never mentions the thing that word would legitimately mean.
CONFUSIONS = [("Georgia", "გერმანია", "Germany")]


@dataclass
class Page:
    number: int
    text: str


class Cancelled(Exception):
    """Raised when the user interrupts generation with Ctrl+C."""


class ModelError(Exception):
    """The model's answer was unusable after retries."""


class AnalysisFailure(ModelError):
    """The model failed to reach a tax verdict.

    Distinct from a contract being silent on a fact. A missing fact is data; a
    missing verdict is a failure, and must never be papered over with a filler
    value that reads like an extracted fact downstream.
    """


def estimate_tokens(text: str) -> int:
    """Token estimate calibrated against this model's tokenizer.

    Measured on qwen3.5:4b via prompt_eval_count: article_104.txt is 5,882 chars
    -> 2,953 tokens (~2.0 chars/token for Georgian), English ~4 chars/token. A flat
    chars//3 rule under-counts Georgian by ~1.5x, so the buckets are split.

    SAFETY_MARGIN exists because the split still under-counts: the spaces, digits
    and punctuation inside Georgian text fall outside the Georgian block and are
    charged at the cheaper Latin rate, which measured 5% low on the real statute.
    This number sizes num_ctx, so it must err high -- guessing low silently
    truncates the input, which is the failure mode this whole file exists to avoid.
    """
    SAFETY_MARGIN = 1.15
    georgian = sum(1 for ch in text if "Ⴀ" <= ch <= "ჿ")
    raw = georgian // 2 + (len(text) - georgian) // 3
    return int(raw * SAFETY_MARGIN) + 1


def parse_page_range(spec: str, total: int) -> set[int]:
    wanted: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            wanted.update(range(int(start), int(end) + 1))
        else:
            wanted.add(int(part))
    bad = sorted(p for p in wanted if p < 1 or p > total)
    if bad:
        shown = f"{bad[0]}-{bad[-1]}" if len(bad) > 1 else str(bad[0])
        raise SystemExit(
            f"error: requested page(s) {shown} but the document has {total} page(s)"
        )
    return wanted


def clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def dedupe(page):
    """Undo fake-bold character doubling.

    Some PDF renderers (including the markdown-to-PDF pipeline that produced the
    benchmark contract) fake bold by drawing each glyph twice at a sub-point
    offset. Extractors faithfully return both copies, so bold text arrives as
    `NNeexxuuss AAnnaallyyttiiccss` -- and it is the *values* that are bold:
    party names, the EIN, the CIK, the effective date, and the stated total
    `$150,000.00`. A model cannot read a number that reads `$$115500,,000000..0000`.

    tolerance=1 is load-bearing. At 2 it also collapses legitimately repeated
    characters -- "SOC 2 Type II" becomes "SOC 2 Type I".
    """
    try:
        return page.dedupe_chars(tolerance=1)
    except Exception:
        return page


def render_table(rows: list[list[str | None]]) -> str:
    """Render an extracted table as markdown.

    pypdf flattens a table into reading order, which interleaves headers and cells
    into columns that no longer line up -- that is how the fee table in the
    benchmark contract lost its totals. A markdown grid keeps each cell attached
    to its column.
    """
    grid = [[(cell or "").replace("\n", " ").strip() for cell in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    header, *body = grid
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return "\n".join(lines)


def extract(path: Path, page_spec: str | None = None) -> list[Page]:
    """Extract contract text, with any tables re-rendered as markdown.

    Uses pdfplumber when available for its table detection, falling back to pypdf
    for text only. The fallback still works -- it just leaves tables mangled, which
    is what put the contract value out of reach in the benchmark run.
    """
    try:
        import pdfplumber
    except ImportError:
        print(
            "  note: pdfplumber not installed -- tables will reach the model as\n"
            "        interleaved columns. `pip install pdfplumber` to fix.",
            file=sys.stderr,
        )
        return extract_pypdf(path, page_spec)

    pages: list[Page] = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        wanted = parse_page_range(page_spec, total) if page_spec else None
        for index, page in enumerate(pdf.pages, start=1):
            if wanted is not None and index not in wanted:
                continue
            page = dedupe(page)
            body = clean(page.extract_text() or "")
            tables = [render_table(t) for t in (page.extract_tables() or [])]
            tables = [t for t in tables if t]
            if tables:
                body += "\n\nTables on this page, re-rendered:\n\n" + "\n\n".join(tables)
            if body:
                pages.append(Page(number=index, text=body))

    if not pages:
        raise SystemExit(no_text_message(path))
    return pages


def extract_pypdf(path: Path, page_spec: str | None = None) -> list[Page]:
    reader = PdfReader(path)
    if reader.is_encrypted:
        # An empty password opens PDFs that are encrypted but not password-locked.
        if reader.decrypt("") == 0:
            raise SystemExit(f"error: {path.name} is password-protected")

    total = len(reader.pages)
    wanted = parse_page_range(page_spec, total) if page_spec else None

    pages: list[Page] = []
    for index, page in enumerate(reader.pages, start=1):
        if wanted is not None and index not in wanted:
            continue
        body = clean(page.extract_text() or "")
        if body:
            pages.append(Page(number=index, text=body))

    if not pages:
        raise SystemExit(no_text_message(path))
    return pages


def no_text_message(path: Path) -> str:
    return (
        f"error: no extractable text in {path.name}.\n"
        "The pages are probably scanned images. Neither pdfplumber nor pypdf OCRs --\n"
        "they only read embedded text. Options: run OCRmyPDF over the file first, or\n"
        "feed the page images to qwen3.5:4b directly (it has vision capability)."
    )


def strip_comments(text: str) -> str:
    """Drop '#' banner lines. article_104_en.txt carries a review-status banner
    that is for humans and must never reach the model."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def load_text_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "\n\n".join(p.text for p in extract_pypdf(path))
    if suffix in {".txt", ".md"}:
        return clean(strip_comments(path.read_text(encoding="utf-8-sig")))
    raise SystemExit(f"error: unsupported file type: {path.name} (use .pdf, .txt, or .md)")


def find_article104(explicit: Path | None, lang: str) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise SystemExit(f"error: no such file: {explicit}")
        return explicit

    stem = Path(ARTICLE_FILES[lang]).stem
    for suffix in (".txt", ".md", ".pdf"):
        candidate = KNOWLEDGE_DIR / f"{stem}{suffix}"
        if candidate.is_file():
            return candidate

    raise SystemExit(
        f"error: Article 104 ({lang}) not found.\n"
        f"Expected {KNOWLEDGE_DIR / ARTICLE_FILES[lang]}\n"
        "or pass an explicit path with --article104."
    )


def warn_if_unreviewed(path: Path) -> None:
    head = path.read_text(encoding="utf-8-sig")[:600]
    if "UNREVIEWED" in head:
        print(
            "\n  WARNING: "
            f"{path.name} is an UNREVIEWED machine translation of a tax statute.\n"
            "  Every citation derived from it is unverified until a Georgian-speaking\n"
            "  tax lawyer checks it against article_104.txt and updates the banner.\n"
            "  Use --article-lang ka to reason over the authoritative Georgian text.\n",
            file=sys.stderr,
        )


def show_prompt(label: str, prompt: str) -> None:
    rule = "=" * 72
    print(
        f"\n{rule}\nPROMPT [{label}] (~{estimate_tokens(prompt)} tokens)\n"
        f"{rule}\n{prompt}\n{rule}\n",
        file=sys.stderr,
    )


def ask(prompt: str, schema: dict, *, label: str, think: bool, show_input: bool) -> str:
    """One Ollama call, with num_ctx sized to the prompt.

    Without an explicit num_ctx Ollama defaults to a small window and silently
    discards everything above it. num_ctx is logged alongside the server's own
    prompt_eval_count so truncation is visible rather than inferred.
    """
    needed = estimate_tokens(prompt) + RESPONSE_HEADROOM
    num_ctx = max(CTX_MIN, min(CTX_MAX, needed))

    if needed > CTX_MAX:
        raise SystemExit(
            f"error: the {label} prompt needs ~{needed} tokens, above the {CTX_MAX} "
            f"limit.\nUse --pages to send fewer contract pages, or raise CTX_MAX if "
            "your hardware allows it."
        )

    if show_input:
        show_prompt(label, prompt)

    stream = chat(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        think=think,
        format=schema,
        options={"num_ctx": num_ctx, **SAMPLING},
        stream=True,
    )

    parts: list[str] = []
    final = None
    try:
        for part in stream:
            piece = part.message.content or ""
            if piece:
                parts.append(piece)
                print(piece, end="", flush=True, file=sys.stderr)
            final = part
    except KeyboardInterrupt:
        # Closing the generator drops the HTTP connection, which tells Ollama to
        # abandon the request rather than keep burning CPU on an unwanted answer.
        stream.close()
        print(file=sys.stderr)
        raise Cancelled from None
    finally:
        print(flush=True, file=sys.stderr)

    used = getattr(final, "prompt_eval_count", None) if final else None
    if used:
        headroom = num_ctx - used
        flag = "  <-- NO HEADROOM, INPUT MAY BE TRUNCATED" if headroom < 256 else ""
        print(
            f"  [{label}] prompt_eval_count={used}  num_ctx={num_ctx}  "
            f"headroom={headroom}{flag}",
            file=sys.stderr,
        )

    return "".join(parts).strip()


def ask_json(
    prompt: str,
    schema: dict,
    validate,
    *,
    label: str,
    think: bool,
    show_input: bool,
    attempts: int = 3,
) -> dict:
    """Call the model, parse, validate, and retry with the complaint fed back.

    This is where every constraint Ollama's grammar cannot express is enforced --
    date formats above all, since `pattern` makes the server reject the request
    outright.
    """
    complaint = None
    for attempt in range(1, attempts + 1):
        text = prompt if complaint is None else (
            f"{prompt}\n\n--- YOUR PREVIOUS ANSWER WAS REJECTED ---\n{complaint}\n"
            "Return the whole object again, corrected."
        )
        raw = ask(text, schema, label=f"{label} #{attempt}", think=think, show_input=show_input)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object, got {type(data).__name__}")
            validate(data)
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            complaint = str(exc)
            print(f"  [{label}] attempt {attempt} rejected: {complaint}", file=sys.stderr)

    raise ModelError(f"{label}: no valid answer after {attempts} attempts. Last: {complaint}")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

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

    print("\n[1/4] parties", file=sys.stderr)
    parties = call_parties(contract, **kw)

    print("\n[2/4] terms", file=sys.stderr)
    terms = call_terms(contract, **kw)

    print("\n[3/4] tax analysis", file=sys.stderr)
    tax = call_tax(parties, terms, article, **kw)

    print("\n[4/4] translation", file=sys.stderr)
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


def pick_file_dialog() -> Path | None:
    """Open the OS file picker. Returns None if tkinter is unavailable or nothing chosen."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        return None

    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)  # otherwise the dialog opens behind the terminal
        chosen = filedialog.askopenfilename(
            title="Select a contract PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        root.destroy()
    except Exception:
        return None

    return Path(chosen) if chosen else None


def clean_path_input(raw: str) -> str:
    """Drag-and-drop and 'Copy as path' wrap the path in quotes; strip them."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1]
    return raw.strip()


def prompt_for_pdf() -> Path:
    """Ask for a PDF: file picker by default, typed or pasted path as fallback."""
    while True:
        try:
            answer = input(
                "\nSelect a contract PDF.\n"
                "  [Enter] open a file picker, or paste/drag a path here (q to quit): "
            )
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            raise SystemExit(130)
        answer = clean_path_input(answer)

        if answer.lower() in {"q", "quit", "exit"}:
            raise SystemExit(0)

        if not answer:
            path = pick_file_dialog()
            if path is None:
                print("  no file picker available here -- type a path instead", file=sys.stderr)
                continue
        else:
            path = Path(answer).expanduser()

        if not path.is_file():
            print(f"  no such file: {path}", file=sys.stderr)
            continue
        if path.suffix.lower() != ".pdf":
            print(f"  not a .pdf: {path.name}", file=sys.stderr)
            continue
        return path


def main() -> None:
    # The Windows console defaults to cp1252, which raises on any non-Latin-1
    # character -- and most values in the output are Georgian.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Analyse a contract against Article 104 of the Georgian Tax Code; output JSON."
    )
    parser.add_argument("pdf", type=Path, nargs="?", help="contract PDF; prompts if omitted")
    parser.add_argument(
        "--article104",
        type=Path,
        help=f"Article 104 text (.txt/.md/.pdf); default: search {KNOWLEDGE_DIR}",
    )
    parser.add_argument(
        "--article-lang",
        choices=("en", "ka"),
        default="en",
        help="statute language the model reasons over (default: en)",
    )
    parser.add_argument(
        "--out", type=Path, help="where to write the JSON (default: next to the contract PDF)"
    )
    parser.add_argument("--pages", help="limit contract extraction, e.g. '1-10' or '2,5,9-12'")
    parser.add_argument("--think", action="store_true", help="enable the model's reasoning mode")
    parser.add_argument(
        "--dump-text", action="store_true", help="print extracted contract text and exit"
    )
    parser.add_argument(
        "--dump-article104",
        action="store_true",
        help="print the Article 104 text as the model will see it, then exit",
    )
    parser.add_argument(
        "--show-input", action="store_true", help="print each prompt before it is sent"
    )
    args = parser.parse_args()

    article_path = find_article104(args.article104, args.article_lang)

    if args.dump_article104:
        text = load_text_document(article_path)
        print(f"=== {article_path} ===", file=sys.stderr)
        print(text)
        if args.article_lang == "ka":
            print(
                "\nCheck the text above: it must be readable Georgian, not boxes or "
                f"Latin garbage.\nIf it is garbled, re-save Article 104 as a UTF-8 .txt "
                f"file in {KNOWLEDGE_DIR}.",
                file=sys.stderr,
            )
        return

    if args.pdf is None:
        args.pdf = prompt_for_pdf()
    elif not args.pdf.is_file():
        raise SystemExit(f"error: no such file: {args.pdf}")

    pages = extract(args.pdf, args.pages)
    words = sum(len(p.text.split()) for p in pages)
    print(f"Extracted {len(pages)} page(s), ~{words} words from {args.pdf.name}", file=sys.stderr)

    if args.dump_text:
        for page in pages:
            print(f"\n=== page {page.number} ===\n{page.text}")
        return

    article_text = load_text_document(article_path)
    warn_if_unreviewed(article_path)
    print(f"Article 104 loaded from {article_path.name}", file=sys.stderr)
    print("Analysing contract... (Ctrl+C to stop)", file=sys.stderr)

    try:
        result = analyse_contract(
            pages, article_text, think=args.think, show_input=args.show_input
        )
    except Cancelled:
        raise SystemExit(130)
    except AnalysisFailure as exc:
        raise SystemExit(
            f"\nerror: the model did not reach a tax verdict.\n  {exc}\n\n"
            "Not filling this in: a verdict the model failed to reach must not reach\n"
            "the Excel looking like an extracted fact. Re-run, or try --think."
        )
    except ModelError as exc:
        raise SystemExit(f"\nerror: {exc}")

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)

    # Uncomment to also save the JSON to a file (next to the PDF, or at --out):
    # out_path = args.out or args.pdf.with_suffix(".json")
    # out_path.write_text(rendered + "\n", encoding="utf-8")
    # print(f"\nSaved: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
