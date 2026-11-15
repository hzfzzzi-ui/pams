"""Account reconciliation API and explicit user confirmations."""

from uuid import uuid4

import psycopg2.extras
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from domain.accounts import account_overview
from domain.accounts.account_overview import get_conn

router = APIRouter()

REVIEW_ACTIONS = {"approve", "defer_missing", "revoke"}


class AccountReviewRequest(BaseModel):
    """User decision for the current account reconciliation state."""

    action: str
    note: str | None = None


@router.get("/accounts/overview")
def accounts_overview():
    """Return account master data and current reconciliation inputs."""
    return account_overview._norm(account_overview._overview_payload())


@router.post("/accounts/{account_code}/review")
def review_account(account_code: str, req: AccountReviewRequest):
    """Persist the user's decision for the current account reconciliation state."""
    action = (req.action or "").strip()
    if action not in REVIEW_ACTIONS:
        raise HTTPException(status_code=400, detail="Unsupported account review action")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('account-import:' + account_code,))
            payload = account_overview._overview_payload(cur)
            account = next((a for a in payload["accounts"] if a["account_code"] == account_code), None)
            if not account:
                raise HTTPException(status_code=404, detail=f"Account {account_code} is not in reconciliation scope")
            status = str(account.get("status") or "")
            primary = account.get("primary_balance") or {}
            if action == "revoke":
                note = (req.note or '').strip()
                if not note:
                    raise HTTPException(status_code=400, detail="Revocation requires a note")
                latest = account_overview._fetch_latest_reviews(cur).get(account_code)
                if not latest or latest.get('revoked_at'):
                    raise HTTPException(status_code=400, detail="No active account review")
                cur.execute("UPDATE evidence_confirmation SET revoked_at=now(),revocation_note=%s WHERE confirmation_id=%s AND revoked_at IS NULL RETURNING confirmation_id",
                            (note, latest['id']))
                if not cur.fetchone():
                    raise HTTPException(status_code=400, detail="No active account review")
            else:
                if action == "approve":
                    if status != "ready_for_review":
                        raise HTTPException(status_code=400, detail="Only ready-for-review accounts can be approved")
                    if primary.get("amount") in (None, ""):
                        raise HTTPException(status_code=400, detail="Account has no balance to approve")
                elif not status.startswith("missing"):
                    raise HTTPException(status_code=400, detail="Only missing-data accounts can be deferred")
                evidence = {
                    "account_code": account_code, "review_action": action, "source_status": status,
                    "review_fingerprint": account_overview._review_fingerprint(account),
                    "reviewed_balance": account_overview._norm(primary.get("amount")),
                    "currency": primary.get("currency") or account.get("base_currency"),
                    "balance_as_of": account_overview._norm(primary.get("as_of")), "reviewed_by": "user",
                }
                cur.execute("""INSERT INTO evidence_confirmation
                    (confirmation_id,confirmation_type,from_account_code,evidence,note,confirmed_at)
                    VALUES (%s,'account_review',%s,%s,%s,clock_timestamp())""",
                    (str(uuid4()), account_code, psycopg2.extras.Json(evidence), req.note or ''))
        conn.commit()

    return account_overview._norm(account_overview._overview_payload())
