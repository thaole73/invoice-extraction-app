import os
import base64
from typing import Optional
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

_client: Optional[Client] = None


def get_secret(key):
    """Access secrets from Streamlit Cloud Secrets, fallback to OS env vars."""
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, "")


def _get_client() -> Client:
    global _client
    if _client is None:
        url = get_secret("SUPABASE_URL")
        key = get_secret("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in the environment or Streamlit secrets.")
        _client = create_client(url, key)
    return _client


def push_to_queue(filename: str, file_bytes: bytes, batch_id: int) -> dict:
    sb = _get_client()
    # 1. Convert to bytes if it's currently a string
    if isinstance(file_bytes, str):
        file_bytes = file_bytes.encode('utf-8')
    else:
        file_bytes = file_bytes
    
    file_b64 = base64.b64encode(file_bytes).decode("ascii")
    result = sb.table("invoices_queue").insert({
        "filename": filename,
        "file_bytes": file_b64,
        "status": "pending",
        "batch_id": batch_id,
    }).execute()
    return result.data[0]


def fetch_pending_invoices() -> list[dict]:
    sb = _get_client()
    result = sb.table("invoices_queue") \
        .select("*") \
        .eq("status", "pending") \
        .order("created_at", desc=False) \
        .execute()
    return result.data


def fetch_fallback_pending_jobs() -> list[dict]:
    """Fetch jobs with status 'fallback_pending' for local worker processing."""
    sb = _get_client()
    result = sb.table("invoices_queue") \
        .select("*") \
        .eq("status", "fallback_pending") \
        .order("created_at", desc=False) \
        .execute()
    return result.data


def fetch_pending_count_for_batch(batch_id: int) -> int:
    """Return the count of pending queue items for a specific batch (DB-level filter)."""
    sb = _get_client()
    result = sb.table("invoices_queue") \
        .select("id") \
        .eq("status", "pending") \
        .eq("batch_id", batch_id) \
        .execute()
    return len(result.data)


def mark_processed(invoice_id: str) -> dict:
    sb = _get_client()
    result = sb.table("invoices_queue").update({
        "status": "processed",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", invoice_id).execute()
    return result.data[0] if result.data else {}


def log_billing(
    batch_id: int,
    model_used: str,
    tokens_in: int,
    tokens_out: int,
    processing_time: float,
    filename: str = "",
    status: str = "success",
) -> dict:
    sb = _get_client()
    result = sb.table("billing_logs").insert({
        "batch_id": batch_id,
        "model_used": model_used,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "processing_time": processing_time,
        "filename": filename,
        "status": status,
    }).execute()
    return result.data[0]


def fetch_billing_logs(batch_id: Optional[int] = None, limit: int = 100) -> list[dict]:
    sb = _get_client()
    query = sb.table("billing_logs").select("*").order("created_at", desc=True)
    if batch_id is not None:
        query = query.eq("batch_id", batch_id)
    query = query.limit(limit)
    result = query.execute()
    return result.data


def create_job(filename: str, file_bytes: bytes, batch_id: int) -> dict:
    """Create a job record in the queue with status 'pending'. Returns the created record."""
    return push_to_queue(filename, file_bytes, batch_id)


def update_job_status(job_id: str, status: str) -> dict:
    """Update a job's status. Sets processed_at for terminal statuses."""
    sb = _get_client()
    update_data: dict = {"status": status}
    if status in ("cloud_completed", "fallback_completed", "fallback_failed"):
        update_data["processed_at"] = datetime.now(timezone.utc).isoformat()
    result = sb.table("invoices_queue").update(update_data).eq("id", job_id).execute()
    return result.data[0] if result.data else {}


def fetch_batch_job_summary(batch_id: int) -> dict:
    """Return status counts for a batch, e.g. {'cloud_completed': 3, 'fallback_pending': 2}."""
    sb = _get_client()
    result = sb.table("invoices_queue") \
        .select("status") \
        .eq("batch_id", batch_id) \
        .execute()
    counts: dict = {}
    for row in result.data:
        s = row.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    return counts
