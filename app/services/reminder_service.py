from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, List, Optional
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from sqlalchemy import select, func

from app.services.accounting import member_balances
from app.db.models import Member, ReminderLog

REMINDER_CHANNELS = ("EMAIL", "SMS", "WHATSAPP")
PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")


@dataclass
class ReminderPolicy:
    owner_name: str = "Justine"
    min_balance: float = 10.0
    cooldown_days: int = 7
    cooldown_minutes: int | None = None


@dataclass
class ReminderCandidate:
    member_id: int
    member: str
    channel: str
    recipient: str
    email: str
    phone: str
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


def _looks_like_phone(s: str) -> bool:
    return bool(PHONE_RE.match((s or "").strip()))


def normalize_reminder_channels(channels: Iterable[str] | None = None) -> list[str]:
    if channels is None:
        return ["EMAIL"]

    out = []
    for channel in channels:
        value = str(channel or "").strip().upper()
        if value in REMINDER_CHANNELS and value not in out:
            out.append(value)
    return out or ["EMAIL"]


def last_reminder_map(db: Session, channels: Iterable[str] | None = None) -> dict[tuple[int, str], datetime]:
    selected_channels = normalize_reminder_channels(channels)
    rows = db.execute(
        select(ReminderLog.member_id, ReminderLog.channel, func.max(ReminderLog.created_at))
        .where(ReminderLog.channel.in_(selected_channels))
        .group_by(ReminderLog.member_id, ReminderLog.channel)
    ).all()
    return {
        (int(member_id), str(channel or "EMAIL").upper()): last_at
        for member_id, channel, last_at in rows
        if member_id is not None and last_at is not None
    }


def compute_reminder_candidates(
    db: Session,
    policy: ReminderPolicy,
    channels: Iterable[str] | None = None,
) -> List[ReminderCandidate]:
    selected_channels = normalize_reminder_channels(channels)
    balances = member_balances(db)

    member_rows = db.execute(
        select(
            Member.id,
            Member.email,
            Member.phone,
            Member.name,
            Member.email_enabled,
            Member.sms_enabled,
            Member.whatsapp_enabled,
        )
    ).all()
    contact_map = {
        int(mid): {
            "email": str(email or "").strip(),
            "phone": str(phone or "").strip(),
            "name": str(name or "").strip(),
            "email_enabled": bool(email_enabled),
            "sms_enabled": bool(sms_enabled),
            "whatsapp_enabled": bool(whatsapp_enabled),
        }
        for (mid, email, phone, name, email_enabled, sms_enabled, whatsapp_enabled) in member_rows
    }

    last_map = last_reminder_map(db, selected_channels)
    now = datetime.now()
    cooldown = (
        timedelta(minutes=policy.cooldown_minutes)
        if policy.cooldown_minutes is not None
        else timedelta(days=policy.cooldown_days)
    )
    cooldown_label = (
        f"{policy.cooldown_minutes}m"
        if policy.cooldown_minutes is not None
        else f"{policy.cooldown_days}d"
    )

    out: List[ReminderCandidate] = []

    for b in balances:
        member_id = int(b["member_id"])
        name = str(b["member"] or "").strip()
        balance = float(b["balance"] or 0.0)

        info = contact_map.get(
            member_id,
            {
                "email": "",
                "phone": "",
                "name": "",
                "email_enabled": False,
                "sms_enabled": False,
                "whatsapp_enabled": False,
            },
        )
        email = str(info["email"])
        phone = str(info["phone"])

        # Skip junk names
        if not name or name.lower() == "nan":
            continue

        for channel in selected_channels:
            last_at = last_map.get((member_id, channel))
            recipient = email if channel == "EMAIL" else phone
            base_args = {
                "member_id": member_id,
                "member": name,
                "channel": channel,
                "recipient": recipient,
                "email": email,
                "phone": phone,
                "balance": balance,
                "last_reminder_at": last_at,
            }

            # Owner excluded
            if name.lower() == policy.owner_name.strip().lower():
                out.append(ReminderCandidate(**base_args, eligible=False, reason="Owner excluded"))
                continue

            # Nothing owed
            if balance <= 0:
                out.append(ReminderCandidate(**base_args, eligible=False, reason="Nothing owed"))
                continue

            # Threshold
            if balance < policy.min_balance:
                out.append(ReminderCandidate(**base_args, eligible=False, reason=f"Below min (${policy.min_balance:.2f})"))
                continue

            if channel == "EMAIL":
                if not bool(info["email_enabled"]):
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="Email disabled"))
                    continue
                if not email:
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="Missing email"))
                    continue
                if not _looks_like_email(email):
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="Invalid email format"))
                    continue
            else:
                if channel == "SMS" and not bool(info["sms_enabled"]):
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="SMS disabled"))
                    continue
                if channel == "WHATSAPP" and not bool(info["whatsapp_enabled"]):
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="WhatsApp disabled"))
                    continue
                if not phone:
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="Missing phone"))
                    continue
                if not _looks_like_phone(phone):
                    out.append(ReminderCandidate(**base_args, eligible=False, reason="Phone must be E.164, e.g. +15551234567"))
                    continue

            # Cooldown
            if last_at and (now - last_at) < cooldown:
                out.append(ReminderCandidate(**base_args, eligible=False, reason=f"Cooldown ({cooldown_label})"))
                continue

            out.append(ReminderCandidate(**base_args, eligible=True, reason="Eligible"))

    # Eligible first, then biggest balances
    out.sort(key=lambda x: (not x.eligible, x.channel, -x.balance, x.member.lower()))
    return out


def get_eligible_reminder_candidates(
    db: Session,
    policy: ReminderPolicy,
    channels: Iterable[str] | None = None,
) -> List[ReminderCandidate]:
    return [c for c in compute_reminder_candidates(db, policy, channels) if c.eligible]

def build_reminder_message(member_name: str, balance: float) -> tuple[str, str, str]:
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


def build_reminder_email(member_name: str, balance: float) -> tuple[str, str, str]:
    return build_reminder_message(member_name, balance)
