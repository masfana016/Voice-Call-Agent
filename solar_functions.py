"""Solar agent tools, stored in Supabase (Postgres) through SQLModel.

lead_id and phone are added by the server, never by the model.
One salesperson, Monday to Friday, 9 to 5 (change the hours in .env).
"""
import os
import smtplib
import threading
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from sqlalchemy import Column, DateTime, Index, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, create_engine, select

load_dotenv()

DB_URI = os.getenv("DB_URI")
if not DB_URI:
    raise ValueError("DB_URI is not set in the .env file")

# echo stays off on purpose: SQL logs would contain names and phone numbers
engine = create_engine(DB_URI, pool_pre_ping=True)

# =========================================================
# SCHEDULE SETTINGS
# =========================================================

TZ_NAME = os.getenv("TIMEZONE", "Asia/Karachi")
LOCAL_TZ = ZoneInfo(TZ_NAME)
WORK_START_HOUR = int(os.getenv("WORK_START_HOUR", "9"))
WORK_END_HOUR = int(os.getenv("WORK_END_HOUR", "17"))
SLOT_MINUTES = int(os.getenv("SLOT_MINUTES", "30"))
MIN_NOTICE_MINUTES = int(os.getenv("MIN_NOTICE_MINUTES", "60"))
WORK_DAYS = {0, 1, 2, 3, 4}  # Monday to Friday
SEARCH_DAYS = 14


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _tz_col(**kwargs):
    return Column(DateTime(timezone=True), **kwargs)


# =========================================================
# MODELS
# =========================================================

class Lead(SQLModel, table=True):
    __tablename__ = "leads"

    id: str = Field(primary_key=True)
    name: str = ""
    phone: str = Field(index=True)
    source: str = "unknown"
    status: str = "calling"  # calling / qualified / appointment_booked / callback / do_not_call
    created_at: datetime = Field(default_factory=_now_utc, sa_column=_tz_col())


class Qualification(SQLModel, table=True):
    __tablename__ = "qualifications"

    lead_id: str = Field(primary_key=True, foreign_key="leads.id")
    owns_property: str = ""
    monthly_bill: str = ""
    roof_type: str = ""
    location: str = ""
    timeline: str = ""
    wants_financing: str = ""
    interest_level: str = ""
    notes: str = ""
    updated_at: datetime = Field(default_factory=_now_utc, sa_column=_tz_col())


