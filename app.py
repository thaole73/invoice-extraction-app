from datetime import datetime
import base64
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
import streamlit as st
from pathlib import Path
import pandas as pd
import httpx
import io

import config
import db_buffer

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
    df = pd.read_csv(billing_csv) if billing_csv.exists() else pd.DataFrame(columns=BILLING_CSV_COLUMNS)
    df.loc[len(df)] = row
    df.to_csv(billing_csv, index=False)


def _read_batch_id() -> int:
    if BATCH_ID_FILE.exists():
        try:
            return int(BATCH_ID_FILE.read_text().strip())
        except (ValueError, TypeError):
            pass
    return 0


st.set_page_config(page_title="Invoice Extractor", layout="wide")

st.markdown("""
<style>
    /* ── Global resets ── */
    .block-container { padding-top: 1.5rem; padding-bottom: 1rem; }

    /* ── Card shell ── */
    .dash-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 1.2rem 1.4rem;
        margin-bottom: 0.8rem;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .dash-card-header {
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: #64748b;
        margin-bottom: 0.4rem;
    }

    /* ── Metrics cards ── */
    .metric-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 1rem 1.2rem;
        text-align: center;
        box-shadow: 0 1px 3px rgba(0,0,0,.04);
    }
    .metric-card .metric-value {
        font-size: 1.8rem;
        font-weight: 700;
        line-height: 1.1;
        margin: 0.2rem 0;
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
    .metric-pending  { border-left: 4px solid #f59e0b; }
    .metric-completed { border-left: 4px solid #22c55e; }
    .metric-failed    { border-left: 4px solid #ef4444; }
    .metric-pending  .metric-value { color: #d97706; }
    .metric-completed .metric-value { color: #16a34a; }
    .metric-failed    .metric-value { color: #dc2626; }

    /* ── Header badge chips ── */
    .badge {
        display: inline-block;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 0.2rem 0.6rem;
        border-radius: 999px;
        background: #f1f5f9;
        color: #475569;
        margin-right: 0.4rem;
    }
    .badge-accent {
        background: #eff6ff;
        color: #2563eb;
    }

    /* ── Upload dropzone emphasis ── */
    .upload-card {
        border: 2px dashed #cbd5e1;
        border-radius: 10px;
        padding: 1.6rem;
        text-align: center;
        background: #f8fafc;
        transition: border-color 0.15s;
    }
    .upload-card:hover { border-color: #93c5fd; }

    /* ── Section spacing helper ── */
    .section-gap { margin-top: 0.4rem; }

    /* ── Sidebar compactness ── */
    [data-testid="stSidebar"] { padding-top: 1rem; }
    [data-testid="stSidebar"] .stButton button { width: 100%; }
</style>
""", unsafe_allow_html=True)

def _time_greeting() -> str:
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"

_pending = len(list(PENDING.iterdir()))
_completed = len(list(COMPLETED.iterdir())) if COMPLETED.exists() else 0
_failed = len(list(FAILED.iterdir())) if FAILED.exists() else 0
_batch = _read_batch_id()

_badge_failed = f'<span class="badge" style="background:#fee2e2;color:#991b1b;">{_failed} failed</span>' if _failed else ""

