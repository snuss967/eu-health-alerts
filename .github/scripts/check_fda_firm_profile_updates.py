#!/usr/bin/env python3
"""Detect changes to a specific FDA Data Dashboard Firm Profile page.

This checker is intentionally separate from the HERA RSS checker because the FDA
Firm Profile page is not an RSS feed. When FDA Data Dashboard API credentials are
configured, it monitors the firm-specific API datasets by FEI number. It also
keeps a normalized public-page snapshot so layout/page-level changes are caught.

Expected GitHub Actions secrets for API mode:
  FDA_DD_AUTH_USER
  FDA_DD_AUTH_KEY

Without API credentials, the script falls back to a public HTML/text snapshot of
the supplied Firm Profile URL. That fallback is useful, but may not capture
client-side dashboard data changes that are populated after page load.
"""

import datetime as _dt
import hashlib
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, Iterable, List, Tuple

import requests
from bs4 import BeautifulSoup

DEFAULT_FDA_FIRM_PROFILE_URL = (
    "https://datadashboard.fda.gov/oii/firmprofile.htm?FEIi=3013702557&/identity/3013702557"
)
DEFAULT_FEI_NUMBER = "3013702557"
DEFAULT_API_BASE_URL = "https://api-datadashboard.fda.gov/v1"

STATE_FILE = os.environ.get("STATE_FILE", ".fda_firmprofile_state.json")
FDA_FIRM_PROFILE_URL = os.environ.get("FDA_FIRM_PROFILE_URL", DEFAULT_FDA_FIRM_PROFILE_URL)
FDA_FEI_NUMBER = os.environ.get("FDA_FEI_NUMBER", DEFAULT_FEI_NUMBER).strip()
FDA_DD_AUTH_USER = os.environ.get("FDA_DD_AUTH_USER", "").strip()
FDA_DD_AUTH_KEY = os.environ.get("FDA_DD_AUTH_KEY", "").strip()
API_BASE_URL = os.environ.get("FDA_DD_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "false").lower() == "true"
REQUIRE_FDA_API = os.environ.get("REQUIRE_FDA_API", "false").lower() == "true"
REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "45"))

# FDA Data Dashboard returns statuscode 400 for success and may return 412 for
# valid requests with zero matching rows. Both should be treated as non-fatal.
FDA_API_SUCCESS_STATUS = 400
FDA_API_NO_RESULTS_STATUS = 412

API_ENDPOINTS: Dict[str, Dict[str, str]] = {
    "inspections_classifications": {
        "sort": "InspectionEndDate",
        "sortorder": "DESC",
        "label": "Inspections / classifications",
    },
    "inspections_citations": {
        "sort": "InspectionEndDate",
        "sortorder": "DESC",
        "label": "Inspection citations / 483 citations",
    },
    "compliance_actions": {
        "sort": "ActionTakenDate",
        "sortorder": "DESC",
        "label": "Compliance actions / warning letters",
    },
    "import_refusals": {
        "sort": "RefusalDate",
        "sortorder": "DESC",
        "label": "Import refusals",
    },
}

RECORD_KEY_FIELDS: Dict[str, Tuple[str, ...]] = {
    "inspections_classifications": (
        "InspectionID",
        "InspectionEndDate",
        "ClassificationCode",
        "Classification",
        "ProjectArea",
        "ProductType",
    ),
    "inspections_citations": (
        "InspectionID",
        "CitationID",
        "InspectionEndDate",
        "ActCFRNumber",
        "ShortDescription",
    ),
    "compliance_actions": (
        "CaseInjunctionID",
        "ActionType",
        "ActionTakenDate",
        "ProductType",
        "Center",
    ),
    "import_refusals": (
        "ShipmentID",
        "ProductCode",
        "RefusalDate",
        "RefusalCharges",
    ),
}


def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def write_output(name: str, value: str) -> None:
    """Write a GitHub Actions step output, preserving multiline values."""
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    with open(out, "a", encoding="utf-8") as f:
        if "\n" in value:
            delimiter = f"EOF_{uuid.uuid4().hex}"
            f.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
        else:
            f.write(f"{name}={value}\n")


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def coerce_fei_number(value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"FDA_FEI_NUMBER must be numeric for FDA Data Dashboard API filters: {value!r}") from exc


