"""Prompt templates for the four calls, kept apart from the code that sends them.

Every field that cannot be read off the page verbatim says INFER explicitly.
In the benchmarked failure the only inference-requiring field the model got
right was residency -- the one field whose guide authorised inference. Fields
without that authorisation came back blank.
"""

from __future__ import annotations

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
