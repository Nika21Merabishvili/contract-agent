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

**What is and isn't comparable.** The script no longer asks the local model to *write* Georgian.
It emits English codes from closed sets and `georgian.py` maps them to Georgian, so 22 of the 27
fields are spelled correctly by construction. Comparing the local model's Georgian spelling
against claude.ai's therefore measures a Python dictionary, not a model — don't score it. The
axes that still compare like with like:

- **Extraction accuracy** — did each field get the right value?
- **Citation accuracy** — *which clauses* does the tax reasoning rest on, not just the verdict.
  Both models reached `არა` on the benchmark contract, but the local model got there by citing
  the cargo-transport clause and calling a principal place of business a `მუდმივი დაწესებულება`.
  Right answer, wrong law: worse than a wrong answer, because it is confident. The script emits
  an `_audit` block with the full clause checklist and the clauses cited — score against that.
- **Refusal to guess** — a field the contract is genuinely silent on should read
  `არ არის მითითებული`. A tax *verdict* must never read that; the script now raises instead of
  filling one in.

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
- "party_a_inn" / "party_b_inn": the party's TAX identification number, copied exactly as written. The label varies by country — accept any of: INN, TIN, tax ID, EIN, VAT number, USt-IdNr, SIREN, SIRET, HRB, Companies House number, company number, registration number, identification number. It is usually near the party's address, often in parentheses. An SEC CIK number is a filer key, NOT a tax identification number — never put a CIK in this field. If the party has no tax identification number anywhere in the contract, use "არ არის მითითებული".
- "party_a_legal_form" / "party_b_legal_form": the party's legal form of incorporation (LLC, Ltd, GmbH, JSC, individual entrepreneur, ...), written in Georgian, e.g. "შეზღუდული პასუხისმგებლობის საზოგადოება" or "ინდივიდუალური მეწარმე". If not stated outright, infer it from the suffix of the party's name (Inc. → კორპორაცია, LLC/Ltd/GmbH → შეზღუდული პასუხისმგებლობის საზოგადოება, AG/JSC → სააქციო საზოგადოება) or from any statement of incorporation. Never combine two forms with a slash — choose one.
- "party_a_address" / "party_b_address": the party's registered/legal address, exactly as written.
- "party_a_role" / "party_b_role": "შემსრულებელი" for the party providing the service, "შემკვეთი" for the party ordering and paying for it.
- "party_a_residency" / "party_b_residency": residency relative to Georgia, with the country in brackets, e.g. "რეზიდენტი (საქართველო)" or "არარეზიდენტი (გერმანია)". If not stated outright, judge from the party's address or place of incorporation.

Then the terms of the deal:
- "service_type": the category of service -- see SERVICE TYPE below.
- "service_description": briefly describe the specific scope of work of this contract, in Georgian (a longer, contract-specific version of the service type).
- "contract_value": the TOTAL contract price as a plain number -- no currency symbols, no thousands separators. Rules, in order: (1) if the contract states a total, total face value, or aggregate fee, use it; (2) if it only lists instalments, phases, or milestones, add them all up and use the sum — infer the total this way rather than returning a single instalment; (3) a cap, ceiling, or "not to exceed" figure covering only a subset of the work (such as expenses) is NOT the contract value — ignore it; (4) if a stated total and the sum of phases disagree, use the stated total. Never invent a figure.
- "payment_frequency": how often payment is made, e.g. "ერთჯერადი" (one-time), "ყოველთვიური" (monthly), "ეტაპობრივი" (milestone-based). Payment tied to phases, milestones, or deliverables is "ეტაპობრივი". If not stated outright, infer it from the payment schedule.
- "currency": ISO code of the contract currency (USD, EUR, GEL, ...).
- "payment_terms": when and under what conditions payment is due, e.g. "წინასწარი გადახდა" (advance payment) or "ინვოისის მიღებიდან 10 დღეში" (10 days after receiving the invoice).
- "signing_date": the date the contract was signed.
- "effective_date": the date the contract enters into force. If not stated outright, infer it from any commencement clause; it may differ from the signing date.
- "contract_duration": how long the contract remains in force, e.g. "12 თვე", "1 წელი", "უვადო" (indefinite). If only the effective and end dates are given, derive the duration from them.
- "end_date": the date the contract expires or is set to terminate. If no expiry is stated outright, infer it from the final phase or milestone date.
- "place_of_service": where the service is actually performed or delivered. This is rarely labelled — infer it, using in order of weight: any clause restricting where work may be performed or where data or personnel may be located; the location of the infrastructure or staff doing the work; the provider's principal place of business. State the territory plainly and cite the restricting clause if there is one. Only use "არ არის მითითებული" if the contract is completely silent on location.

