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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    phone_last4 = Column(String, nullable=True)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("year", "month", name="uq_invoice_year_month"),)

    id = Column(Integer, primary_key=True)
    year = Column(Integer, nullable=False)
    month = Column(String, nullable=False)  # "Jan", "Feb", ...
    total_amount = Column(Float, default=0.0, nullable=False)
    due_date = Column(Date, nullable=True)
    pdf_path = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

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

    member = relationship("Member")
    invoice = relationship("Invoice")


class ReminderLog(Base):
    __tablename__ = "reminder_logs"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    email = Column(String, nullable=False)

    amount = Column(Float, nullable=False)  # outstanding at time of send
    subject = Column(String, nullable=False)
    body = Column(String, nullable=False)

    success = Column(Integer, default=1, nullable=False)  # 1/0
    error = Column(String, nullable=True)
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

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="OWNER", nullable=False)   # OWNER for now
    is_active = Column(Boolean, default=True, nullable=False)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    member = relationship("Member")