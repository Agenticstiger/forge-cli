# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Column-name PII pre-classifier (name-based, no value scanning).

H6 fix: contracts emitted by the JDBC introspection path and the LLM
synthesis path were leaving obvious PII columns (``c_email``, ``ssn``,
``date_of_birth``, ``phone_number``, …) untagged. The Judge ``security``
axis scored 1-2 because the contract carried no PII signal at all even
when the column literally contained the word ``email``.

This module ships a small, deterministic, **name-based** classifier
that tags FLUID v0.7.3 columns with:

* ``tags: ["pii-email", "pii-phone", …]`` — kebab-case, matches the
  schema's ``[a-z0-9][a-z0-9-]*[a-z0-9]`` tag pattern
* ``sensitivity: "pii"`` (or ``"phi"`` for the few healthcare classes)
  — using the canonical ``sensitivityLevel`` enum
* ``semanticType: "email" | "phone" | …`` — free-form semantic-type
  string already declared on the column schema

The classifier is **column-name only**. Value scanning (e.g. Presidio
NER, GCP DLP InspectContent) is a separate concern with very different
performance / privacy implications and is intentionally out of scope —
the CLI must stay light.

Prior art surveyed (search receipts in the PR description):

* **tokern/piicatcher** (Apache-2.0) — direct prior art. The
  ``ColumnNameRegexDetector`` (``piicatcher/scanner.py``) keys a
  regex table by PII class. Pattern adapted; the vocabulary below
  extends piicatcher's set with the Presidio / GCP DLP / AWS Glue
  category names we need.
* **microsoft/presidio** (MIT) — heavy reference (~30 deps including
  spaCy / phonenumbers / tldextract). Too much for a CLI we want to
  stay light, but the canonical entity vocabulary is borrowed
  (``EMAIL_ADDRESS``, ``PHONE_NUMBER``, ``US_SSN``, ``CREDIT_CARD``,
  …). Verbatim Presidio email pattern is the reference for our
  email value-form regex (unused here since we're name-based; kept
  in the docstring for the inevitable future value-scanner).
* **madisonmay/CommonRegex** (MIT) — the lightweight ancestor most
  other libs build on (piiregex, piicatcher, DataFog). Confirms the
  shape: a small Python regex dict, no extra deps.
* **GCP DLP InfoType reference** — canonical list of ~150 PII
  categories (EMAIL_ADDRESS, PHONE_NUMBER, PERSON_NAME,
  US_SOCIAL_SECURITY_NUMBER, STREET_ADDRESS, IBAN_CODE,
  CREDIT_CARD_NUMBER, AGE, IP_ADDRESS, MAC_ADDRESS, …). Our class
  vocabulary aligns with this.
* **AWS Glue Managed PII Identifiers** — second confirmation of the
  vocabulary (PERSON_NAME, EMAIL, USA_SSN, PHONE_NUMBER, BANK_ACCOUNT,
  IP_ADDRESS, MAC_ADDRESS, USA_DRIVING_LICENSE, …).

Design choices:

* **One regex per PII class**, ``re.IGNORECASE`` against the whole
  column name (``re.search`` not ``re.fullmatch``) — matches
  piicatcher's approach. A column called ``customer_email_hash`` is
  still tagged ``pii-email`` (with ``sensitivity: pii``) because the
  *purpose* is still email-handling.
* **Multiple matches accepted** — ``classify_column("contact_phone_email")``
  → ``["email", "phone"]``. Caller can decide how to render. In
  practice, the column-naming conventions in the wild rarely produce
  multi-match collisions, but the rule is "tag everything that fits".
* **Ambiguous names get a single broad tag** — ``name`` could be
  person-name or org-name; we tag it ``name`` (sensitivity ``pii``)
  and let downstream / LLM refinement pick the subclass. This is the
  same call piicatcher makes for ``Person``.
* **No checksum validation** — the column NAME doesn't carry a value
  to validate. Luhn check on a column called ``credit_card_num`` is
  meaningless. Value-scanning is the right place for that.

Out of scope (intentional):

* Value scanning. Use Presidio or GCP DLP InspectContent.
* Locale-specific name lists. Presidio's recognizer registry already
  has per-country variants (es, de, fr, pl, …) — re-implementing
  those here would be reinventing.
* Statistical NER on column names. Names like ``acct_x123`` need NER
  + sample value scan; out of scope for the name-only pre-classifier.

Kill-switch: ``FLUID_COPILOT_PII_CLASSIFIER=0`` disables the pass
entirely (returns the original schema unchanged).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Pattern

LOG = logging.getLogger("fluid.copilot.pii")

