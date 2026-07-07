from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, UniqueConstraint,Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base

class Member(Base):
    __tablename__ = "members"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    is_active = Column(Integer, default=1, nullable=False)  # 1/0
    email_enabled = Column(Boolean, default=True, nullable=False)
    sms_enabled = Column(Boolean, default=False, nullable=False)
    whatsapp_enabled = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    phone_last4 = Column(String, nullable=True)

    # Who added this member (a MEMBER-role plan owner adding a new person to
    # their plan). NULL means "not attributable" (legacy data, or created by
    # the application OWNER) - only the application OWNER can edit those.
    created_by_member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    created_by_member = relationship("Member", remote_side=[id])


class Plan(Base):
    """
    A billing plan/group: a set of members sharing recurring invoices (e.g.
    a family mobile plan). Supports scaling the app to more than one
    bill/plan at once - see docs/decisions/2026-07-04-roadmap/04-multi-plan-schema.md.
    """

    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    carrier_type = Column(String, nullable=True)  # e.g. "T-Mobile", drives which LLM prompt/anchors to use
    owner_member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    owner_member = relationship("Member", foreign_keys=[owner_member_id])
    invoices = relationship("Invoice", back_populates="plan")


class PlanMember(Base):
    """Which members belong to which plan. A member can belong to multiple plans."""

    __tablename__ = "plan_members"
    __table_args__ = (UniqueConstraint("plan_id", "member_id", name="uq_plan_member"),)

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("Plan")
    member = relationship("Member")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("plan_id", "year", "month", name="uq_invoice_plan_year_month"),)

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(String, nullable=False)  # "Jan", "Feb", ...
    total_amount = Column(Float, default=0.0, nullable=False)
    due_date = Column(Date, nullable=True)
    pdf_path = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("Plan", back_populates="invoices")
    allocations = relationship("Allocation", back_populates="invoice", cascade="all, delete-orphan")


class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (UniqueConstraint("invoice_id", "member_id", name="uq_allocation_invoice_member"),)

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    amount_due = Column(Float, default=0.0, nullable=False)

    invoice = relationship("Invoice", back_populates="allocations")
    member = relationship("Member")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    date = Column(Date, nullable=False)
    amount = Column(Float, nullable=False)

    # If member_id is NULL, treat it as a "system/outbound/other" payment.
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)

    # INBOUND = member -> you, OUTBOUND = you -> carrier/other
    direction = Column(String, nullable=False)  # "INBOUND" | "OUTBOUND"
    description = Column(String, nullable=True)

    # Optional link to invoice
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    plan = relationship("Plan")
    member = relationship("Member")
    invoice = relationship("Invoice")


class ReminderLog(Base):
    __tablename__ = "reminder_logs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    channel = Column(String, default="EMAIL", nullable=False)  # EMAIL | SMS | WHATSAPP
    recipient = Column(String, nullable=True)
    sender = Column(String, nullable=True)

    # Kept for compatibility with existing email reminder code/log views.
    email = Column(String, nullable=True)

    amount = Column(Float, nullable=False)  # outstanding at time of send
    subject = Column(String, nullable=True)
    body = Column(String, nullable=False)

    provider = Column(String, nullable=True)  # SMTP | TWILIO
    provider_message_id = Column(String, nullable=True)
    provider_status = Column(String, nullable=True)

    success = Column(Integer, default=1, nullable=False)  # 1/0
    error = Column(String, nullable=True)
    error_code = Column(String, nullable=True)
    status = Column(String, nullable=True)

    member = relationship("Member")


class PaymentApplication(Base):
    __tablename__ = "payment_applications"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)

    amount_applied = Column(Float, nullable=False)

    payment = relationship("Payment")
    invoice = relationship("Invoice")
    member = relationship("Member")

class BillImportJob(Base):
    """
    A single run of the Bill Import v2 (RAG, opt-in) pipeline. This is the
    audit trail/golden dataset for the new pipeline - the legacy synchronous
    import flow in app/ui/bill_import.py never writes here.

    No PDF is ever stored - only the small cleaned text (a few KB) survives,
    keyed by content_hash so a repeat upload of the same bill short-circuits
    to the cached result with zero LLM/embedding calls.
    """

    __tablename__ = "bill_import_jobs"
    __table_args__ = (UniqueConstraint("plan_id", "content_hash", name="uq_bill_import_job_plan_hash"),)

    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=False)
    uploaded_by_member_id = Column(Integer, ForeignKey("members.id"), nullable=True)

    content_hash = Column(String, nullable=False)
    cleaned_text = Column(String, nullable=False)

    selected_chunks_json = Column(String, nullable=True)
    precedent_used_json = Column(String, nullable=True)
    llm_raw_response = Column(String, nullable=True)
    proposal_json = Column(String, nullable=True)

    status = Column(String, default="PENDING", nullable=False)  # PENDING|PROCESSING|DONE|FAILED
    error = Column(String, nullable=True)

    mode = Column(String, nullable=True)  # NORMAL|EVALUATE_ONLY, set once processed
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    diff_json = Column(String, nullable=True)  # populated only in EVALUATE_ONLY mode

    # Observability - lets the "Inspect a job" UI and the admin Eval
    # Dashboard show exactly what was sent to the LLM and what it cost.
    system_prompt = Column(String, nullable=True)
    known_roster_json = Column(String, nullable=True)
    token_usage_json = Column(String, nullable=True)
    cache_hit_count = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    plan = relationship("Plan")
    uploaded_by_member = relationship("Member")
    invoice = relationship("Invoice")


class MemberIdentifier(Base):
    """
    Generalized member-matching identifier for the Bill Import v2 pipeline -
    not just phone numbers. A bill line might only carry an email, a name,
    or an account-holder field instead of a phone number (as T-Mobile bills
    have). This is additive alongside (and backfilled from) the legacy
    Member.phone_last4 column, which stays untouched for the legacy pipeline.

    plan_id is nullable: an identifier can be scoped to one plan (e.g. a
    phone number tied to a specific line) or left global across all of a
    member's plans (e.g. an email, usually stable).
    """

    __tablename__ = "member_identifiers"

    id = Column(Integer, primary_key=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True)
    identifier_type = Column(String, nullable=False)  # PHONE_LAST4|EMAIL|NAME|ACCOUNT
    identifier_value = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    member = relationship("Member")
    plan = relationship("Plan")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="OWNER", nullable=False)   # OWNER for now
    is_active = Column(Boolean, default=True, nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    invite_sent_at = Column(DateTime(timezone=True), nullable=True)
    password_reset_token = Column(String, nullable=True)
    password_reset_expires_at = Column(DateTime(timezone=True), nullable=True)
    password_reset_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    member = relationship("Member")