st.markdown(f"""
<div class="dash-card" style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap;">
  <div>
    <div style="font-size:1.35rem; font-weight:700; color:#1e293b; margin-bottom:0.1rem;">Invoice Extraction App</div>
    <div style="font-size:0.82rem; color:#64748b;">{_time_greeting()} &mdash; upload invoices, review extracted rows, export clean results.</div>
  </div>
  <div style="margin-top:0.4rem;">
    <span class="badge badge-accent">Batch {_batch}</span>
    <span class="badge" style="background:#fef3c7;color:#92400e;">{_pending} pending</span>
    <span class="badge" style="background:#dcfce7;color:#166534;">{_completed} completed</span>
    {_badge_failed}
  </div>
</div>
""", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Session state initialisation
# ---------------------------------------------------------------------------

if "trigger_balloons" not in st.session_state:
    st.session_state.trigger_balloons = False

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

if "processing_active" not in st.session_state:
    st.session_state.processing_active = False

if "processing_just_finished" not in st.session_state:
    st.session_state.processing_just_finished = False

if "cloud_results" not in st.session_state:
    st.session_state.cloud_results = []

if not BATCH_ID_FILE.exists():
    BATCH_ID_FILE.write_text(str(st.session_state.active_batch_id))

# ---------------------------------------------------------------------------
# Sidebar – Compact utility
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        f"""<div style="padding:0.6rem 0;">
        <div style="font-size:0.72rem; font-weight:600; text-transform:uppercase;
            letter-spacing:0.05em; color:#94a3b8; margin-bottom:0.3rem;">Batch Controls</div>
        <div style="font-size:0.82rem; color:#475569;">
            Active batch: <strong>{st.session_state.active_batch_id}</strong>
        </div>
        </div>""",
        unsafe_allow_html=True,
    )

    if st.button("Start New Batch", type="primary", width="stretch"):
        st.session_state.uploader_key_counter += 1
        st.session_state.processed_files = set()
        st.session_state.edited_df = pd.DataFrame()
        st.session_state.cloud_results = []
        st.session_state.batch_status = "New batch started — upload files above."
        if "batch_pending_start" in st.session_state:
            del st.session_state["batch_pending_start"]
        new_batch_id = st.session_state.active_batch_id + 1
        st.session_state.active_batch_id = new_batch_id
        BATCH_ID_FILE.write_text(str(new_batch_id))
        st.cache_data.clear()
        st.rerun()

    st.caption("Archives preserved across batches.")

    st.markdown("---")
    st.caption("Invoice Extractor v1.0")


# ---------------------------------------------------------------------------
# Cloud-first extraction (Gemini API) with fallback
# ---------------------------------------------------------------------------
def _extract_cloud(filepath: Path) -> tuple[list[dict], str, dict]:
    """Try Gemini API extraction. Returns (rows, model_used, usage_info) or raises."""
    with open(filepath, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": config.API_MODEL,
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
        "Authorization": f"Bearer {config.API_KEY}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{config.API_BASE_URL}/chat/completions", json=payload, headers=headers)
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

    model_label = f"api:{config.API_MODEL}"
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
    if not config.SMTP_HOST:
        return "error: SMTP_HOST is not configured."
    if not config.SMTP_USER or not config.SMTP_PASS:
        return "error: SMTP credentials (SMTP_USER, SMTP_PASS) are not configured."
    if not config.SMTP_FROM:
        return "error: SMTP_FROM sender address is not configured."

    msg = MIMEMultipart()
    msg["From"] = config.SMTP_FROM
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
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.ehlo()
            if config.SMTP_PORT != 465:
                server.starttls()
                server.ehlo()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.SMTP_FROM, [recipient], msg.as_string())
        return "ok"
    except smtplib.SMTPAuthenticationError:
        return "error: SMTP authentication failed — check SMTP_USER and SMTP_PASS."
    except smtplib.SMTPConnectError:
        return f"error: Could not connect to {config.SMTP_HOST}:{config.SMTP_PORT}."
    except smtplib.SMTPException as exc:
        return f"error: SMTP error — {exc}"
    except OSError as exc:
        return f"error: Network error — {exc}"


# ---------------------------------------------------------------------------
# Upload area
# ---------------------------------------------------------------------------
st.markdown('<div class="dash-card">', unsafe_allow_html=True)
st.markdown('<div class="dash-card-header">Upload Invoices</div>', unsafe_allow_html=True)
st.caption("Drop JPEG or PNG invoice images below. Files are sent to the worker for extraction automatically.")