class Appointment(SQLModel, table=True):
    __tablename__ = "appointments"
    # One salesperson: two booked appointments can never share a start time
    __table_args__ = (
        Index(
            "uq_appointments_booked_start",
            "start_at",
            unique=True,
            postgresql_where=text("status = 'booked'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: str = Field(foreign_key="leads.id", index=True)
    start_at: datetime = Field(sa_column=_tz_col(nullable=False))
    end_at: datetime = Field(sa_column=_tz_col(nullable=False))
    status: str = "booked"  # booked / cancelled
    notes: str = ""
    created_at: datetime = Field(default_factory=_now_utc, sa_column=_tz_col())


class Callback(SQLModel, table=True):
    __tablename__ = "callbacks"

    id: Optional[int] = Field(default=None, primary_key=True)
    lead_id: str = Field(foreign_key="leads.id", index=True)
    preferred_time: str = ""
    reason: str = ""
    created_at: datetime = Field(default_factory=_now_utc, sa_column=_tz_col())


class DoNotCall(SQLModel, table=True):
    __tablename__ = "do_not_call"

    phone: str = Field(primary_key=True)
    reason: str = ""
    created_at: datetime = Field(default_factory=_now_utc, sa_column=_tz_col())


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


# =========================================================
# NOTIFICATIONS (email and/or SMS to the salesperson)
# =========================================================

def _email_configured() -> bool:
    return all(os.getenv(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO"))


def _sms_configured() -> bool:
    return all(
        os.getenv(k)
        for k in ("NOTIFY_SMS_TO", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    )


def _send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    to = os.getenv("NOTIFY_EMAIL_TO")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("NOTIFY_EMAIL_FROM", user)
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)


def _send_sms(body: str) -> None:
    to = os.getenv("NOTIFY_SMS_TO")
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    sender = os.getenv("TWILIO_PHONE_NUMBER")
    from twilio.rest import Client

    Client(sid, token).messages.create(to=to, from_=sender, body=body[:300])


def _notify_blocking(subject: str, body: str, sms_text: Optional[str] = None) -> None:
    channels = []
    if _email_configured():
        channels.append(("email", lambda: _send_email(subject, body)))
    if _sms_configured():
        channels.append(("sms", lambda: _send_sms(sms_text or f"{subject}\n{body}")))

    if not channels:
        print("Notification skipped: no channel set up. For email set SMTP_USER, "
              "SMTP_PASSWORD and NOTIFY_EMAIL_TO. For SMS set NOTIFY_SMS_TO.")
        return

    for label, send in channels:
        try:
            send()
            print(f"Notification sent by {label}")
        except Exception as e:
            hint = ""
            if label == "email" and isinstance(e, OSError):
                hint = " (cannot reach the mail server: some hosts, including Railway on non-Pro plans, block SMTP)"
            print(f"Notification ({label}) failed: {type(e).__name__}: {e}{hint}")


def notify(subject: str, body: str, sms_text: Optional[str] = None) -> None:
    """Fire and forget, so the caller never waits for an email or SMS to send.
    sms_text is a one-line version for SMS, to keep it to a single message."""
    threading.Thread(target=_notify_blocking, args=(subject, body, sms_text), daemon=True).start()


# =========================================================
# SCHEDULE LOGIC (pure functions, no database)
# =========================================================

def day_slots(d: date) -> list:
    """All slot start times (local, timezone-aware) on a working day."""
    if d.weekday() not in WORK_DAYS:
        return []
    cur = datetime.combine(d, time(WORK_START_HOUR), tzinfo=LOCAL_TZ)
    end = datetime.combine(d, time(WORK_END_HOUR), tzinfo=LOCAL_TZ)
    step = timedelta(minutes=SLOT_MINUTES)
    slots = []
    while cur + step <= end:
        slots.append(cur)
        cur += step
    return slots


def free_slots(d: date, booked_utc: set, now_local: datetime) -> list:
    earliest = now_local + timedelta(minutes=MIN_NOTICE_MINUTES)
    return [
        s for s in day_slots(d)
        if s >= earliest and s.astimezone(timezone.utc) not in booked_utc
    ]


def spread(items: list, n: int = 3) -> list:
    """Pick up to n options spread across the day instead of the first n."""
    if len(items) <= n:
        return list(items)
    idx = sorted({round(i * (len(items) - 1) / (n - 1)) for i in range(n)})
    return [items[i] for i in idx]


def parse_local_start(text_value: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(str(text_value).strip(), fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    return None


def pretty_when(local_dt: datetime) -> dict:
    return {
        "day": local_dt.strftime("%A %d %B"),
        "time": local_dt.strftime("%I:%M %p").lstrip("0"),
    }


def today_text() -> str:
    now = datetime.now(LOCAL_TZ)
    return f"{now.strftime('%A, %d %B %Y')} ({TZ_NAME})"


# =========================================================
# LEAD HELPERS (used by the server)
# =========================================================

def _lead_dict(lead: Lead) -> dict:
    return {"id": lead.id, "name": lead.name, "phone": lead.phone, "source": lead.source}


def create_lead(name: str, phone: str, source: str) -> dict:
    import uuid

    lead = Lead(id=uuid.uuid4().hex[:12], name=name, phone=phone, source=source)
    with Session(engine) as session:
        session.add(lead)
        session.commit()
        session.refresh(lead)
        return _lead_dict(lead)


def get_lead(lead_id: str) -> Optional[dict]:
    if not lead_id:
        return None
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        return _lead_dict(lead) if lead else None


def delete_lead(lead_id: str) -> None:
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        if lead:
            session.delete(lead)
            session.commit()


def is_do_not_call(phone: str) -> bool:
    with Session(engine) as session:
        return session.get(DoNotCall, phone) is not None


def _set_status(session: Session, lead_id: str, status: str) -> Optional[Lead]:
    lead = session.get(Lead, lead_id)
    if lead:
        lead.status = status
        session.add(lead)
    return lead


# =========================================================
# TOOLS (called by the model)
# =========================================================

def save_qualification(
    owns_property: str = "",
    monthly_bill: str = "",
    roof_type: str = "",
    location: str = "",
    timeline: str = "",
    wants_financing: str = "",
    interest_level: str = "",
    notes: str = "",
    lead_id: str = "",
    phone: str = "",
) -> dict:
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return {"error": "Unknown lead."}
        q = session.get(Qualification, lead_id) or Qualification(lead_id=lead_id)
        q.owns_property = owns_property
        q.monthly_bill = monthly_bill
        q.roof_type = roof_type
        q.location = location
        q.timeline = timeline
        q.wants_financing = wants_financing
        q.interest_level = interest_level
        q.notes = notes
        q.updated_at = _now_utc()
        session.add(q)
        if lead.status in ("calling", "qualified"):
            lead.status = "qualified"
            session.add(lead)
        session.commit()
        name, lead_phone = lead.name, lead.phone

    if interest_level.strip().lower() == "hot":
        notify(
            "Hot solar lead",
            f"{name or 'A lead'} ({lead_phone}) sounds very interested.\n"
            f"Location: {location}\nMonthly bill: {monthly_bill}\nTimeline: {timeline}\nNotes: {notes}",
            sms_text=f"Hot solar lead: {name or 'Lead'} {lead_phone}, {location}"[:160],
        )
    return {"status": "saved"}


def check_availability(date: str = "", lead_id: str = "", phone: str = "") -> dict:
    """Free slots for one day. date must be YYYY-MM-DD."""
    try:
        d = datetime.strptime(str(date).strip(), "%Y-%m-%d").date()
    except ValueError:
        return {"error": "The date must look like 2026-11-23."}

    now_local = datetime.now(LOCAL_TZ)
    if d < now_local.date():
        return {"error": "That date is in the past."}

    with Session(engine) as session:
        def free_on(day) -> list:
            start = datetime.combine(day, time(0), tzinfo=LOCAL_TZ).astimezone(timezone.utc)
            end = start + timedelta(days=1)
            rows = session.exec(
                select(Appointment.start_at).where(
                    Appointment.status == "booked",
                    Appointment.start_at >= start,
                    Appointment.start_at < end,
                )
            ).all()
            booked = {r.astimezone(timezone.utc) for r in rows}
            return free_slots(day, booked, now_local)

        slots = free_on(d)
        if slots:
            times = [s.strftime("%H:%M") for s in slots]
            return {
                "date": d.isoformat(),
                "weekday": d.strftime("%A"),
                "free_slots": times,
                "suggested": spread(times),
            }

        next_date = None
        for i in range(1, SEARCH_DAYS + 1):
            candidate = d + timedelta(days=i)
            if free_on(candidate):
                next_date = candidate
                break

    return {
        "date": d.isoformat(),
        "weekday": d.strftime("%A"),
        "free_slots": [],
        "note": "No free times that day. Appointments are Monday to Friday during working hours.",
        "next_available_date": next_date.isoformat() if next_date else None,
    }


def book_appointment(start: str = "", notes: str = "", lead_id: str = "", phone: str = "") -> dict:
    """Book a site survey. start is local time like 2026-11-23T10:30."""
    local_start = parse_local_start(start)
    if not local_start:
        return {"error": "The start time must look like 2026-11-23T10:30."}

    now_local = datetime.now(LOCAL_TZ)
    if local_start not in day_slots(local_start.date()):
        return {"error": "That is not a bookable time. Use a time from check_availability."}
    if local_start < now_local + timedelta(minutes=MIN_NOTICE_MINUTES):
        return {"error": "That time is too soon. Offer a later time."}

    start_utc = local_start.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(minutes=SLOT_MINUTES)

    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return {"error": "Unknown lead."}

        # Booking again moves the lead's earlier appointment instead of doubling up
        old = session.exec(
            select(Appointment).where(
                Appointment.lead_id == lead_id,
                Appointment.status == "booked",
                Appointment.start_at >= _now_utc(),
            )
        ).all()
        for a in old:
            a.status = "cancelled"
            session.add(a)
        session.flush()

        session.add(Appointment(lead_id=lead_id, start_at=start_utc, end_at=end_utc, notes=notes))
        lead.status = "appointment_booked"
        session.add(lead)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return {"error": "That time was just taken. Check availability again and offer another time."}

        q = session.get(Qualification, lead_id)
        name, lead_phone = lead.name, lead.phone
        summary = (
            f"Location: {q.location}\nMonthly bill: {q.monthly_bill}\nRoof: {q.roof_type}\n"
            f"Interest: {q.interest_level}\nNotes: {q.notes}"
            if q else "No qualification answers saved."
        )

    when = pretty_when(local_start)
    notify(
        f"Site survey booked: {when['day']} {when['time']}",
        f"{name or 'A lead'} ({lead_phone}) booked a site survey.\n"
        f"When: {when['day']} at {when['time']} ({TZ_NAME})\n\n{summary}",
        sms_text=f"Survey booked {when['day']} {when['time']}: {name or 'Lead'} {lead_phone}"[:160],
    )
    return {"status": "booked", "day": when["day"], "time": when["time"]}


def request_callback(
    preferred_time: str = "",
    reason: str = "",
    lead_id: str = "",
    phone: str = "",
) -> dict:
    """For leads who want a call later instead of booking a time now."""
    with Session(engine) as session:
        lead = session.get(Lead, lead_id)
        if not lead:
            return {"error": "Unknown lead."}
        session.add(Callback(lead_id=lead_id, preferred_time=preferred_time, reason=reason))
        if lead.status in ("calling", "qualified"):
            lead.status = "callback"
            session.add(lead)
        session.commit()
        name, lead_phone = lead.name, lead.phone

    notify(
        "Callback requested",
        f"{name or 'A lead'} ({lead_phone}) asked for a callback.\n"
        f"Preferred time: {preferred_time}\nReason: {reason}",
        sms_text=f"Callback: {name or 'Lead'} {lead_phone}, {preferred_time}"[:160],
    )
    return {"status": "noted", "message": "A team member will follow up."}


def mark_do_not_call(reason: str = "", lead_id: str = "", phone: str = "") -> dict:
    with Session(engine) as session:
        if phone and session.get(DoNotCall, phone) is None:
            session.add(DoNotCall(phone=phone, reason=reason))
        _set_status(session, lead_id, "do_not_call")
        # No reason to keep a future visit for someone who asked to stop
        for a in session.exec(
            select(Appointment).where(
                Appointment.lead_id == lead_id,
                Appointment.status == "booked",
                Appointment.start_at >= _now_utc(),
            )
        ).all():
            a.status = "cancelled"
            session.add(a)
        session.commit()
    return {"status": "done", "message": "This number will not be called again."}


FUNCTION_MAP = {
    "save_qualification": save_qualification,
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "request_callback": request_callback,
    "mark_do_not_call": mark_do_not_call,
}