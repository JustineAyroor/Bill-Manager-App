from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.services.accounting import member_balances
from app.db.models import Member, ReminderLog


@dataclass
class ReminderPolicy:
    owner_name: str = "Justine"
    min_balance: float = 10.0
    cooldown_days: int = 7


@dataclass
class ReminderCandidate:
    member_id: int
    member: str
    email: str
    balance: float
    last_reminder_at: Optional[datetime]
    eligible: bool
    reason: str


def _looks_like_email(s: str) -> bool:
    s = (s or "").strip()
    if "@" not in s:
        return False
    local, _, domain = s.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return True


def last_reminder_map(db: Session) -> dict[int, datetime]:
    rows = db.execute(
        select(ReminderLog.member_id, func.max(ReminderLog.created_at))
        .group_by(ReminderLog.member_id)
    ).all()
    return {int(r[0]): r[1] for r in rows if r[0] is not None and r[1] is not None}


def compute_reminder_candidates(db: Session, policy: ReminderPolicy) -> List[ReminderCandidate]:
    balances = member_balances(db)

    member_rows = db.execute(select(Member.id, Member.email, Member.name)).all()
    email_map = {int(mid): (str(email or "").strip(), str(name or "").strip()) for (mid, email, name) in member_rows}

    last_map = last_reminder_map(db)
    now = datetime.now()

    out: List[ReminderCandidate] = []

    for b in balances:
        member_id = int(b["member_id"])
        name = str(b["member"] or "").strip()
        balance = float(b["balance"] or 0.0)

        email, _ = email_map.get(member_id, ("", ""))

        # Skip junk names
        if not name or name.lower() == "nan":
            continue

        # Owner excluded
        if name.lower() == policy.owner_name.strip().lower():
            out.append(ReminderCandidate(member_id, name, email, balance, last_map.get(member_id), False, "Owner excluded"))
            continue

        # Nothing owed
        if balance <= 0:
            out.append(ReminderCandidate(member_id, name, email, balance, last_map.get(member_id), False, "Nothing owed"))
            continue

        # Threshold
        if balance < policy.min_balance:
            out.append(ReminderCandidate(member_id, name, email, balance, last_map.get(member_id), False, f"Below min (${policy.min_balance:.2f})"))
            continue

        # Email checks
        if not email:
            out.append(ReminderCandidate(member_id, name, "", balance, last_map.get(member_id), False, "Missing email"))
            continue

        if not _looks_like_email(email):
            out.append(ReminderCandidate(member_id, name, email, balance, last_map.get(member_id), False, "Invalid email format"))
            continue

        # Cooldown
        last_at = last_map.get(member_id)
        if last_at and (now - last_at) < timedelta(days=policy.cooldown_days):
            out.append(ReminderCandidate(member_id, name, email, balance, last_at, False, f"Cooldown ({policy.cooldown_days}d)"))
            continue

        out.append(ReminderCandidate(member_id, name, email, balance, last_at, True, "Eligible"))

    # Eligible first, then biggest balances
    out.sort(key=lambda x: (not x.eligible, -x.balance, x.member.lower()))
    return out


def get_eligible_reminder_candidates(db: Session, policy: ReminderPolicy) -> List[ReminderCandidate]:
    return [c for c in compute_reminder_candidates(db, policy) if c.eligible]

def build_reminder_email(member_name: str, balance: float) -> tuple[str, str, str]:
    subject = "T-Mobile plan payment reminder"

    text = (
        f"Hi {member_name},\n\n"
        f"Reminder: your current outstanding balance is ${balance:.2f} for the T-Mobile plan.\n\n"
        f"Please send the payment when you can.\n\n"
        f"Thanks,\n"
        f"Justine"
    )

    html = f"""
    <div style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#111;">
      <div style="max-width:560px; margin:0 auto; border:1px solid #e6e6e6; border-radius:12px; overflow:hidden;">
        <div style="padding:16px 18px; background:#f7f7f7;">
          <div style="font-size:16px; font-weight:700;">T-Mobile Plan Reminder</div>
          <div style="font-size:12px; color:#666; margin-top:4px;">A quick note about your current balance</div>
        </div>

        <div style="padding:18px;">
          <p style="margin:0 0 10px 0;">Hi <b>{member_name}</b>,</p>

          <p style="margin:0 0 14px 0;">
            This is a friendly reminder that your current outstanding total is:
          </p>

          <div style="display:inline-block; padding:10px 12px; border-radius:10px; background:#fff7ed; border:1px solid #fed7aa;">
            <span style="font-size:12px; color:#9a3412;">Outstanding</span><br/>
            <span style="font-size:22px; font-weight:800; color:#9a3412;">${balance:.2f}</span>
          </div>

          <p style="margin:14px 0 0 0;">
            Please send the payment when you can. If you already paid recently, you can ignore this message.
          </p>

          <p style="margin:18px 0 0 0; color:#666; font-size:12px;">
            Thanks,<br/>
            Justine
          </p>
        </div>
      </div>
    </div>
    """
    return subject, text, html