def on_upload_change():
    if st.session_state.get(f"uploader_{st.session_state.uploader_key_counter}"):
        st.session_state.trigger_balloons = True

uploader_key = f"uploader_{st.session_state.uploader_key_counter}"

uploaded_files = st.file_uploader(
    "Choose JPEG or PNG files",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True,
    key=uploader_key,
    on_change=on_upload_change,
    label_visibility="collapsed",
)

st.markdown('</div>', unsafe_allow_html=True)

if st.session_state.trigger_balloons:
    st.snow()
    st.session_state.trigger_balloons = False

if st.session_state.batch_status:
    st.success(st.session_state.batch_status)
    st.session_state.batch_status = ""

if uploaded_files:
    saved = []
    for f in uploaded_files:
        if f.name in st.session_state.processed_files:
            continue
        dest = PENDING / f.name
        with open(dest, "wb") as buf:
            buf.write(f.getbuffer())
        saved.append(f.name)
        st.session_state.processed_files.add(f.name)

    if saved:
        st.success(f"Saved {len(saved)} file(s) to `{PENDING}/`")

        batch_id = st.session_state.active_batch_id
        cloud_ok = 0
        cloud_fallback = 0
        for name in saved:
            filepath = PENDING / name
            try:
                t0 = time.perf_counter()
                rows, model_label, usage_info = _extract_cloud(filepath)
                elapsed = round(time.perf_counter() - t0, 3)
                for r in rows:
                    r["batch_id"] = batch_id
                    r["processing_time_seconds"] = elapsed
                st.session_state.cloud_results.extend(rows)
                df = pd.read_csv(OUTPUT_CSV) if OUTPUT_CSV.exists() else pd.DataFrame(columns=NEW_BATCH_COLUMNS)
                for r in rows:
                    df.loc[len(df)] = r
                df.to_csv(OUTPUT_CSV, index=False)
                try:
                    db_buffer.log_billing(
                        batch_id=batch_id,
                        model_used=model_label,
                        tokens_in=usage_info.get("tokens_in", 0),
                        tokens_out=usage_info.get("tokens_out", 0),
                        processing_time=elapsed,
                        filename=name,
                        status="success",
                    )
                except Exception as billing_exc:
                    st.warning(f"Billing log write failed: {billing_exc}")
                _save_billing_local(
                    batch_id=batch_id,
                    filename=name,
                    status="success",
                    model_used=model_label,
                    tokens_in=usage_info.get("tokens_in", 0),
                    tokens_out=usage_info.get("tokens_out", 0),
                    processing_time=elapsed,
                )
                cloud_ok += 1
            except Exception as exc:
                cloud_fallback += 1
                file_bytes = filepath.read_bytes()
                db_buffer.push_to_queue(name, file_bytes, batch_id)
                try:
                    db_buffer.log_billing(
                        batch_id=batch_id,
                        model_used="fallback_queue",
                        tokens_in=0,
                        tokens_out=0,
                        processing_time=0.0,
                        filename=name,
                        status="queued",
                    )
                except Exception as billing_exc:
                    pass
                _save_billing_local(
                    batch_id=batch_id,
                    filename=name,
                    status="queued",
                    model_used="fallback_queue",
                    tokens_in=0,
                    tokens_out=0,
                    processing_time=0.0,
                )
                st.warning(f"Cloud extraction failed for {name}.... ☁️🌧️ but don't worry! I've queued it for local processing 🧸🧸🧸. Error: {exc}.")

        if cloud_ok > 0:
            st.success(f"Cloud extraction succeeded for {cloud_ok} file(s) — results ready for review.")
        if cloud_fallback > 0:
            st.info(f"{cloud_fallback} file(s) queued to cloud buffer for local fallback processing.")
        for name in saved:
            st.write(f"- {name}")
        st.session_state.processing_active = True
        st.session_state.processing_just_finished = False
        st.rerun()