__all__ = [
    "PII_CLASSIFIERS",
    "PII_SENSITIVITY_MAP",
    "apply_pii_tags",
    "classifier_enabled",
    "classify_column",
]


# ---------------------------------------------------------------------------
# Vocabulary keyed by canonical PII class. The class names are
# lowercase ASCII tokens that map 1:1 onto ``semanticType`` strings on
# the column object and onto ``pii-<class>`` tags in ``column.tags``.
# Pattern shape and naming derived from tokern/piicatcher (Apache-2.0)
# `ColumnNameRegexDetector`; vocabulary extended from Presidio / GCP DLP /
# AWS Glue.
#
# Patterns are intentionally **anchored loosely** (``re.search``, no
# ``^`` / ``$``) so prefixed/suffixed columns still tag — e.g. a column
# called ``encrypted_email_hash`` matches the email pattern. Each
# alternative branch is bounded so we don't over-tag (e.g. ``email``
# alone does NOT match the ``credit_card`` pattern even though both
# branches share short tokens).
# ---------------------------------------------------------------------------


PII_CLASSIFIERS: Dict[str, Pattern[str]] = {
    # Email — based on piicatcher + Presidio's EMAIL_ADDRESS context list.
    # Matches: email, e_mail, e-mail, mail (when not "voicemail"),
    # email_address, user_email, customer_email.
    "email": re.compile(
        r"(?:^|[_\-])e[_\-]?mail(?:[_\-]?address)?(?:[_\-]|$)|"
        r"(?:^|[_\-])email(?:[_\-]|$)|"
        r"\bemail\b|"
        r"(?:^|[_\-])mail(?:[_\-]?to)?(?:[_\-]|$)",
        re.IGNORECASE,
    ),
    # Phone — piicatcher + GCP DLP PHONE_NUMBER + AWS Glue PHONE_NUMBER.
    # Matches: phone, telephone, mobile, cell, cellphone, fax,
    # phone_no/num/number, contact_phone.
    "phone": re.compile(
        r"(?:^|[_\-])(?:phone|telephone|mobile|cellphone|cell_?phone|"
        r"fax)(?:[_\-]?(?:number|num|no))?(?:[_\-]|$)|"
        r"\b(?:phone|telephone|mobile|cellphone|fax)\b",
        re.IGNORECASE,
    ),
    # SSN — piicatcher + Presidio US_SSN + GCP DLP US_SOCIAL_SECURITY_NUMBER.
    # US-only. Other national IDs land under ``national_id``.
    "ssn": re.compile(
        r"(?:^|[_\-])(?:ssn|ss_?n|social_?security(?:_?(?:number|no|num))?|"
        r"social_?security_?number)(?:[_\-]|$)|"
        r"\bssn\b",
        re.IGNORECASE,
    ),
    # National ID — Presidio + GCP DLP cover INDIA_AADHAAR, BRAZIL_CPF,
    # FRANCE_NIR, GERMANY_IDENTITY_CARD_NUMBER, UK_NINO, etc. We tag
    # the obvious column-name forms; ``ssn`` is its own class above.
    # Matches: aadhaar, cpf, cnpj, nin, nino, national_id, passport,
    # itin, driver_license, drivers_license, driving_license, dni,
    # voter_id.
    "national_id": re.compile(
        r"(?:^|[_\-])(?:aadhaar|aadhar|cpf|cnpj|nino?|national_?id|"
        r"passport(?:_?(?:number|no|num))?|itin|"
        r"(?:driver|driving|drivers?)_?(?:license|licence)|dni|"
        r"voter_?id|tax_?id|ein|tin)(?:[_\-]|$)|"
        r"\b(?:aadhaar|cpf|cnpj|passport|itin)\b",
        re.IGNORECASE,
    ),
    # Payment card — piicatcher CreditCard + Presidio CREDIT_CARD.
    # Matches: credit_card, credit_card_num/number, cc_num/number,
    # creditcard, ccnum, card_number, cardnumber, debit_card, cvv,
    # cvc, iban.
    "payment_card": re.compile(
        r"(?:^|[_\-])(?:credit_?card(?:_?(?:number|num|no))?|"
        r"cc_?(?:number|num|no)|debit_?card|"
        r"card_?(?:number|num|no)|cvv|cvc|iban|bank_?account|"
        r"account_?number)(?:[_\-]|$)|"
        r"\b(?:creditcard|cardnumber|iban|cvv|cvc)\b",
        re.IGNORECASE,
    ),
    # Date of birth — piicatcher BirthDate.
    # Matches: dob, date_of_birth, dateofbirth, birthday, birthdate,
    # date_of_death (still PII per HIPAA / GDPR), birth_year.
    "dob": re.compile(
        r"(?:^|[_\-])(?:dob|d_o_b|date_?of_?birth|date_?of_?death|"
        r"birth_?date|birth_?day|birthday|birth_?year)(?:[_\-]|$)|"
        r"\b(?:dob|birthdate|birthday)\b",
        re.IGNORECASE,
    ),
    # Address — piicatcher Address + GCP DLP STREET_ADDRESS. We tag
    # STREET / POSTAL addresses. The bare token ``address`` (or
    # ``addr``) is ambiguous with ``email_address`` / ``ip_address`` /
    # ``mac_address`` — those collisions are resolved by the
    # de-conflict pass in ``classify_column`` (which strips ``address``
    # when ``email``/``ip_address``/``mac_address`` also matched). The
    # regex itself can stay broad; the de-conflict is policy.
    "address": re.compile(
        r"(?:^|[_\-])(?:address|addr|street(?:_?(?:name|address))?|"
        r"mailing_?address|billing_?address|shipping_?address|"
        r"home_?address|delivery_?address|postal_?address|"
        r"physical_?address|residential_?address|"
        r"postal_?code|post_?code|zip(?:_?code)?|zipcode|po_?box)"
        r"(?:[_\-]|$)|"
        r"\b(?:street|postcode|zipcode|po_?box)\b",
        re.IGNORECASE,
    ),
    # Geo coords — GCP DLP LOCATION + Presidio LOCATION. Matches:
    # latitude, longitude, geo_lat, geo_lng, gps_coords.
    "geo": re.compile(
        r"(?:^|[_\-])(?:lat(?:itude)?|long(?:itude)?|lng|"
        r"gps_?(?:coords?|location)|geo_?(?:lat|long|lng|location))"
        r"(?:[_\-]|$)|"
        r"\b(?:latitude|longitude)\b",
        re.IGNORECASE,
    ),
    # Person name — piicatcher Person. Ambiguous (could be org-name);
    # we tag and let the LLM / downstream refinement pick the subclass.
    # Matches: first_name, fname, last_name, lname, full_name, surname,
    # given_name, family_name, middle_name, maiden_name, nickname,
    # person_name, customer_name, user_name (NOT username for login).
    # Note: ``user_name`` is handled by the ``credentials`` class
    # (login-username), so this branch deliberately excludes the bare
    # ``user_name`` form to avoid double-tag.
    "name": re.compile(
        r"(?:^|[_\-])(?:first_?name|fname|last_?name|lname|"
        r"full_?name|sur_?name|given_?name|family_?name|"
        r"middle_?name|maiden_?name|nick_?name|person_?name|"
        r"contact_?name)(?:[_\-]|$)|"
        r"\b(?:firstname|lastname|fullname|surname|nickname)\b",
        re.IGNORECASE,
    ),
    # IP address — piicatcher + GCP DLP IP_ADDRESS + AWS Glue IP_ADDRESS.
    # Matches: ip, ip_address, ip_addr, ipv4, ipv6, client_ip,
    # x_forwarded_for, remote_ip.
    "ip_address": re.compile(
        r"(?:^|[_\-])(?:ip|ip_?addr(?:ess)?|ipv4|ipv6|"
        r"client_?ip|remote_?ip|x_?forwarded_?for)(?:[_\-]|$)|"
        r"\b(?:ipaddress|ipaddr|ipv4|ipv6)\b",
        re.IGNORECASE,
    ),
    # MAC address — AWS Glue MAC_ADDRESS. Matches: mac, mac_address,
    # mac_addr.
    "mac_address": re.compile(
        r"(?:^|[_\-])mac_?addr(?:ess)?(?:[_\-]|$)|" r"\bmacaddr(?:ess)?\b",
        re.IGNORECASE,
    ),
    # Credentials / secrets — piicatcher Password + GCP DLP PASSWORD /
    # AUTH_TOKEN. Matches: password, passwd, pass, pwd, secret, token,
    # api_key, apikey, api-key, auth_token, authorization, credential,
    # private_key, access_key, refresh_token, session_id.
    "credentials": re.compile(
        r"(?:^|[_\-])(?:pass(?:word|wd)?|pwd|secret|token|"
        r"api_?key|access_?key|refresh_?token|auth_?token|"
        r"authorization|credentials?|private_?key|session_?id|"
        r"client_?secret)(?:[_\-]|$)|"
        r"\b(?:password|passwd|apikey|api_key|token|secret)\b",
        re.IGNORECASE,
    ),
    # Username — piicatcher UserName + GCP DLP USER_NAME. Login/
    # account name, NOT a person's name (those go under ``name``).
    # Matches: username, user_name, login, login_name, account_name,
    # handle. **Does NOT** match a bare ``user_id`` — that's an
    # opaque numeric identifier in most warehouses and tagging every
    # ``user_id`` as PII is the kind of false-positive that erodes
    # signal trust. piicatcher takes the same call (``user(id|name|)``
    # only when preceded by another token like ``user``, not standalone).
    "username": re.compile(
        r"(?:^|[_\-])(?:user_?name|login(?:_?name)?|"
        r"account_?name|handle)(?:[_\-]|$)|"
        r"\b(?:username|login)\b",
        re.IGNORECASE,
    ),
    # Health / medical — Presidio + GCP DLP medical categories
    # (MEDICAL_RECORD_NUMBER, US_DEA_NUMBER, US_HEALTHCARE_NPI).
    # Tagged ``phi`` not ``pii``. Matches: mrn, medical_record_number,
    # patient_id, diagnosis, icd, icd10, hipaa_id, npi, dea.
    "medical": re.compile(
        r"(?:^|[_\-])(?:mrn|medical_?record_?(?:number|num|no)?|"
        r"patient_?id|diagnosis|icd(?:_?10)?|hipaa_?id|"
        r"npi|dea(?:_?number)?)(?:[_\-]|$)|"
        r"\b(?:mrn|hipaa)\b",
        re.IGNORECASE,
    ),
    # Race / ethnicity / religion / sexual orientation — Presidio's
    # GDPR "special category" set. Matches: race, ethnicity, religion,
    # sexual_orientation, gender, gender_identity.
    "special_category": re.compile(
        r"(?:^|[_\-])(?:race|ethnicity|religion|sexual_?orientation|"
        r"gender|gender_?identity|marital_?status)(?:[_\-]|$)|"
        r"\b(?:race|ethnicity|religion|gender)\b",
        re.IGNORECASE,
    ),
}