def request_headers(extra_headers: Dict[str, str] | None = None) -> Dict[str, str]:
    headers = {
        "User-Agent": "eu-health-alerts FDA Firm Profile Monitor/1.0",
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def fetch_public_page_component(url: str) -> Dict[str, Any]:
    response = requests.get(
        url,
        headers=request_headers({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

    normalized = {
        "url": response.url,
        "status_code": response.status_code,
        "text": text,
    }
    return {
        "label": "Public Firm Profile page snapshot",
        "count": 1,
        "digest": sha256_json(normalized),
        "record_ids": [sha256_json(normalized)],
        "preview": text[:1000],
        "url": response.url,
        "status_code": response.status_code,
        "content_length": len(response.text),
        "normalized_text_length": len(text),
    }


def api_rows_from_response(endpoint: str, data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, int | None]:
    statuscode = data.get("statuscode")
    message = data.get("message", "")
    if statuscode == FDA_API_NO_RESULTS_STATUS:
        return [], 0, 0
    if statuscode != FDA_API_SUCCESS_STATUS:
        raise RuntimeError(
            f"FDA Data Dashboard API returned statuscode={statuscode!r} for {endpoint}: {message!r}"
        )

    page_rows = data.get("result", [])
    if page_rows is None:
        page_rows = []
    if not isinstance(page_rows, list):
        raise RuntimeError(f"Unexpected FDA Data Dashboard API result payload from {endpoint}: {type(page_rows)}")

    resultcount = int(data.get("resultcount") or len(page_rows))
    totalrecordcount_raw = data.get("totalrecordcount")
    totalrecordcount = int(totalrecordcount_raw) if totalrecordcount_raw is not None else None
    return page_rows, resultcount, totalrecordcount


def fetch_api_rows(endpoint: str, fei_number: str, sort: str, sortorder: str) -> List[Dict[str, Any]]:
    fei = coerce_fei_number(fei_number)
    headers = request_headers(
        {
            "Content-Type": "application/json",
            "Authorization-User": FDA_DD_AUTH_USER,
            "Authorization-Key": FDA_DD_AUTH_KEY,
        }
    )
    url = f"{API_BASE_URL}/{endpoint}"
    rows: List[Dict[str, Any]] = []
    start = 1
    page_size = 5000
    max_pages = 25
    totalrecordcount: int | None = None

    for page_number in range(max_pages):
        payload = {
            "start": start,
            "rows": page_size,
            "returntotalcount": page_number == 0,
            "sort": sort,
            "sortorder": sortorder,
            "filters": {"FEINumber": [fei]},
            "columns": [],
        }
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected FDA Data Dashboard API response from {endpoint}: {str(data)[:500]}")

        page_rows, resultcount, page_totalrecordcount = api_rows_from_response(endpoint, data)
        if page_totalrecordcount is not None:
            totalrecordcount = page_totalrecordcount

        rows.extend(page_rows)
        if resultcount < page_size:
            break
        if totalrecordcount is not None and len(rows) >= totalrecordcount:
            break
        start += resultcount
    else:
        raise RuntimeError(f"Stopped paging {endpoint} after {max_pages} pages; increase max_pages if needed.")

    return rows


def normalized_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Sort by canonical JSON so cosmetic API ordering changes do not create false positives.
    return sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str))


def record_id(endpoint: str, row: Dict[str, Any]) -> str:
    fields = RECORD_KEY_FIELDS.get(endpoint, tuple(row.keys()))
    values = [str(row.get(field, "")).strip() for field in fields]
    raw = "|".join(values).strip("|")
    return raw or sha256_json(row)


def trim(value: Any, limit: int = 120) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def record_summary(endpoint: str, row: Dict[str, Any]) -> str:
    if endpoint == "inspections_classifications":
        return " | ".join(
            filter(
                None,
                [
                    trim(row.get("InspectionEndDate")),
                    f"Inspection {trim(row.get('InspectionID'))}" if row.get("InspectionID") else "",
                    trim(row.get("ClassificationCode") or row.get("Classification")),
                    trim(row.get("ProjectArea") or row.get("ProductType")),
                    trim(row.get("LegalName")),
                ],
            )
        )
    if endpoint == "inspections_citations":
        return " | ".join(
            filter(
                None,
                [
                    trim(row.get("InspectionEndDate")),
                    f"Inspection {trim(row.get('InspectionID'))}" if row.get("InspectionID") else "",
                    f"Citation {trim(row.get('CitationID'))}" if row.get("CitationID") else "",
                    trim(row.get("ActCFRNumber")),
                    trim(row.get("ShortDescription")),
                ],
            )
        )
    if endpoint == "compliance_actions":
        return " | ".join(
            filter(
                None,
                [
                    trim(row.get("ActionTakenDate")),
                    trim(row.get("ActionType")),
                    f"Case/Injunction {trim(row.get('CaseInjunctionID'))}" if row.get("CaseInjunctionID") else "",
                    trim(row.get("ProductType")),
                    trim(row.get("LegalName")),
                ],
            )
        )
    if endpoint == "import_refusals":
        return " | ".join(
            filter(
                None,
                [
                    trim(row.get("RefusalDate")),
                    f"Shipment {trim(row.get('ShipmentID'))}" if row.get("ShipmentID") else "",
                    trim(row.get("ProductCode")),
                    trim(row.get("ProductCodeDescription")),
                    f"Charges {trim(row.get('RefusalCharges'))}" if row.get("RefusalCharges") else "",
                ],
            )
        )
    return trim(row)


def fetch_api_component(endpoint: str, fei_number: str, meta: Dict[str, str]) -> Dict[str, Any]:
    rows = fetch_api_rows(endpoint, fei_number, meta["sort"], meta["sortorder"])
    records = [
        {
            "id": record_id(endpoint, row),
            "summary": record_summary(endpoint, row),
        }
        for row in rows
    ]
    return {
        "label": meta["label"],
        "count": len(rows),
        "digest": sha256_json(normalized_rows(rows)),
        "record_ids": [record["id"] for record in records],
        "records": records,
    }


def build_current_snapshot() -> Dict[str, Any]:
    api_credentials_configured = bool(FDA_DD_AUTH_USER and FDA_DD_AUTH_KEY)
    if REQUIRE_FDA_API and not api_credentials_configured:
        raise RuntimeError(
            "REQUIRE_FDA_API is true but FDA_DD_AUTH_USER / FDA_DD_AUTH_KEY are not configured. "
            "Add those secrets or set REQUIRE_FDA_API=false to allow public-page snapshot fallback."
        )

    components: Dict[str, Dict[str, Any]] = {
        "public_page": fetch_public_page_component(FDA_FIRM_PROFILE_URL),
    }

    mode = "public_page_only"
    if api_credentials_configured:
        mode = "api_plus_public_page"
        for endpoint, meta in API_ENDPOINTS.items():
            components[endpoint] = fetch_api_component(endpoint, FDA_FEI_NUMBER, meta)

    fingerprint_payload = {
        name: {
            "count": component.get("count"),
            "digest": component.get("digest"),
        }
        for name, component in sorted(components.items())
    }

    return {
        "monitoring_mode": mode,
        "api_credentials_configured": api_credentials_configured,
        "components": components,
        "fingerprint": sha256_json(fingerprint_payload),
    }


def changed_components(previous: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    prev_components = previous.get("component_digests", {}) or {}
    current_components = current.get("components", {}) or {}
    changed: List[str] = []
    for name, component in current_components.items():
        if prev_components.get(name) != component.get("digest"):
            changed.append(name)
    return changed


def component_counts(current: Dict[str, Any]) -> Dict[str, int]:
    return {name: int(component.get("count") or 0) for name, component in current.get("components", {}).items()}


def component_digests(current: Dict[str, Any]) -> Dict[str, str]:
    return {name: component.get("digest", "") for name, component in current.get("components", {}).items()}


def component_record_ids(current: Dict[str, Any]) -> Dict[str, List[str]]:
    return {name: list(component.get("record_ids", [])) for name, component in current.get("components", {}).items()}


def build_email_body(
    current: Dict[str, Any],
    previous: Dict[str, Any],
    changed: List[str],
    first_run: bool,
) -> str:
    lines = [
        f"FDA Firm Profile monitor detected an update for FEI {FDA_FEI_NUMBER}.",
        "",
        f"Firm Profile URL: {FDA_FIRM_PROFILE_URL}",
        f"Monitoring mode: {current['monitoring_mode']}",
    ]
    if not current.get("api_credentials_configured"):
        lines.extend(
            [
                "",
                "Note: FDA Data Dashboard API credentials are not configured, so this run used only the public HTML/text snapshot.",
                "For firm-level inspections, citations, compliance actions, and import-refusal data monitoring, add FDA_DD_AUTH_USER and FDA_DD_AUTH_KEY as GitHub Actions secrets.",
            ]
        )

    if first_run:
        lines.extend(["", "This is the first stored snapshot for this monitor."])

    lines.extend(["", "Changed sections:"])
    if changed:
        for name in changed:
            component = current["components"].get(name, {})
            label = component.get("label", name)
            count = component.get("count")
            prev_count = (previous.get("component_counts", {}) or {}).get(name)
            if prev_count is None:
                lines.append(f"- {label}: current count {count}")
            else:
                lines.append(f"- {label}: {prev_count} -> {count}")
    else:
        lines.append("- No section-level details available.")

    prev_ids_by_component = previous.get("record_ids", {}) or {}
    for name in changed:
        if name == "public_page":
            continue
        component = current["components"].get(name, {})
        prev_ids = set(prev_ids_by_component.get(name, []))
        records = component.get("records", [])
        new_records = [record for record in records if record.get("id") not in prev_ids]
        label = component.get("label", name)
        lines.extend(["", f"Potential new {label.lower()} records:"])
        if not prev_ids:
            lines.append("- No prior record-ID baseline exists for this section yet.")
        elif new_records:
            for record in new_records[:10]:
                lines.append(f"- {record.get('summary') or record.get('id')}")
            if len(new_records) > 10:
                lines.append(f"- ...and {len(new_records) - 10} more new record(s).")
        else:
            lines.append("- Existing record content changed, but no new record IDs were detected.")

    lines.extend(["", "Current counts:"])
    for name, component in current.get("components", {}).items():
        lines.append(f"- {component.get('label', name)}: {component.get('count')}")

    lines.extend(
        [
            "",
            "State file: " + STATE_FILE,
        ]
    )
    return "\n".join(lines)


def main() -> None:
    utc_now = _dt.datetime.now(_dt.UTC).replace(microsecond=0)
    now = utc_now.isoformat().replace("+00:00", "Z")
    today = utc_now.date().isoformat()
    previous = load_state(STATE_FILE)
    previous_fingerprint = previous.get("fingerprint")

    current = build_current_snapshot()
    current_fingerprint = current["fingerprint"]
    first_run = not previous_fingerprint
    has_updates = (bool(previous_fingerprint) and current_fingerprint != previous_fingerprint) or (
        first_run and NOTIFY_ON_FIRST_RUN
    )
    state_changed = current_fingerprint != previous_fingerprint
    changed = changed_components(previous, current)

    new_state = {
        "target": {
            "name": "FDA Data Dashboard Firm Profile",
            "fei_number": FDA_FEI_NUMBER,
            "url": FDA_FIRM_PROFILE_URL,
        },
        "monitoring_mode": current["monitoring_mode"],
        "api_credentials_configured": current["api_credentials_configured"],
        "fingerprint": current_fingerprint,
        "component_digests": component_digests(current),
        "component_counts": component_counts(current),
        "record_ids": component_record_ids(current),
        "last_checked_iso": now,
        "last_changed_iso": now if state_changed else previous.get("last_changed_iso"),
    }
    save_state(STATE_FILE, new_state)

    subject = f"FDA firm profile update: FEI {FDA_FEI_NUMBER} changed as of {today}"
    body = build_email_body(current, previous, changed, first_run)
    if not has_updates:
        subject = f"FDA firm profile check: no changes for FEI {FDA_FEI_NUMBER} as of {today}"
        body = "\n".join(
            [
                f"No FDA Firm Profile changes detected for FEI {FDA_FEI_NUMBER}.",
                "",
                f"Firm Profile URL: {FDA_FIRM_PROFILE_URL}",
                f"Monitoring mode: {current['monitoring_mode']}",
                f"State file: {STATE_FILE}",
            ]
        )

    write_output("has_updates", "true" if has_updates else "false")
    write_output("state_changed", "true" if state_changed else "false")
    write_output("email_subject", subject)
    write_output("email_body", body)

    print(subject)
    print(f"Monitoring mode: {current['monitoring_mode']}")
    print(f"State changed: {state_changed}")
    if changed:
        print("Changed components: " + ", ".join(changed))
    for name, component in current.get("components", {}).items():
        print(f"{name}: count={component.get('count')} digest={component.get('digest')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
