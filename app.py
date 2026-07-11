from datetime import datetime
import base64
import os
import shutil
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
import streamlit as st
import streamlit.components.v1 as components
from pathlib import Path
import pandas as pd
import httpx
import io

import db_buffer


def get_secret(key):
    """Access secrets from Streamlit Cloud Secrets, fallback to OS env vars."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, "")



DATA_DIR = Path("data")
PENDING = DATA_DIR / "pending_docs"
COMPLETED = DATA_DIR / "completed_docs"
FAILED = DATA_DIR / "failed_docs"
OUTPUT_CSV = DATA_DIR / "output.csv"
BATCH_ID_FILE = DATA_DIR / "current_batch_id"
BILLING_LOGS_DIR = DATA_DIR / "billing_logs"

BILLING_CSV_COLUMNS = [
    "created_at", "batch_id", "filename", "status", "model_used",
    "tokens_in", "tokens_out", "processing_time",
]

NEW_BATCH_COLUMNS = [
    "batch_id",
    "source_file",
    "document_index",
    "date",
    "seller",
    "buyer",
    "remark",
    "item",
    "unit_of_measure",
    "quantity",
    "unit_price",
    "total_amount",
    "line_remark",
    "processing_time_seconds",
    "model_used",
    "parse_status",
]

for d in [PENDING, COMPLETED, FAILED, BILLING_LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

MAX_BATCH_FILES = 10
MAX_FILE_MB = 15


def _save_billing_local(
    batch_id: int,
    filename: str,
    status: str,
    model_used: str,
    tokens_in: int,
    tokens_out: int,
    processing_time: float,
):
    """Append a billing record to the local CSV file for management review."""
    row = {
        "created_at": datetime.now().isoformat(),
        "batch_id": batch_id,
        "filename": filename,
        "status": status,
        "model_used": model_used,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "processing_time": processing_time,
    }
    billing_csv = BILLING_LOGS_DIR / "billing_logs.csv"
    write_header = not billing_csv.exists() or billing_csv.stat().st_size == 0
    df = pd.DataFrame([row])
    df.to_csv(billing_csv, mode="a", header=write_header, index=False)


def _read_batch_id() -> int:
    if BATCH_ID_FILE.exists():
        try:
            return int(BATCH_ID_FILE.read_text().strip())
        except (ValueError, TypeError):
            pass
    return 0


def _get_batch_progress(active_batch_id: int, batch_total: int = 0) -> dict:
    """Compute batch progress from CSV + Supabase job statuses.

    Source of truth:
    - completed/failed: output.csv rows filtered by batch_id (unique source_file count)
    - fallback_pending: Supabase invoices_queue WHERE status='fallback_pending' AND batch_id=X
    - total: max(csv_total, fallback_pending_total, batch_total)
    """
    completed = 0
    failed = 0
    fallback_pending = 0
    filenames = []

    if OUTPUT_CSV.exists():
        try:
            df = pd.read_csv(OUTPUT_CSV)
            if "batch_id" in df.columns and "source_file" in df.columns:
                batch_rows = df[df["batch_id"] == active_batch_id]
                unique_files = batch_rows["source_file"].unique().tolist()
                filenames = unique_files
                if "parse_status" in batch_rows.columns:
                    for fname in unique_files:
                        file_rows = batch_rows[batch_rows["source_file"] == fname]
                        if (file_rows["parse_status"] == "failed").any():
                            failed += 1
                        else:
                            completed += 1
                else:
                    completed = len(unique_files)
        except Exception:
            pass

    try:
        summary = db_buffer.fetch_batch_job_summary(active_batch_id)
        fallback_pending = summary.get("fallback_pending", 0)
    except Exception:
        pass

    total = max(completed + failed + fallback_pending, batch_total)
    remaining = max(0, total - completed - failed)

    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "queued": fallback_pending,
        "remaining": remaining,
        "filenames": filenames,
    }


st.set_page_config(page_title="Invoice Extractor", layout="wide")

st.markdown("""
<style>
    /* ── Global layout ── */
    .block-container {
        padding-top: 1rem;
        padding-bottom: 1rem;
        max-width: 1100px;
    }

    /* ── Banner ── */
    .banner-wrapper {
        margin-bottom: 1rem;
    }
    .banner-wrapper img {
        border-radius: 12px;
        width: 100%;
        display: block;
    }

    /* ── Hero header card ── */
    .hero-card {
        background: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 100%);
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 1.4rem 1.6rem;
        margin-bottom: 1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .hero-title {
        font-size: 1.5rem;
        font-weight: 700;
        color: #0f172a;
        margin: 0 0 0.15rem 0;
        letter-spacing: -0.01em;
    }
    .hero-subtitle {
        font-size: 0.88rem;
        color: #64748b;
        margin: 0;
    }
    .hero-badges {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
        margin-top: 0.6rem;
    }

    /* ── Section card ── */
    .dash-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 1.2rem 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .dash-card-header {
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: #64748b;
        margin-bottom: 0.3rem;
    }
    .dash-card-title {
        font-size: 1.05rem;
        font-weight: 700;
        color: #1e293b;
        margin: 0 0 0.15rem 0;
    }
    .dash-card-desc {
        font-size: 0.82rem;
        color: #64748b;
    }

    /* ── Upload card ── */
    .upload-section {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 1.4rem 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .upload-section .dash-card-header {
        margin-bottom: 0.15rem;
    }
    .upload-help {
        font-size: 0.82rem;
        color: #94a3b8;
        margin-bottom: 0.8rem;
    }

    /* ── Metric cards ── */
    .metric-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 1.1rem 1.2rem;
        text-align: center;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
        min-height: 110px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }
    .metric-card .metric-value {
        font-size: 1.9rem;
        font-weight: 700;
        line-height: 1.1;
        margin: 0.25rem 0;
    }
    .metric-card .metric-label {
        font-size: 0.72rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #64748b;
    }
    .metric-card .metric-note {
        font-size: 0.72rem;
        color: #94a3b8;
        margin-top: 0.15rem;
    }
    .metric-pending   { border-left: 4px solid #f59e0b; }
    .metric-completed { border-left: 4px solid #22c55e; }
    .metric-queued    { border-left: 4px solid #8b5cf6; }
    .metric-pending   .metric-value { color: #d97706; }
    .metric-completed .metric-value { color: #16a34a; }
    .metric-queued    .metric-value { color: #7c3aed; }

    /* ── Badges ── */
    .badge {
        display: inline-flex;
        align-items: center;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 0.25rem 0.7rem;
        border-radius: 999px;
        background: #f1f5f9;
        color: #475569;
        white-space: nowrap;
    }
    .badge-accent {
        background: #eff6ff;
        color: #2563eb;
    }
    .badge-pending {
        background: #fef3c7;
        color: #92400e;
    }
    .badge-completed {
        background: #dcfce7;
        color: #166534;
    }

    /* ── Notifications ── */
    .notif-area {
        margin-bottom: 1rem;
    }

    /* ── Workspace card (extracted data) ── */
    .workspace-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 1.2rem 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .workspace-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 0.5rem;
        margin-bottom: 0.4rem;
    }
    .workspace-title {
        font-size: 1.05rem;
        font-weight: 700;
        color: #1e293b;
        margin: 0;
    }
    .workspace-meta {
        font-size: 0.82rem;
        color: #64748b;
    }

    /* ── Sidebar ── */
    [data-testid="stSidebar"] {
        padding-top: 1rem;
    }
    [data-testid="stSidebar"] .stButton button {
        width: 100%;
    }
    .sidebar-section-label {
        font-size: 0.7rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: #94a3b8;
        margin-bottom: 0.3rem;
    }

    /* ── Misc ── */
    .section-divider {
        border: none;
        border-top: 1px solid #f1f5f9;
        margin: 0.2rem 0 0.8rem 0;
    }
    .toolbar-row {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        flex-wrap: wrap;
        margin-bottom: 0.6rem;
    }

    /* ── Processing animation ── */
    .processing-anim-container {
        text-align: center;
        padding: 1.2rem 1rem 0.4rem;
        margin-bottom: 0.5rem;
    }
    .processing-anim-text {
        font-size: 1rem;
        font-weight: 600;
        color: #475569;
        letter-spacing: 0.01em;
    }
</style>
""", unsafe_allow_html=True)


def _time_greeting() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

if "processed_files" not in st.session_state:
    st.session_state.processed_files = set()

if "uploader_key_counter" not in st.session_state:
    st.session_state.uploader_key_counter = 0

if "batch_status" not in st.session_state:
    st.session_state.batch_status = ""

if "edited_df" not in st.session_state:
    st.session_state.edited_df = pd.DataFrame()

if "active_batch_id" not in st.session_state:
    st.session_state.active_batch_id = _read_batch_id()

if "processing_just_finished" not in st.session_state:
    st.session_state.processing_just_finished = False

if "cloud_results" not in st.session_state:
    st.session_state.cloud_results = []

if "show_celebration" not in st.session_state:
    st.session_state.show_celebration = False

if "batch_files" not in st.session_state:
    st.session_state.batch_files = []

if "batch_total" not in st.session_state:
    st.session_state.batch_total = 0

if "processing_animation" not in st.session_state:
    st.session_state.processing_animation = False

if not BATCH_ID_FILE.exists():
    BATCH_ID_FILE.write_text(str(st.session_state.active_batch_id))


def _get_current_progress() -> dict:
    """Compute batch progress using session-state batch total as the denominator."""
    return _get_batch_progress(
        st.session_state.active_batch_id,
        batch_total=st.session_state.batch_total,
    )


_prog = _get_current_progress()
_pending = _prog["remaining"]
_completed = _prog["completed"]
_batch = _read_batch_id()

# ── Banner image (graceful fallback if missing) ──
BANNER_PATH = Path("assets") / "banner.png"
if BANNER_PATH.exists():
    st.markdown('<div class="banner-wrapper">', unsafe_allow_html=True)
    st.image(str(BANNER_PATH), width="stretch")
    st.markdown('</div>', unsafe_allow_html=True)
else:
    # Fallback: no banner image, just show the header below
    pass

# ── Hero header card ──
st.markdown(f"""
<div class="hero-card">
  <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.6rem;">
    <div>
      <div class="hero-title">Invoice Extraction App</div>
      <div class="hero-subtitle">{_time_greeting()} &mdash; upload invoices, review extracted rows, export clean results.</div>
    </div>
    <div class="hero-badges">
      <span class="badge badge-accent">Batch {_batch}</span>
      <span class="badge badge-pending">{_pending} pending</span>
      <span class="badge badge-completed">{_completed} completed</span>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)


def _reset_batch_state():
    """Clear all batch-specific session state for a new batch."""
    st.session_state.processed_files = set()
    st.session_state.edited_df = pd.DataFrame()
    st.session_state.cloud_results = []
    st.session_state.processing_just_finished = False
    st.session_state.show_celebration = False
    st.session_state.processing_animation = False
    st.session_state.batch_files = []
    st.session_state.batch_total = 0


# ---------------------------------------------------------------------------
# Sidebar – Compact utility
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        f"""<div style="padding:0.4rem 0 0.6rem 0;">
        <div class="sidebar-section-label">Batch Controls</div>
        <div style="font-size:0.82rem; color:#475569;">
            Active batch: <strong>{st.session_state.active_batch_id}</strong>
        </div>
        </div>""",
        unsafe_allow_html=True,
    )

    if st.button("Start New Batch", type="primary", width="stretch"):
        st.session_state.uploader_key_counter += 1
        _reset_batch_state()
        st.session_state.batch_status = "New batch started — upload files above."
        if "batch_pending_start" in st.session_state:
            del st.session_state["batch_pending_start"]
        new_batch_id = st.session_state.active_batch_id + 1
        st.session_state.active_batch_id = new_batch_id
        BATCH_ID_FILE.write_text(str(new_batch_id))
        st.cache_data.clear()
        st.rerun()

    st.markdown(
        '<div style="margin-top:0.4rem;">'
        '<div style="font-size:0.75rem; color:#94a3b8; line-height:1.4;">'
        'Archives are preserved across batches.'
        '</div></div>',
        unsafe_allow_html=True,
    )

    st.markdown("---")

    st.markdown(
        '<div style="font-size:0.7rem; color:#cbd5e1; letter-spacing:0.03em;">'
        'Invoice Extractor v1.0'
        '</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Cloud-first extraction (Gemini API) with fallback
# ---------------------------------------------------------------------------
def _extract_cloud(filepath: Path) -> tuple[list[dict], str, dict]:
    """Try Gemini API extraction. Returns (rows, model_used, usage_info) or raises."""
    with open(filepath, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    api_model = get_secret("API_MODEL")
    api_key = get_secret("API_KEY")
    api_base_url = get_secret("API_BASE_URL")

    payload = {
        "model": api_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CLOUD_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{api_base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        resp_json = resp.json()
        raw = resp_json["choices"][0]["message"]["content"]

    usage = resp_json.get("usage", {})
    usage_info = {
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }

    parsed, status = _parse_json(raw)
    if parsed is None:
        raise ValueError(f"Failed to parse cloud response: {raw[:200]}")

    rows = _flatten_docs(parsed, filepath.name)
    if not rows:
        raise ValueError("Cloud extraction returned no line items")

    model_label = f"api:{api_model}"
    for r in rows:
        r["model_used"] = model_label
        r["parse_status"] = status
    return rows, model_label, usage_info


def _extract_cloud_from_bytes(file_bytes: bytes, filename: str) -> tuple[list[dict], str, dict]:
    """Try Gemini API extraction from pre-read bytes. Avoids re-reading from disk."""
    image_b64 = base64.b64encode(file_bytes).decode("utf-8")

    api_model = get_secret("API_MODEL")
    api_key = get_secret("API_KEY")
    api_base_url = get_secret("API_BASE_URL")

    payload = {
        "model": api_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _CLOUD_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{api_base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        resp_json = resp.json()
        raw = resp_json["choices"][0]["message"]["content"]

    usage = resp_json.get("usage", {})
    usage_info = {
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
    }

    parsed, status = _parse_json(raw)
    if parsed is None:
        raise ValueError(f"Failed to parse cloud response: {raw[:200]}")

    rows = _flatten_docs(parsed, filename)
    if not rows:
        raise ValueError("Cloud extraction returned no line items")

    model_label = f"api:{api_model}"
    for r in rows:
        r["model_used"] = model_label
        r["parse_status"] = status
    return rows, model_label, usage_info


def _parse_json(raw: str) -> tuple:
    import json, re
    cleaned = raw.strip()
    if not cleaned:
        return None, "failed"
    try:
        return json.loads(cleaned), "ok"
    except json.JSONDecodeError:
        pass
    if cleaned.startswith("```"):
        first_nl = cleaned.find("\n")
        stripped = cleaned[first_nl + 1:] if first_nl != -1 else cleaned[3:]
        idx = stripped.rfind("```")
        if idx != -1:
            stripped = stripped[:idx].strip()
        if stripped:
            try:
                return json.loads(stripped), "repaired"
            except json.JSONDecodeError:
                cleaned = stripped
    repaired = cleaned.replace("'", '"')
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    open_braces = repaired.count("{") - repaired.count("}")
    if open_braces > 0:
        repaired += "}" * open_braces
    try:
        return json.loads(repaired), "repaired"
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start:i + 1]), "repaired"
                    except json.JSONDecodeError:
                        break
    return None, "failed"


def _flatten_docs(raw: dict, source_file: str) -> list[dict]:
    docs = raw.get("documents", [])
    if not docs:
        return []
    results = []
    for doc in docs:
        doc_index = doc.get("document_index", 1)
        date = doc.get("date", "")
        seller = doc.get("seller", "")
        buyer = doc.get("buyer", "")
        remark = doc.get("remark", "")
        items = doc.get("line_items", [])
        if not items:
            results.append({
                "source_file": source_file, "document_index": doc_index,
                "date": date, "seller": seller, "buyer": buyer, "remark": remark,
                "item": "", "unit_of_measure": "", "quantity": 0.0,
                "unit_price": 0.0, "total_amount": 0.0, "line_remark": "",
            })
            continue
        for li in items:
            text = (li.get("item") or "").strip()
            qty = li.get("quantity", 0) or 0
            if not text and qty == 0:
                continue
            results.append({
                "source_file": source_file, "document_index": doc_index,
                "date": date, "seller": seller, "buyer": buyer, "remark": remark,
                "item": text,
                "unit_of_measure": (li.get("unit_of_measure") or "").strip(),
                "quantity": float(li.get("quantity", 0) or 0),
                "unit_price": float(li.get("unit_price", 0) or 0),
                "total_amount": float(li.get("total_amount", 0) or 0),
                "line_remark": (li.get("line_remark") or "").strip(),
            })
    return results


_CLOUD_PROMPT = """You are a business document extraction assistant.
The image may be in Vietnamese, English, or mixed language.
Extract ALL visible documents and line items from the image.
Return ONLY valid JSON matching this schema (no markdown, no code fences, no extra keys):
{
  "documents": [
    {
      "document_index": 1,
      "date": "",
      "seller": "",
      "buyer": "",
      "remark": "",
      "line_items": [
        {
          "item": "",
          "unit_of_measure": "",
          "quantity": 0.0,
          "unit_price": 0.0,
          "total_amount": 0.0,
          "line_remark": ""
        }
      ]
    }
  ]
}
Rules:
- seller: issuing company, supplier, warehouse, or source side
- buyer: customer, requester, or person requesting goods
- Vietnamese labels: người bán/bên bán→seller, người mua/bên mua/người đề nghị→buyer
- Convert dates to dd/mm/yyyy
- Preserve Vietnamese text exactly
- Return 0.0 for missing numeric fields, "" for missing text fields
- Ignore blank table rows
- Output valid JSON only
"""


# ---------------------------------------------------------------------------
# Email delivery helper
# ---------------------------------------------------------------------------
def send_email_with_attachment(
    recipient: str,
    subject: str,
    body: str,
    attachment_bytes: bytes,
    attachment_filename: str,
) -> str:
    """Send an email with an Excel attachment via SMTP. Returns status message."""
    smtp_host = get_secret("SMTP_HOST")
    smtp_port = int(get_secret("SMTP_PORT") or 587)
    smtp_user = get_secret("SMTP_USER")
    smtp_pass = get_secret("SMTP_PASS")
    smtp_from = get_secret("SMTP_FROM")

    if not smtp_host:
        return "error: SMTP_HOST is not configured."
    if not smtp_user or not smtp_pass:
        return "error: SMTP credentials (SMTP_USER, SMTP_PASS) are not configured."
    if not smtp_from:
        return "error: SMTP_FROM sender address is not configured."

    msg = MIMEMultipart()
    msg["From"] = smtp_from
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    part = MIMEBase("application", "octet-stream")
    part.set_payload(attachment_bytes)
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{attachment_filename}"',
    )
    msg.attach(part)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            if smtp_port != 465:
                server.starttls()
                server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [recipient], msg.as_string())
        return "ok"
    except smtplib.SMTPAuthenticationError:
        return "error: SMTP authentication failed — check SMTP_USER and SMTP_PASS."
    except smtplib.SMTPConnectError:
        return f"error: Could not connect to {smtp_host}:{smtp_port}."
    except smtplib.SMTPException as exc:
        return f"error: SMTP error — {exc}"
    except OSError as exc:
        return f"error: Network error — {exc}"


# ---------------------------------------------------------------------------
# Upload area — wrapped in st.form for explicit submit
# ---------------------------------------------------------------------------
st.markdown('<div class="upload-section">', unsafe_allow_html=True)
st.markdown('<div class="dash-card-header">Upload Invoices</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="upload-help">Drop JPEG or PNG invoice images below, then click <strong>Process Files</strong> to start extraction. '
    f'(Max {MAX_BATCH_FILES} files, {MAX_FILE_MB} MB each)</div>',
    unsafe_allow_html=True,
)

uploader_key = f"uploader_{st.session_state.uploader_key_counter}"

with st.form("upload_form", clear_on_submit=False):
    uploaded_files = st.file_uploader(
        "Choose JPEG or PNG files",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key=uploader_key,
        label_visibility="collapsed",
    )
    submit_clicked = st.form_submit_button(
        "Process Files",
        type="primary",
        width="stretch",
    )

st.markdown('</div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Handle form submission — Phase 1: save files, trigger animation, then rerun
# ---------------------------------------------------------------------------
if submit_clicked and uploaded_files:
    new_files = [f for f in uploaded_files if f.name not in st.session_state.processed_files]
    if len(new_files) > MAX_BATCH_FILES:
        st.error(f"Too many files. Maximum is {MAX_BATCH_FILES} per batch. You selected {len(new_files)}.")
    else:
        oversized = [f for f in new_files if f.size > MAX_FILE_MB * 1024 * 1024]
        if oversized:
            names = ", ".join(f.name for f in oversized)
            st.error(f"File(s) exceed {MAX_FILE_MB} MB limit: {names}")
        else:
            saved = []
            for f in new_files:
                dest = PENDING / f.name
                with open(dest, "wb") as buf:
                    buf.write(f.getbuffer())
                saved.append(f.name)
                st.session_state.processed_files.add(f.name)

            if saved:
                st.session_state.batch_files = [f.name for f in uploaded_files]
                st.session_state.batch_total = len(uploaded_files)
                st.session_state.processing_animation = True
                st.rerun()

# ---------------------------------------------------------------------------
# Phase 2: animation render + processing
# The animation MUST render before the heavy processing block so the browser
# receives the HTML while the server is still crunching files.
# ---------------------------------------------------------------------------
if st.session_state.processing_animation and st.session_state.batch_files:
    # ── Show Lottie cat animation FIRST ──
    st.markdown(
        '<div class="processing-anim-container">'
        '<div class="processing-anim-text">Processing files... please wait!</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    _cat_anim_path = Path("assets") / "cat_animation.json"
    if _cat_anim_path.exists():
        import base64 as _b64
        _cat_data = _b64.b64encode(_cat_anim_path.read_bytes()).decode()
        _cat_src = f"data:application/json;base64,{_cat_data}"
    else:
        _cat_src = "https://assets2.lottiefiles.com/packages/lf20_9xLBhO.json"
    components.html(
        f"""
        <div style="display:flex;justify-content:center;padding:0.5rem 0;">
          <lottie-player
            src="{_cat_src}"
            background="transparent"
            speed="1"
            style="width:200px;height:200px;"
            loop autoplay>
          </lottie-player>
        </div>
        <script src="https://unpkg.com/@lottiefiles/lottie-player@latest/dist/lottie-player.js"></script>
        """,
        height=240,
    )

    # ── Now do the heavy processing ──
    saved = [
        f for f in st.session_state.batch_files
        if (PENDING / f).exists()
    ]

    if saved:
        batch_id = st.session_state.active_batch_id

        cloud_ok = 0
        cloud_failed = 0
        fallback_files = []
        all_rows = []
        billing_records = []

        for name in saved:
            filepath = PENDING / name

            try:
                file_bytes = filepath.read_bytes()
            except Exception:
                cloud_failed += 1
                fallback_files.append(name)
                continue

            try:
                t0 = time.perf_counter()
                rows, model_label, usage_info = _extract_cloud_from_bytes(file_bytes, filepath.name)
                elapsed = round(time.perf_counter() - t0, 3)

                for r in rows:
                    r["batch_id"] = batch_id
                    r["processing_time_seconds"] = elapsed
                all_rows.extend(rows)

                billing_records.append({
                    "batch_id": batch_id, "filename": name, "status": "success",
                    "model_used": model_label,
                    "tokens_in": usage_info.get("tokens_in", 0),
                    "tokens_out": usage_info.get("tokens_out", 0),
                    "processing_time": elapsed,
                })

                cloud_ok += 1
            except Exception as exc:
                job_id = None
                try:
                    job = db_buffer.create_job(name, file_bytes, batch_id)
                    job_id = job.get("id")
                except Exception:
                    pass
                if job_id:
                    try:
                        db_buffer.update_job_status(job_id, "fallback_pending")
                    except Exception:
                        pass

                billing_records.append({
                    "batch_id": batch_id, "filename": name, "status": "queued",
                    "model_used": "fallback_queue",
                    "tokens_in": 0, "tokens_out": 0, "processing_time": 0.0,
                })
                fallback_files.append(name)
                cloud_failed += 1

            try:
                shutil.move(str(filepath), str(COMPLETED / filepath.name))
            except Exception:
                pass

        if all_rows:
            try:
                if OUTPUT_CSV.exists():
                    df = pd.read_csv(OUTPUT_CSV)
                else:
                    df = pd.DataFrame(columns=NEW_BATCH_COLUMNS)
                new_df = pd.DataFrame(all_rows)
                df = pd.concat([df, new_df], ignore_index=True)
                df.to_csv(OUTPUT_CSV, index=False)
            except Exception:
                pass

        for rec in billing_records:
            try:
                db_buffer.log_billing(**rec)
            except Exception:
                pass
            _save_billing_local(**rec)

        if fallback_files:
            st.warning(
                f"Cloud extraction failed for {len(fallback_files)} file(s): "
                f"{', '.join(fallback_files)}. These have been queued for local "
                f"fallback processing. Run lworker.py on your local machine to "
                f"process them, then click Refresh Data to see results."
            )

        st.session_state.processing_animation = False
        st.session_state.show_celebration = True
        st.session_state.processing_just_finished = True
        st.rerun()

# ---------------------------------------------------------------------------
# Celebration — balloons
# ---------------------------------------------------------------------------
if st.session_state.show_celebration:
    st.balloons()
    st.session_state.show_celebration = False

# ---------------------------------------------------------------------------
# Batch status message
# ---------------------------------------------------------------------------
if st.session_state.batch_status:
    st.success(st.session_state.batch_status)
    st.session_state.batch_status = ""

# ---------------------------------------------------------------------------
# Processing complete message
# ---------------------------------------------------------------------------
if st.session_state.processing_just_finished:
    progress = _get_current_progress()
    _total = progress["total"]
    _done = progress["completed"] + progress["failed"]
    if _total > 0:
        st.success(f"All {_total} invoices processed. {_done} completed — results ready for review.")
    else:
        st.success("All invoices processed. Results ready for review.")
    st.session_state.processing_just_finished = False

# ---------------------------------------------------------------------------
# Fallback pending notification — persistent reminder when files await local worker
# ---------------------------------------------------------------------------
_prog_check = _get_current_progress()
if _prog_check["queued"] > 0:
    st.info(
        f"**{_prog_check['queued']} file(s)** are pending local fallback processing. "
        f"Run `lworker.py` on your local machine, then click **Refresh Data** to see results."
    )

# ---------------------------------------------------------------------------
# Metrics cards — source of truth per card:
#   Pending     = total - completed - failed (files not yet done)
#   Completed   = unique source_files in output.csv for this batch (cloud + fallback)
#   Fallback Queue = Supabase invoices_queue WHERE status='fallback_pending' AND batch_id=X
# ---------------------------------------------------------------------------
_m_prog = _get_current_progress()
_m_pending = _m_prog["remaining"]
_m_completed = _m_prog["completed"]
_m_queued = _m_prog["queued"]

st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)

mc1, mc2, mc3 = st.columns(3, gap="medium")
with mc1:
    st.markdown(f"""
    <div class="metric-card metric-pending">
        <div class="metric-label">Pending</div>
        <div class="metric-value">{_m_pending}</div>
        <div class="metric-note">awaiting extraction</div>
    </div>
    """, unsafe_allow_html=True)
with mc2:
    st.markdown(f"""
    <div class="metric-card metric-completed">
        <div class="metric-label">Completed</div>
        <div class="metric-value">{_m_completed}</div>
        <div class="metric-note">files processed</div>
    </div>
    """, unsafe_allow_html=True)
with mc3:
    st.markdown(f"""
    <div class="metric-card metric-queued">
        <div class="metric-label">Fallback Queue</div>
        <div class="metric-value">{_m_queued}</div>
        <div class="metric-note">awaiting local worker</div>
    </div>
    """, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Review & Export – Editable Data Table (Card)
# ---------------------------------------------------------------------------
active_batch_id = st.session_state.active_batch_id

if OUTPUT_CSV.exists():
    full_df = pd.read_csv(OUTPUT_CSV)
    if "batch_id" in full_df.columns:
        raw_df = full_df[full_df["batch_id"] == active_batch_id].copy()
    else:
        raw_df = pd.DataFrame(columns=NEW_BATCH_COLUMNS)
else:
    raw_df = pd.DataFrame(columns=NEW_BATCH_COLUMNS)

if st.session_state.edited_df.empty and not raw_df.empty:
    st.session_state.edited_df = raw_df.copy()

_row_count = len(st.session_state.edited_df)

st.markdown('<div class="section-divider"></div>', unsafe_allow_html=True)
st.markdown('<div class="workspace-card">', unsafe_allow_html=True)
st.markdown(f"""
<div class="workspace-header">
  <div>
    <div class="workspace-title">Extracted Data</div>
    <div class="workspace-meta">Batch {active_batch_id} &middot; {_row_count} row{"s" if _row_count != 1 else ""}</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Toolbar row ──
tb1, tb2, tb3, tb4 = st.columns([1, 1, 1, 1])
with tb1:
    refresh_clicked = st.button("Refresh Data", key="refresh_btn", width="stretch")
with tb2:
    pass
with tb3:
    pass
with tb4:
    pass

if refresh_clicked:
    if OUTPUT_CSV.exists():
        full_df_r = pd.read_csv(OUTPUT_CSV)
        if "batch_id" in full_df_r.columns:
            st.session_state.edited_df = full_df_r[full_df_r["batch_id"] == active_batch_id].copy()
        else:
            st.session_state.edited_df = pd.DataFrame(columns=NEW_BATCH_COLUMNS)
    else:
        st.session_state.edited_df = pd.DataFrame(columns=NEW_BATCH_COLUMNS)
    st.rerun()

if st.session_state.edited_df.empty:
    st.info("No extracted data yet. Upload invoices and click **Process Files** to start extraction.")
else:
    editor_key = f"review_editor_{active_batch_id}"
    edited = st.data_editor(
        st.session_state.edited_df,
        width="stretch",
        num_rows="dynamic",
        key=editor_key,
    )

    edited["quantity"] = pd.to_numeric(edited["quantity"], errors="coerce").fillna(0)
    edited["unit_price"] = pd.to_numeric(edited["unit_price"], errors="coerce").fillna(0)
    new_total = (edited["quantity"] * edited["unit_price"]).round(2)

    recalc_needed = not new_total.equals(edited["total_amount"])
    edited["total_amount"] = new_total
    st.session_state.edited_df = edited

    if recalc_needed:
        st.rerun()

    # ── Delete rows ──
    with tb2:
        row_labels = {
            i: f"Row {i}: {str(r.get('seller',''))[:20]} | {str(r.get('item',''))[:20]}"
            for i, r in st.session_state.edited_df.iterrows()
        }
        rows_to_delete = st.multiselect(
            "Delete rows",
            options=list(st.session_state.edited_df.index),
            format_func=lambda i: row_labels.get(i, ""),
            key="delete_rows_sel",
            label_visibility="collapsed",
            placeholder="Select rows…",
        )
        if rows_to_delete and st.button("Delete Rows", key="delete_btn", type="secondary", width="stretch"):
            st.session_state.edited_df = (
                st.session_state.edited_df.drop(rows_to_delete).reset_index(drop=True)
            )
            if "delete_rows_sel" in st.session_state:
                del st.session_state["delete_rows_sel"]
            st.rerun()

    # ── Export ──
    export_df = st.session_state.edited_df.drop(
        columns=["processing_time_seconds", "model_used", "parse_status"],
        errors="ignore",
    )
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        export_df.to_excel(writer, index=False, sheet_name="Invoices")
    excel_data = buf.getvalue()

    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"extracted_invoices_{now}.xlsx"

    with tb3:
        st.download_button(
            label="Export Excel",
            data=excel_data,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

    # ── Email ──
    with tb4:
        with st.popover("Send Email", width="stretch"):
            email_recipient = st.text_input(
                "Recipient",
                key="email_recipient",
                placeholder="user@example.com",
                label_visibility="collapsed",
            )
            if st.button("Send", key="send_email_btn", width="stretch"):
                if not email_recipient or "@" not in email_recipient:
                    st.error("Enter a valid email address.")
                elif not get_secret("SMTP_HOST"):
                    st.error("SMTP not configured.")
                else:
                    with st.spinner("Sending…"):
                        result = send_email_with_attachment(
                            recipient=email_recipient,
                            subject=f"Invoice Extractor — {filename}",
                            body=f"Attached: {len(export_df)} row(s) from Batch {active_batch_id}.",
                            attachment_bytes=excel_data,
                            attachment_filename=filename,
                        )
                    if result == "ok":
                        st.success(f"Sent to {email_recipient}")
                    else:
                        st.error(result)

st.markdown('</div>', unsafe_allow_html=True)  # close workspace-card