SERVICE TYPE ("service_type"): match the contract against these categories and write the Georgian name given after the arrow:
- IT services (software development, support, hosting) -> "IT მომსახურება"
- Consulting services (business consulting, financial advice) -> "საკონსულტაციო მომსახურება"
- Marketing services (advertising campaigns, SMM, branding) -> "მარკეტინგული მომსახურება"
- Legal services (representation, document preparation) -> "იურიდიული მომსახურება"
- Logistics services (cargo transportation, warehousing) -> "ლოგისტიკური მომსახურება"
If none of these fit, describe the actual service found in the contract, in Georgian.

STEP 2 -- TAX ANALYSIS UNDER ARTICLE 104 (the "tax_analysis" object)
Using the data you extracted (especially the parties' residency, roles, service type, and place of service), analyse the contract against the Georgian text of Article 104 (income received from a source in Georgia) in the attached `article_104.txt`. Work strictly from that text -- do not rely on outside knowledge of the Tax Code.

Do not go looking for the clause that fits. Article 104 defines a closed list, so work through it. For EACH of the sub-clauses გ.ა, გ.ბ, გ.გ, გ.დ, გ.ე, გ.ვ, გ.ზ, გ.თ answer yes/no with a one-line reason, then do the same for ა, ბ, დ, ე, ო, ჟ, რ. Answering "no" with a reason IS the required answer for a clause that does not fit -- do not skip it, and do not stop at the first clause that looks familiar.

Take special care with the three that decide most cross-border service contracts:
- გ.ა -- is the service PHYSICALLY performed on Georgian territory?
- გ.ზ -- are the parties in different states AND is the PROVIDER a Georgian resident? (If the provider is not a Georgian resident, this is "no".)
- გ.თ -- are the parties in different states AND does the provider render the service IN Georgia through a permanent establishment, an employee, or costs incurred in Georgia?

A "მუდმივი დაწესებულება" (permanent establishment) is a specific legal status. A party's principal place of business or head office is NOT by itself a permanent establishment. Do not assert one unless the contract states it.

Then fill in:
- "is_georgian_source_income": "დიახ" or "არა" -- "დიახ" only if at least one clause in your checklist is yes. If every clause is no, this must be "არა".
- "is_georgian_source_income_justification": justify the answer in professional Georgian, citing the exact clause/sub-clause of Article 104 (e.g. "104-ე მუხლის პირველი ნაწილის „გ.ზ“ ქვეპუნქტი"). For a "არა", cite the clauses that would have had to apply and did not.
- "withholding_or_reverse_vat_obligation": "დიახ", "არა", or "ნაწილობრივ" -- does a withholding tax obligation at the source of payment, or a reverse-charge VAT obligation, arise for either party? You must reach a verdict. Never answer "არ არის მითითებული" here: it is not one of the three allowed values, and a conclusion you could not reach is not an extracted fact.
- "withholding_or_reverse_vat_explanation": briefly explain the reasoning in professional Georgian, referencing the relevant provision where applicable.

Finally, add an "_audit" object next to the other two, so the citations can be scored rather than just the verdict:
- "_audit": { "checklist": { "<clause>": {"applies": "yes"|"no", "reason": "<one line, English>"}, ... }, "cited_clauses": ["<clause>", ...] }
The checklist must contain all fifteen clauses listed above. "cited_clauses" are the clauses your conclusion actually rests on.

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
  },
  "_audit": {
    "checklist": {
      "გ.ა": { "applies": "", "reason": "" },
      "გ.ბ": { "applies": "", "reason": "" },
      "გ.გ": { "applies": "", "reason": "" },
      "გ.დ": { "applies": "", "reason": "" },
      "გ.ე": { "applies": "", "reason": "" },
      "გ.ვ": { "applies": "", "reason": "" },
      "გ.ზ": { "applies": "", "reason": "" },
      "გ.თ": { "applies": "", "reason": "" },
      "ა": { "applies": "", "reason": "" },
      "ბ": { "applies": "", "reason": "" },
      "დ": { "applies": "", "reason": "" },
      "ე": { "applies": "", "reason": "" },
      "ო": { "applies": "", "reason": "" },
      "ჟ": { "applies": "", "reason": "" },
      "რ": { "applies": "", "reason": "" }
    },
    "cited_clauses": []
  }
}