# Map PII class → sensitivity-level enum from the FLUID v0.7.3 schema
# (``sensitivityLevel`` $def). Most lands under ``pii``; the medical
# class is ``phi``; credentials map to ``restricted`` because
# ``cleartext`` describes a treatment state not a content class. The
# narrative is "the sensitivity label tells you *how careful* you must
# be"; the tag list tells you *what* the column is.
PII_SENSITIVITY_MAP: Dict[str, str] = {
    "email": "pii",
    "phone": "pii",
    "ssn": "pii",
    "national_id": "pii",
    "payment_card": "pii",
    "dob": "pii",
    "address": "pii",
    "geo": "pii",
    "name": "pii",
    "ip_address": "pii",
    "mac_address": "pii",
    "credentials": "restricted",
    "username": "pii",
    "medical": "phi",
    "special_category": "pii",
}


def classifier_enabled() -> bool:
    """Kill-switch — ``FLUID_COPILOT_PII_CLASSIFIER=0`` returns False."""
    value = os.environ.get("FLUID_COPILOT_PII_CLASSIFIER", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def classify_column(name: str) -> List[str]:
    """Return the list of PII class labels matching ``name``.

    Empty list when the column name carries no PII signal. Multiple
    matches are returned in alphabetical order so the output is
    deterministic across runs (important for diff-friendly contract
    re-emission).

    De-conflict rules (applied AFTER the regex pass, BEFORE the sort):

    * If ``email`` / ``ip_address`` / ``mac_address`` matched, drop
      ``address`` (the postal-address pattern over-matches on the
      ``_address`` suffix common to those classes). The more specific
      class wins.
    * If ``username`` matched, drop ``name`` (login fields shouldn't
      double-tag as person-name).
    """
    if not name or not isinstance(name, str):
        return []
    matches: List[str] = []
    for pii_class, pattern in PII_CLASSIFIERS.items():
        if pattern.search(name):
            matches.append(pii_class)

    matches_set = set(matches)
    # ``email_address`` / ``ip_address`` / ``mac_address`` are the more
    # specific classes — strip the postal ``address`` match.
    if matches_set & {"email", "ip_address", "mac_address"} and "address" in matches_set:
        matches = [m for m in matches if m != "address"]
    # ``username`` is more specific than ``name`` — strip ``name`` when
    # we already tagged the column as a login handle.
    if "username" in matches_set and "name" in matches_set:
        matches = [m for m in matches if m != "name"]

    return sorted(matches)


def apply_pii_tags(
    schema: List[Dict[str, Any]],
    *,
    overwrite: bool = False,
) -> List[Dict[str, Any]]:
    """Walk every column in *schema*, classify, and attach PII metadata.

    Mutates **and** returns the input list (typical schema-emit usage
    expects in-place modification; returning the list is a courtesy
    for one-liners).

    For each column whose name matches one or more PII classes we set:

    * ``tags`` — appended (de-duplicated) with ``pii-<class>`` per match
    * ``sensitivity`` — set to the strongest sensitivity across matches
      (priority order: ``phi`` > ``restricted`` > ``pii``)
    * ``semanticType`` — set to the single best-match class when the
      column has no existing semanticType. Multi-match columns get the
      first alphabetic class.

    Parameters
    ----------
    schema
        List of column dicts (FLUID v0.7.3 ``column`` shape).
    overwrite
        When False (default), an existing ``sensitivity`` or
        ``semanticType`` value is preserved (conservative — never
        stomp user-set or LLM-set fields). When True, values are
        overwritten — used by integration tests, not by the runtime
        path. Tags are always merged (de-duplicated, not overwritten).
    """
    if not classifier_enabled():
        return schema
    if not isinstance(schema, list):
        return schema

    for col in schema:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        classes = classify_column(str(name) if name else "")
        if not classes:
            continue

        # Tags — merge into existing list, de-dup, keep order stable.
        existing_tags = col.get("tags") or []
        if not isinstance(existing_tags, list):
            existing_tags = []
        new_tags = [f"pii-{c}".replace("_", "-") for c in classes]
        merged: List[str] = list(existing_tags)
        for t in new_tags:
            if t not in merged:
                merged.append(t)
        col["tags"] = merged

        # Sensitivity — strongest wins. phi > restricted > pii.
        sens_priority = {"phi": 3, "restricted": 2, "pii": 1}
        candidate_levels = [PII_SENSITIVITY_MAP[c] for c in classes]
        candidate_levels.sort(key=lambda s: sens_priority.get(s, 0), reverse=True)
        new_sensitivity = candidate_levels[0]
        if overwrite or not col.get("sensitivity"):
            col["sensitivity"] = new_sensitivity

        # Semantic type — single best-match class. Pick first
        # alphabetically (already sorted by classify_column).
        if overwrite or not col.get("semanticType"):
            col["semanticType"] = classes[0]

    return schema


def _classify_schema_summary(schema: List[Dict[str, Any]]) -> Dict[str, int]:
    """Return ``{pii_class: count}`` for observability/telemetry.

    Read-only helper — does not mutate the schema. Used by the
    enrichment pass to surface a one-line summary like
    ``"3 PII columns tagged: 2 email, 1 phone"`` without re-doing the
    regex match.
    """
    summary: Dict[str, int] = {}
    for col in schema:
        if not isinstance(col, dict):
            continue
        name = col.get("name")
        for cls in classify_column(str(name) if name else ""):
            summary[cls] = summary.get(cls, 0) + 1
    return summary


def classify_contract_schemas(
    contract: Dict[str, Any],
    *,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Walk every ``exposes[].contract.schema`` in *contract* and
    apply PII tags in place. Returns a summary dict suitable for
    persistence under ``.fluid/agents/<run-id>/enrichment/pii.json``.

    Returns ``{models: [{model_name, tagged: {pii_class: count}}], totals: {pii_class: count}}``.
    Empty totals when no PII detected or when the classifier is
    disabled via the ``FLUID_COPILOT_PII_CLASSIFIER=0`` kill switch.
    """
    summary: Dict[str, Any] = {"models": [], "totals": {}}
    # Respect the kill switch up front — no tag emission, no summary
    # population. The caller's enrichment receipt still records the
    # empty summary so judges / downstream consumers can distinguish
    # "no PII detected" from "classifier was off".
    if not classifier_enabled():
        return summary

    exposes = contract.get("exposes") if isinstance(contract, dict) else None
    if not isinstance(exposes, list):
        return summary

    for expose in exposes:
        if not isinstance(expose, dict):
            continue
        model_name = expose.get("exposeId") or expose.get("name") or "model"
        ec = expose.get("contract") or {}
        if not isinstance(ec, dict):
            continue
        schema = ec.get("schema")
        if not isinstance(schema, list):
            continue
        per_model = _classify_schema_summary(schema)
        apply_pii_tags(schema, overwrite=overwrite)
        if per_model:
            summary["models"].append({"model_name": str(model_name), "tagged": per_model})
            for cls, count in per_model.items():
                summary["totals"][cls] = summary["totals"].get(cls, 0) + count
    return summary


_LOG: Optional[logging.Logger] = LOG  # alias so test patches resolve