# ---------------------------------------------------------------------------
# Processing Progress Bar
# ---------------------------------------------------------------------------
if st.session_state.processing_active:
    import time as _time

    status_container = st.status("Processing invoices...", expanded=True)
    progress_bar = st.progress(0, text="Starting...")

    if "batch_pending_start" not in st.session_state:
        st.session_state.batch_pending_start = len(list(PENDING.iterdir()))

    pending_count = len(list(PENDING.iterdir()))
    start_count = max(st.session_state.batch_pending_start, 1)
    completed_count = max(start_count - pending_count, 0)
    pct = min(completed_count / start_count, 1.0)
    progress_bar.progress(pct, text=f"Processing... {int(pct * 100)}%")
    status_container.update(label=f"Processing invoices... {int(pct * 100)}%")

    while pending_count > 0 and pct < 1.0:
        _time.sleep(2)
        pending_count = len(list(PENDING.iterdir()))
        completed_count = max(start_count - pending_count, 0)
        pct = min(completed_count / start_count, 1.0)

        if pct < 0.3:
            msg = "🧸 Extracting fields..."
        elif pct < 0.6:
            msg = "🔢 Processing documents..."
        elif pct < 0.9:
            msg = "🎀 Almost done..."
        else:
            msg = "✨ 🚀 Finalizing..."

        progress_bar.progress(pct, text=f"{msg} {int(pct * 100)}%")
        status_container.update(label=f"Processing invoices... {int(pct * 100)}%")

    progress_bar.progress(1.0, text="Done.")
    status_container.update(label="All invoices processed.", state="complete")
    st.session_state.processing_active = False
    st.session_state.processing_just_finished = True
    st.rerun()

if st.session_state.processing_just_finished:
    st.balloons()
    st.success("All invoices processed. Results ready for review.")
    st.session_state.processing_just_finished = False

# ---------------------------------------------------------------------------
# Metrics cards
# ---------------------------------------------------------------------------
_m_pending = len(list(PENDING.iterdir()))
_m_completed = len(list(COMPLETED.iterdir())) if COMPLETED.exists() else 0
_m_failed = len(list(FAILED.iterdir())) if FAILED.exists() else 0

mc1, mc2, mc3 = st.columns(3)
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
    <div class="metric-card metric-failed">
        <div class="metric-label">Failed</div>
        <div class="metric-value">{_m_failed}</div>
        <div class="metric-note">needs attention</div>
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

st.markdown(f"""
<div class="dash-card">
  <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap;">
    <div>
      <div class="dash-card-header">Extracted Data</div>
      <div style="font-size:0.82rem; color:#64748b;">
        Batch {active_batch_id} &middot; {_row_count} row{"s" if _row_count != 1 else ""}
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Toolbar row ──
tb1, tb2, tb3, tb4 = st.columns([1, 1, 1, 1])
with tb1:
    refresh_clicked = st.button("Refresh Data", key="refresh_btn", use_container_width=True)
with tb2:
    pass  # placeholder for delete (populated inside the data block)
with tb3:
    pass  # placeholder for export (populated inside the data block)
with tb4:
    pass  # placeholder for email (populated inside the data block)

if refresh_clicked:
    st.session_state.edited_df = raw_df.copy()
    st.rerun()

if st.session_state.edited_df.empty:
    st.info("No extracted data yet. Upload invoices and wait for the worker to process them.")
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
        if rows_to_delete and st.button("Delete Rows", key="delete_btn", type="secondary", use_container_width=True):
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
            use_container_width=True,
        )

    # ── Email ──
    with tb4:
        with st.popover("Send Email", use_container_width=True):
            email_recipient = st.text_input(
                "Recipient",
                key="email_recipient",
                placeholder="user@example.com",
                label_visibility="collapsed",
            )
            if st.button("Send", key="send_email_btn", use_container_width=True):
                if not email_recipient or "@" not in email_recipient:
                    st.error("Enter a valid email address.")
                elif not config.SMTP_HOST:
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
