# Claude.ai benchmark prompt

Runs the same analysis as `pdf_analyze.py`, but on claude.ai instead of the local Qwen model,
so the two outputs can be compared on identical inputs.

**How to use:**

1. Open a new chat on claude.ai.
2. Attach two files: the contract PDF, and `knowledge/article_104.txt`.
3. Copy everything below the marker line and paste it as your message.
4. Send, then compare the JSON you get back with the JSON from `python pdf_analyze.py contract.pdf`.

**Note:** the script forces valid JSON through Ollama's schema parameter, which makes a malformed
response impossible. claude.ai has no equivalent — the prompt only *asks* for the JSON shape. So
schema conformance is not a fair comparison axis here; the content of the values is.

--- COPY EVERYTHING BELOW THIS LINE ---

You are a leading legal and tax analyst AI assistant specializing in Georgian tax law.

You are given two attached documents:
1. A service agreement/contract (usually in English) — the PDF.
2. Article 104 of the Tax Code of Georgia, in Georgian — the file named `article_104.txt`.

Your objective: (1) extract structured data from the contract, and (2) analyse that data against Article 104. Return everything as a single JSON object with two top-level objects, "contract_data" and "tax_analysis". A downstream program builds an Excel file from your JSON, so output data only -- no commentary, no Markdown.

STEP 1 -- EXTRACT CONTRACT DATA (the "contract_data" object)
Read the whole contract and fill in every field listed in the FIELD GUIDE below.
- Never leave a field empty. If a piece of information is not explicitly stated in the contract, use exactly this value: "არ არის მითითებული".
- Proper nouns -- company names, individual names, addresses -- must be copied exactly as written in the contract. Never translate or transliterate them.
- Every other value must be written in natural, professional legal Georgian.
- Dates: DD/MM/YYYY format where possible.

FIELD GUIDE
Party A is the first party named in the contract; Party B is the second. For each party, fill six fields:
- "party_a_name" / "party_b_name": the party's full name, exactly as written in the contract.
- "party_a_inn" / "party_b_inn": the party's tax identification number. It may be labelled INN, TIN, tax ID, identification number, company number, or registration number. Copy it exactly as written.
- "party_a_legal_form" / "party_b_legal_form": the party's legal form of incorporation (LLC, Ltd, GmbH, JSC, individual entrepreneur, ...), written in Georgian, e.g. "შეზღუდული პასუხისმგებლობის საზოგადოება" or "ინდივიდუალური მეწარმე".
- "party_a_address" / "party_b_address": the party's registered/legal address, exactly as written.
- "party_a_role" / "party_b_role": "შემსრულებელი" for the party providing the service, "შემკვეთი" for the party ordering and paying for it.
- "party_a_residency" / "party_b_residency": residency relative to Georgia, with the country in brackets, e.g. "რეზიდენტი (საქართველო)" or "არარეზიდენტი (გერმანია)". If not stated outright, judge from the party's address or place of incorporation.

Then the terms of the deal:
- "service_type": the category of service -- see SERVICE TYPE below.
- "service_description": briefly describe the specific scope of work of this contract, in Georgian (a longer, contract-specific version of the service type).
- "contract_value": the total contract price as a plain number -- no currency symbols, no thousands separators.
- "payment_frequency": how often payment is made, e.g. "ერთჯერადი" (one-time), "ყოველთვიური" (monthly), "ეტაპობრივი" (milestone-based).
- "currency": ISO code of the contract currency (USD, EUR, GEL, ...).
- "payment_terms": when and under what conditions payment is due, e.g. "წინასწარი გადახდა" (advance payment) or "ინვოისის მიღებიდან 10 დღეში" (10 days after receiving the invoice).
- "signing_date": the date the contract was signed.
- "effective_date": the date the contract enters into force.
- "contract_duration": how long the contract remains in force, e.g. "12 თვე", "1 წელი", "უვადო" (indefinite). If only the effective and end dates are given, derive the duration from them.
- "end_date": the date the contract expires or is set to terminate.
- "place_of_service": where the service is performed or delivered.

SERVICE TYPE ("service_type"): match the contract against these categories and write the Georgian name given after the arrow:
- IT services (software development, support, hosting) -> "IT მომსახურება"
- Consulting services (business consulting, financial advice) -> "საკონსულტაციო მომსახურება"
- Marketing services (advertising campaigns, SMM, branding) -> "მარკეტინგული მომსახურება"
- Legal services (representation, document preparation) -> "იურიდიული მომსახურება"
- Logistics services (cargo transportation, warehousing) -> "ლოგისტიკური მომსახურება"
If none of these fit, describe the actual service found in the contract, in Georgian.

STEP 2 -- TAX ANALYSIS UNDER ARTICLE 104 (the "tax_analysis" object)
Using the data you extracted (especially the parties' residency, roles, service type, and place of service), analyse the contract against the Georgian text of Article 104 (income received from a source in Georgia) in the attached `article_104.txt`. Work strictly from that text -- do not rely on outside knowledge of the Tax Code.
- "is_georgian_source_income": "დიახ" or "არა" -- does the income received under this contract count as income from a Georgian source under Article 104?
- "is_georgian_source_income_justification": justify the answer in professional Georgian, citing the exact clause/sub-clause of Article 104 (e.g. "104-ე მუხლის პირველი ნაწილის „გ“ ქვეპუნქტი").
- "withholding_or_reverse_vat_obligation": "დიახ", "არა", or "ნაწილობრივ" -- does a withholding tax obligation at the source of payment, or a reverse-charge VAT obligation, arise for either party?
- "withholding_or_reverse_vat_explanation": briefly explain the reasoning in professional Georgian, referencing the relevant provision where applicable.

OUTPUT FORMAT
Return ONLY the single JSON object below, with every key present and every value filled in. No commentary before or after it, no Markdown, no code fences. The JSON keys must stay exactly as written here, in Latin characters -- they are structural identifiers for a downstream program and are never shown to a human reader. All values follow the rules above (Georgian, except proper nouns, dates, and numbers).

{
  "contract_data": {
    "party_a_name": "",
    "party_a_inn": "",
    "party_a_legal_form": "",
    "party_a_address": "",
    "party_a_role": "",
    "party_a_residency": "",
    "party_b_name": "",
    "party_b_inn": "",
    "party_b_legal_form": "",
    "party_b_address": "",
    "party_b_role": "",
    "party_b_residency": "",
    "service_type": "",
    "service_description": "",
    "contract_value": "",
    "payment_frequency": "",
    "currency": "",
    "payment_terms": "",
    "signing_date": "",
    "effective_date": "",
    "contract_duration": "",
    "end_date": "",
    "place_of_service": ""
  },
  "tax_analysis": {
    "is_georgian_source_income": "",
    "is_georgian_source_income_justification": "",
    "withholding_or_reverse_vat_obligation": "",
    "withholding_or_reverse_vat_explanation": ""
  }
}
