"""
Expert Church Monitoring System
================================
A single-file Flask + SQL attendance system for churches.

Roles
-----
- superadmin : full control. Creates/deletes churches and church admins.
- admin      : belongs to one church. Manages members, marks attendance,
               registers first timers, runs absentee reports, sees
               birthday reminders for the current month.

Run locally:
    pip install -r requirements.txt
    python app.py

Deploy:
    See README.md for Render + SQLite Cloud instructions.
"""

import os
import io
import re
import calendar
import csv
import uuid
import base64
import hmac
import time
import hashlib
import secrets
import json
import smtplib
import click
import urllib.request
import urllib.error
from email.message import EmailMessage
from functools import wraps
from datetime import datetime, date, timedelta

from flask import (
    Flask, request, redirect, url_for, flash, render_template,
    abort, Response, send_from_directory, g
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required,
    current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy import extract, func, inspect, text
from sqlalchemy.exc import IntegrityError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# App & configuration
# ---------------------------------------------------------------------------

APP_NAME = "Expert Church Monitoring System"

app = Flask(__name__)
# Behind Render's proxy, trust X-Forwarded-Proto/Host so external URLs (the QR
# check-in link) come out as https://your-domain rather than http://internal-host.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_db_url = os.environ.get("DATABASE_URL", "sqlite:///church.db")
# Render / some providers hand out "postgres://" which SQLAlchemy 1.4+ rejects.
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

# Each church can have its own address, e.g. rccg.expertchurch.com. Set
# BASE_DOMAIN=expertchurch.com to switch that on; left empty (local dev), the
# app behaves as one site and ignores the host name.
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "").strip().lower().lstrip(".")
RESERVED_SUBDOMAINS = {"www", "admin", "app", "mail", "api", "static"}
SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Nigeria (WAT) is a fixed UTC+1 offset year-round — no DST to account for.
NIGERIA_OFFSET = timedelta(hours=1)


@app.template_filter("lagos_time")
def lagos_time(dt):
    """Convert a naive UTC datetime (as stored in the db) to Nigeria local time for display."""
    if dt is None:
        return dt
    return dt + NIGERIA_OFFSET
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024  # 3 MB uploads

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

# Outgoing mail (absentee follow-ups). Any SMTP provider works — for Gmail use
# smtp.gmail.com / 587 and an App Password, not the account password.
MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or MAIL_USERNAME
MAIL_USE_SSL = os.environ.get("MAIL_USE_SSL", "0") == "1"  # port 465 style; otherwise STARTTLS

# Outgoing SMS (absentee follow-ups) through eBulkSMS (https://ebulksms.com).
# The username is the email address you log in to eBulkSMS with; generate the
# API key in its dashboard. The sender name is at most 11 letters/digits.
EBULKSMS_USERNAME = os.environ.get("EBULKSMS_USERNAME", "")
EBULKSMS_API_KEY = os.environ.get("EBULKSMS_API_KEY", "")
EBULKSMS_SENDER = os.environ.get("EBULKSMS_SENDER", "")
EBULKSMS_DND = os.environ.get("EBULKSMS_DND", "1") == "1"  # also reach MTN DND numbers (2 units each)
EBULKSMS_URL = os.environ.get("EBULKSMS_URL", "https://api.ebulksms.com/sendsms.json")

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to continue."
login_manager.login_message_category = "warning"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Church(db.Model):
    __tablename__ = "church"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    code = db.Column(db.String(10), unique=True, nullable=False)
    subdomain = db.Column(db.String(63), unique=True)  # rccg -> rccg.<BASE_DOMAIN>
    address = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(120))
    pastor_name = db.Column(db.String(120))
    description = db.Column(db.Text)
    logo_filename = db.Column(db.String(255))  # legacy: file on disk (lost on Render redeploys)
    # The logo itself, base64-encoded in the database so it survives redeploys
    # on hosts with a wiped filesystem (e.g. Render's free plan).
    logo_data = db.Column(db.Text)
    logo_mimetype = db.Column(db.String(50))
    next_seq = db.Column(db.Integer, default=1, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    checkin_secret = db.Column(db.String(64))  # signs the daily QR check-in link
    absentee_email_subject = db.Column(db.String(200))  # last-used template, remembered for next time
    absentee_email_body = db.Column(db.Text)
    absentee_sms_body = db.Column(db.Text)
    # Comma-separated feature keys the superadmin switched on. NULL means the
    # church has never been configured and keeps every feature that existed
    # before the switches were added (DEFAULT_FEATURES).
    features = db.Column(db.Text)
    # First-timer tracking: how long a first timer is followed, and how many
    # services they must attend in that time to become a permanent member.
    first_timer_months = db.Column(db.Integer)
    first_timer_min_services = db.Column(db.Integer)
    current_service_id = db.Column(db.Integer)  # the service QR check-in scans count for

    def next_member_number(self):
        # If every member record for this church has been deleted, start the
        # numbering over from scratch instead of continuing the old count —
        # there's nothing left to collide with.
        if not Member.query.filter_by(church_id=self.id).first():
            self.next_seq = 1
        seq = self.next_seq or 1
        self.next_seq = seq + 1
        return f"{self.code}-{seq:04d}"


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'superadmin', 'admin' (one per church), 'user' or 'unit_head'
    full_name = db.Column(db.String(120))
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)  # set after a reset
    department_id = db.Column(db.Integer)  # a 'unit_head' login belongs to exactly one department

    church = db.relationship("Church", backref="admins")

    def set_password(self, raw, temporary=False):
        self.password_hash = generate_password_hash(raw)
        self.must_change_password = temporary

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)


class Member(db.Model):
    __tablename__ = "member"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    member_number = db.Column(db.String(20), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    gender = db.Column(db.String(10))
    date_of_birth = db.Column(db.Date)
    phone = db.Column(db.String(50))
    email = db.Column(db.String(120))
    address = db.Column(db.String(255))
    photo_filename = db.Column(db.String(255))
    status = db.Column(db.String(20), default="first_timer")  # first_timer / member
    is_editable = db.Column(db.Boolean, default=False, nullable=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    left_at = db.Column(db.DateTime, nullable=True)  # set when moved to "no longer in church"
    is_child = db.Column(db.Boolean, default=False, nullable=False)  # tracked in children's church separately
    first_timer_start = db.Column(db.Date)  # day the tracking period began
    promoted_at = db.Column(db.DateTime)  # when a first timer became a permanent member
    emergency_contact_name = db.Column(db.String(120))
    emergency_contact_phone = db.Column(db.String(50))

    church = db.relationship("Church", backref="members")

    __table_args__ = (
        db.UniqueConstraint("church_id", "member_number", name="uq_church_member_number"),
    )


class Service(db.Model):
    """A named service a church holds, e.g. 'First Service' or 'Midweek Bible Study'."""
    __tablename__ = "service"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    weekday = db.Column(db.Integer)  # usual day, 0 = Monday ... 6 = Sunday; optional
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Attendance(db.Model):
    __tablename__ = "attendance"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    service_date = db.Column(db.Date, nullable=False, default=date.today)
    service_id = db.Column(db.Integer, db.ForeignKey("service.id"), nullable=False)
    marked_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member", backref="attendances")
    service = db.relationship("Service")

    __table_args__ = (
        db.UniqueConstraint("member_id", "service_date", "service_id", name="uq_member_date_service"),
    )


class AbsenteeEmail(db.Model):
    """One row per follow-up email sent, so a member is only emailed once per absence period."""
    __tablename__ = "absentee_email"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("member_id", "period_start", "period_end", name="uq_absentee_email_period"),
    )


class AbsenteeSMS(db.Model):
    """One row per follow-up SMS sent, so a member is only texted once per absence period."""
    __tablename__ = "absentee_sms"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("member_id", "period_start", "period_end", name="uq_absentee_sms_period"),
    )


class ServiceSegment(db.Model):
    """A part of the service whose minister is logged, e.g. 'Opening Prayer / Call to Worship'."""
    __tablename__ = "service_segment"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)


class ServiceLog(db.Model):
    """What happened at one service: the sermon, the preacher and who handled each segment."""
    __tablename__ = "service_log"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    service_date = db.Column(db.Date, nullable=False)
    service_id = db.Column(db.Integer, db.ForeignKey("service.id"), nullable=False)
    sermon_topic = db.Column(db.String(200))
    bible_passages = db.Column(db.String(300))
    preacher = db.Column(db.String(120))
    notes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    service = db.relationship("Service")
    ministers = db.relationship("ServiceLogMinister", backref="log", cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("church_id", "service_date", "service_id", name="uq_service_log"),
    )


class ServiceLogMinister(db.Model):
    __tablename__ = "service_log_minister"

    id = db.Column(db.Integer, primary_key=True)
    log_id = db.Column(db.Integer, db.ForeignKey("service_log.id"), nullable=False)
    segment_id = db.Column(db.Integer, db.ForeignKey("service_segment.id"), nullable=False)
    minister = db.Column(db.String(120), nullable=False)

    segment = db.relationship("ServiceSegment")

    __table_args__ = (
        db.UniqueConstraint("log_id", "segment_id", name="uq_log_segment"),
    )


class Department(db.Model):
    """A department or unit (choir, ushers, media) with its own head and roster."""
    __tablename__ = "department"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    leave_token = db.Column(db.String(40), unique=True, nullable=False)  # in the public leave-request link
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DepartmentMember(db.Model):
    __tablename__ = "department_member"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member")

    __table_args__ = (
        db.UniqueConstraint("department_id", "member_id", name="uq_department_member"),
    )


class DepartmentMeeting(db.Model):
    __tablename__ = "department_meeting"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    meeting_date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(150), nullable=False)  # unit meeting, rehearsal, worker prayers
    minutes = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    attendance = db.relationship("MeetingAttendance", backref="meeting", cascade="all, delete-orphan")


MEETING_STATUSES = ("present", "late", "absent", "excused")


class MeetingAttendance(db.Model):
    __tablename__ = "meeting_attendance"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("department_meeting.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    status = db.Column(db.String(10), nullable=False)  # present, late, absent or excused
    note = db.Column(db.String(200))

    member = db.relationship("Member")

    __table_args__ = (
        db.UniqueConstraint("meeting_id", "member_id", name="uq_meeting_member"),
    )


class LeaveRequest(db.Model):
    """A worker's request to be absent from duty, approved or declined by the unit head or admin."""
    __tablename__ = "leave_request"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="pending")  # pending, approved, declined
    decided_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    decided_at = db.Column(db.DateTime)
    decision_note = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member")


class UnitReport(db.Model):
    """Minutes, a report or a financial account sent by a unit to the pastoral office."""
    __tablename__ = "unit_report"

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    kind = db.Column(db.String(10), nullable=False)  # minutes, report or financial
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    submitted_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)


class MessageTemplate(db.Model):
    """A church's saved wording for one kind of message (e.g. birthday) on one channel (sms or email)."""
    __tablename__ = "message_template"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    kind = db.Column(db.String(20), nullable=False)  # 'birthday' or 'first_timer'
    channel = db.Column(db.String(10), nullable=False)  # 'sms' or 'email'
    subject = db.Column(db.String(200))
    body = db.Column(db.Text, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("church_id", "kind", "channel", name="uq_message_template"),
    )


class MessageLog(db.Model):
    """One row per message sent, so the same person is not sent the same message twice by accident."""
    __tablename__ = "message_log"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    channel = db.Column(db.String(10), nullable=False)
    period_key = db.Column(db.String(20), nullable=False)  # birthday: the year; first timer: the date
    sent_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("member_id", "kind", "channel", "period_key", name="uq_message_log"),
    )


class Program(db.Model):
    """A special, usually one-off program (conference, retreat) people register for by QR code."""
    __tablename__ = "program"

    id = db.Column(db.Integer, primary_key=True)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    program_date = db.Column(db.Date)
    description = db.Column(db.Text)
    token = db.Column(db.String(40), unique=True, nullable=False)  # in the public registration link
    is_open = db.Column(db.Boolean, default=True, nullable=False)
    deadline = db.Column(db.Date)  # last day people can register; empty means no deadline
    cap = db.Column(db.Integer)  # maximum registrations; empty means no limit
    extra_question = db.Column(db.String(200))  # optional extra question shown on the form
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    church = db.relationship("Church", backref="programs")
    registrations = db.relationship("ProgramRegistration", backref="program", lazy="dynamic")


class ProgramRegistration(db.Model):
    __tablename__ = "program_registration"

    id = db.Column(db.Integer, primary_key=True)
    program_id = db.Column(db.Integer, db.ForeignKey("program.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    full_name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    phone_key = db.Column(db.String(20), nullable=False)  # digits only, so 0803... and +234803... are the same person
    email = db.Column(db.String(120))
    gender = db.Column(db.String(10))
    address = db.Column(db.String(255))
    date_of_birth = db.Column(db.Date)  # day and month only, like members
    marital_status = db.Column(db.String(30))
    occupation = db.Column(db.String(120))
    is_member = db.Column(db.Boolean, default=False, nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"))
    extra_answer = db.Column(db.String(300))
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("program_id", "phone_key", name="uq_program_phone"),
    )


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def role_required(*roles):
    def decorator(fn):
        @wraps(fn)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


# Everyone who works inside a church. The church admin additionally manages
# settings, deletions, messaging, exports and the church's users.
STAFF = ("admin", "user")

# Features the superadmin can switch on or off for each church.
FEATURES = {
    "children": "Children's church",
    "qr_checkin": "QR self check-in",
    "absentee_email": "Absentee follow-up emails",
    "absentee_sms": "Absentee follow-up SMS",
    "first_timers": "First-timer tracking and automatic promotion",
    "programs": "Special programs with QR registration",
    "services": "Several named services, day filters and attendance report",
    "service_analytics": "Service Analytics: service log and one-click report",
    "departments": "Departments and Units: rosters, meetings, leave requests and reports",
    "birthday_messages": "Birthday messages, sent by staff with a button",
    "first_timer_messages": "First-timer welcome messages, sent by staff with a button",
}
# What a church that has never been configured gets: everything that existed
# before the switches did. New features are added to FEATURES but not here, so
# they stay off until the superadmin enables them.
DEFAULT_FEATURES = {"children", "qr_checkin", "absentee_email", "absentee_sms"}


def church_features(church):
    if church is None:
        return set()
    if church.features is None:
        return set(DEFAULT_FEATURES)
    return {f for f in church.features.split(",") if f in FEATURES}


def feature_required(key):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if key not in church_features(current_user.church):
                abort(404)
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def temp_password():
    return secrets.token_urlsafe(7)


# --- Services ----------------------------------------------------------------
# Every attendance record points at a service. A church that never sets any up
# has one hidden 'Main Service', so nothing changes for it. With the "services"
# feature on, the admin can add more and pick which one attendance is for.

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def church_services(church):
    services = Service.query.filter_by(church_id=church.id).order_by(Service.id).all()
    if not services:
        service = Service(church_id=church.id, name="Main Service")
        db.session.add(service)
        db.session.commit()
        services = [service]
    return services


def multi_service(church):
    """True when the church uses several services, so pickers and filters should show."""
    return "services" in church_features(church) and len(church_services(church)) > 1


def current_service(church):
    """The service QR check-ins and quick entries count for."""
    services = church_services(church)
    if "services" in church_features(church) and church.current_service_id:
        for s in services:
            if s.id == church.current_service_id:
                return s
    return services[0]


def chosen_service(church, raw_id):
    """The service picked on a form, falling back to the current one."""
    if "services" in church_features(church) and str(raw_id or "").isdigit():
        for s in church_services(church):
            if s.id == int(raw_id):
                return s
    return current_service(church)


def attendance_filter(church):
    """(weekdays, service ids) ticked on a report. Empty sets mean no restriction."""
    if "services" not in church_features(church):
        return set(), set()
    days = {int(d) for d in request.args.getlist("day") if d.isdigit() and 0 <= int(d) <= 6}
    valid = {s.id for s in church_services(church)}
    picked = {int(x) for x in request.args.getlist("service") if x.isdigit() and int(x) in valid}
    return days, picked


def apply_attendance_filter(query, days, service_ids):
    if days:
        # SQL day-of-week counts Sunday as 0; Python's weekday() counts Monday as 0.
        query = query.filter(extract("dow", Attendance.service_date).in_([(d + 1) % 7 for d in days]))
    if service_ids:
        query = query.filter(Attendance.service_id.in_(service_ids))
    return query


MIN_PASSWORD_LENGTH = 8


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(file_storage):
    """Save an uploaded image with a random filename. Returns filename or None."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        flash("Only image files (png, jpg, jpeg, gif, webp) are allowed.", "danger")
        return None
    ext = secure_filename(file_storage.filename).rsplit(".", 1)[1].lower()
    fname = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
    return fname


def parse_date(value, default=None):
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return default


# Birthdays are stored as day + month only — never a year. People are often
# uncomfortable sharing their true birth year, and the app only ever needs
# the day/month to remind admins of upcoming birthdays. We still store it in
# a normal db.Date column (so extract()/strftime() keep working) by pinning
# every birthday to the same placeholder leap year, which is never shown.
BIRTHDAY_YEAR = 2000  # a leap year, so 29 February is a valid choice

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_NAME_TO_NUM = {}
for _i, _name in enumerate(MONTH_NAMES, start=1):
    _MONTH_NAME_TO_NUM[_name.lower()] = _i
    _MONTH_NAME_TO_NUM[_name[:3].lower()] = _i


def parse_month(value):
    """Accept a month as a number (1-12) or a name/abbreviation ('August', 'Aug')."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        m = int(float(value))
        return m if 1 <= m <= 12 else None
    except ValueError:
        return _MONTH_NAME_TO_NUM.get(value.lower())


def build_birthday(day_value, month_value):
    """Combine separate day + month inputs into a placeholder-year date, or None."""
    day_value = (day_value or "").strip()
    month = parse_month(month_value)
    if not day_value or month is None:
        return None
    try:
        day = int(float(day_value))
    except ValueError:
        return None
    try:
        return date(BIRTHDAY_YEAR, month, day)
    except ValueError:
        return None


SIDEBAR_VERSES = [
    ("For where two or three are gathered together in my name, there am I in the midst of them.", "Matthew 18:20"),
    ("Not forsaking the assembling of ourselves together, as the manner of some is; but exhorting one another.", "Hebrews 10:25"),
    ("I was glad when they said unto me, Let us go into the house of the LORD.", "Psalm 122:1"),
    ("Behold, how good and how pleasant it is for brethren to dwell together in unity!", "Psalm 133:1"),
]

AVATAR_PALETTE = ["#fb2c36", "#0f9d76", "#171717", "#f2a93b", "#7c5cf6", "#0891b2", "#c2410c", "#65a30d"]


@app.template_filter("initials")
def initials_filter(name):
    parts = [p for p in (name or "").strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@app.template_filter("avatar_color")
def avatar_color_filter(member_id):
    return AVATAR_PALETTE[(member_id or 0) % len(AVATAR_PALETTE)]


def clean_subdomain(raw):
    """Return (subdomain, error). An empty value is fine and means 'no own address'."""
    value = (raw or "").strip().lower()
    if not value:
        return None, None
    if not SUBDOMAIN_RE.match(value):
        return None, "Subdomain may only use letters, numbers and hyphens."
    if value in RESERVED_SUBDOMAINS:
        return None, f"'{value}' is reserved. Choose another subdomain."
    return value, None


def church_base_url(church):
    """Site root for a church: its own subdomain when set up, else the current host."""
    if BASE_DOMAIN and church.subdomain:
        return f"{request.scheme}://{church.subdomain}.{BASE_DOMAIN}"
    return request.host_url.rstrip("/")


@app.before_request
def resolve_tenant():
    """Work out which church this host name belongs to and keep logins inside it."""
    g.tenant = None
    if not BASE_DOMAIN:
        return
    host = request.host.split(":")[0].lower()
    if not host.endswith("." + BASE_DOMAIN):
        return  # main site (apex, www, Render's own address): superadmin area
    sub = host[: -len(BASE_DOMAIN) - 1]
    if sub in RESERVED_SUBDOMAINS:
        return
    g.tenant = Church.query.filter_by(subdomain=sub).first()
    if g.tenant is None:
        abort(404)
    if current_user.is_authenticated and current_user.church_id != g.tenant.id:
        logout_user()  # a session from another church or the superadmin; never carry over


@app.before_request
def require_password_change():
    """After a reset the temporary password only works to reach the change-password page."""
    if (current_user.is_authenticated and current_user.must_change_password
            and request.endpoint not in ("change_password", "logout", "static")):
        return redirect(url_for("change_password"))


@app.context_processor
def inject_globals():
    hour = datetime.now().hour
    if hour < 12:
        greeting_word = "Good Morning"
    elif hour < 17:
        greeting_word = "Good Afternoon"
    else:
        greeting_word = "Good Evening"

    birthday_count = 0
    if current_user.is_authenticated and current_user.role in STAFF and current_user.church_id:
        today = date.today()
        birthday_count = Member.query.filter(
            Member.church_id == current_user.church_id,
            Member.left_at.is_(None), Member.is_child.is_(False),
            Member.date_of_birth.isnot(None),
            extract("month", Member.date_of_birth) == today.month,
        ).count()

    verse_text, verse_ref = SIDEBAR_VERSES[date.today().toordinal() % len(SIDEBAR_VERSES)]

    return {
        "now_year": datetime.utcnow().year, "app_name": APP_NAME, "month_names": MONTH_NAMES,
        "greeting_word": greeting_word, "global_birthday_count": birthday_count,
        "sidebar_verse_text": verse_text, "sidebar_verse_ref": verse_ref,
        "today": date.today(), "base_domain": BASE_DOMAIN,
        "has_feature": lambda key: (
            current_user.is_authenticated and key in church_features(current_user.church)
        ),
        "all_features": FEATURES, "default_features": DEFAULT_FEATURES,
        "church_features": church_features,
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role == "superadmin":
            return redirect(url_for("superadmin_dashboard"))
        if current_user.role == "unit_head":
            return redirect(url_for("dept_home", dept_id=current_user.department_id))
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if g.tenant and user.church_id != g.tenant.id:
                flash("Invalid username or password.", "danger")
                return render_template("auth_login.html")
            if not g.tenant and BASE_DOMAIN and user.role != "superadmin" and user.church and user.church.subdomain:
                flash(f"Please log in at {user.church.subdomain}.{BASE_DOMAIN}", "warning")
                return render_template("auth_login.html")
            login_user(user)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("index"))
        flash("Invalid username or password.", "danger")
    return render_template("auth_login.html")


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    forced = current_user.must_change_password
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("Your current password is wrong.", "danger")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f"The new password must be at least {MIN_PASSWORD_LENGTH} characters.", "danger")
        elif new != confirm:
            flash("The two new passwords don't match.", "danger")
        elif new == current:
            flash("Choose a password different from the current one.", "danger")
        else:
            current_user.set_password(new)
            db.session.commit()
            flash("Password changed.", "success")
            return redirect(url_for("index"))
    return render_template("auth_change_password.html", forced=forced)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


LOGO_MIMETYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def save_church_logo(church, file_storage):
    """Store an uploaded logo in the database. Returns True if a logo was saved."""
    if not file_storage or file_storage.filename == "":
        return False
    if not allowed_file(file_storage.filename):
        flash("Only image files (png, jpg, jpeg, gif, webp) are allowed.", "danger")
        return False
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    church.logo_data = base64.b64encode(file_storage.read()).decode("ascii")
    church.logo_mimetype = LOGO_MIMETYPES[ext]
    church.logo_filename = None
    return True


@app.template_global()
def church_logo_url(church):
    """URL of a church's logo, or None. Falls back to an old on-disk upload if it still exists."""
    if not church:
        return None
    if church.logo_data:
        # The version hash changes whenever the logo does, so browsers can cache it safely.
        version = hashlib.sha1(church.logo_data.encode()).hexdigest()[:10]
        return url_for("church_logo", church_id=church.id, v=version)
    if church.logo_filename and os.path.exists(os.path.join(app.config["UPLOAD_FOLDER"], church.logo_filename)):
        return url_for("uploaded_file", filename=church.logo_filename)
    return None


@app.route("/church/<int:church_id>/logo")
def church_logo(church_id):
    # Public on purpose: the member check-in page shows the logo without a login.
    church = Church.query.get_or_404(church_id)
    if not church.logo_data:
        abort(404)
    resp = Response(base64.b64decode(church.logo_data), mimetype=church.logo_mimetype or "image/png")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------------------
# Superadmin routes
# ---------------------------------------------------------------------------

def selected_features(form):
    return ",".join(key for key in FEATURES if form.get("feature_" + key))


@app.route("/superadmin")
@role_required("superadmin")
def superadmin_dashboard():
    churches = Church.query.order_by(Church.created_at.desc()).all()
    total_admins = User.query.filter_by(role="admin").count()
    total_members = Member.query.count()
    return render_template(
        "superadmin/dashboard.html", churches=churches,
        total_admins=total_admins, total_members=total_members
    )


@app.route("/superadmin/churches/new", methods=["GET", "POST"])
@role_required("superadmin")
def superadmin_new_church():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip().upper()
        subdomain, sub_error = clean_subdomain(request.form.get("subdomain"))
        if not name or not code:
            flash("Name and code are required.", "danger")
        elif Church.query.filter_by(code=code).first():
            flash("That church code is already in use.", "danger")
        elif sub_error:
            flash(sub_error, "danger")
        elif subdomain and Church.query.filter_by(subdomain=subdomain).first():
            flash("That subdomain is already in use.", "danger")
        else:
            church = Church(
                name=name, code=code, subdomain=subdomain,
                features=selected_features(request.form),
                pastor_name=request.form.get("pastor_name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=request.form.get("email", "").strip(),
                address=request.form.get("address", "").strip(),
                description=request.form.get("description", "").strip(),
            )
            save_church_logo(church, request.files.get("logo"))
            db.session.add(church)
            db.session.commit()
            flash(f"Church '{name}' created.", "success")
            return redirect(url_for("superadmin_church_detail", church_id=church.id))
    return render_template("superadmin/church_form.html", church=None)


@app.route("/superadmin/churches/<int:church_id>/edit", methods=["GET", "POST"])
@role_required("superadmin")
def superadmin_edit_church(church_id):
    church = Church.query.get_or_404(church_id)
    if request.method == "POST":
        subdomain, sub_error = clean_subdomain(request.form.get("subdomain"))
        clash = subdomain and Church.query.filter(Church.subdomain == subdomain, Church.id != church.id).first()
        if sub_error or clash:
            flash(sub_error or "That subdomain is already in use.", "danger")
            return render_template("superadmin/church_form.html", church=church)
        church.subdomain = subdomain
        church.features = selected_features(request.form)
        church.name = request.form.get("name", "").strip() or church.name
        church.pastor_name = request.form.get("pastor_name", "").strip()
        church.phone = request.form.get("phone", "").strip()
        church.email = request.form.get("email", "").strip()
        church.address = request.form.get("address", "").strip()
        church.description = request.form.get("description", "").strip()
        save_church_logo(church, request.files.get("logo"))
        db.session.commit()
        flash("Church details updated.", "success")
        return redirect(url_for("superadmin_church_detail", church_id=church.id))
    return render_template("superadmin/church_form.html", church=church)


@app.route("/superadmin/churches/<int:church_id>")
@role_required("superadmin")
def superadmin_church_detail(church_id):
    church = Church.query.get_or_404(church_id)
    return render_template("superadmin/church_detail.html", church=church)


@app.route("/superadmin/churches/<int:church_id>/delete", methods=["POST"])
@role_required("superadmin")
def superadmin_delete_church(church_id):
    church = Church.query.get_or_404(church_id)
    # Plain queries, not church.members: loading the relationships first makes SQLAlchemy try to
    # update rows the bulk deletes below have already removed.
    Attendance.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    AbsenteeEmail.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    AbsenteeSMS.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    for dept in Department.query.filter_by(church_id=church.id).all():
        delete_department(dept)
    log_ids = [r[0] for r in db.session.query(ServiceLog.id).filter_by(church_id=church.id).all()]
    if log_ids:
        ServiceLogMinister.query.filter(ServiceLogMinister.log_id.in_(log_ids)).delete(synchronize_session=False)
    ServiceLog.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    ServiceSegment.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    MessageLog.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    MessageTemplate.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    ProgramRegistration.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    Program.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    Member.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    Service.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    User.query.filter_by(church_id=church.id).delete(synchronize_session=False)
    name = church.name
    db.session.delete(church)
    db.session.commit()
    flash(f"Church '{name}' and all related data were deleted.", "success")
    return redirect(url_for("superadmin_dashboard"))


@app.route("/superadmin/churches/<int:church_id>/admins/new", methods=["GET", "POST"])
@role_required("superadmin")
def superadmin_new_admin(church_id):
    church = Church.query.get_or_404(church_id)
    if User.query.filter_by(church_id=church.id, role="admin").first():
        flash("This church already has its admin. Remove that admin first to replace them.", "warning")
        return redirect(url_for("superadmin_church_detail", church_id=church.id))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        if not username:
            flash("Username is required.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("That username is already taken.", "danger")
        else:
            password = temp_password()
            admin = User(username=username, role="admin", church_id=church.id, full_name=full_name)
            admin.set_password(password, temporary=True)
            db.session.add(admin)
            db.session.commit()
            flash(f"Admin '{username}' created for {church.name}. Temporary password: {password} "
                  "(shown once; they must change it at first login).", "success")
            return redirect(url_for("superadmin_church_detail", church_id=church.id))
    return render_template("superadmin/admin_form.html", church=church)


@app.route("/superadmin/admins/<int:admin_id>/reset-password", methods=["POST"])
@role_required("superadmin")
def superadmin_reset_admin_password(admin_id):
    admin = User.query.get_or_404(admin_id)
    if admin.role != "admin":
        abort(403)
    password = temp_password()
    admin.set_password(password, temporary=True)
    db.session.commit()
    flash(f"New temporary password for '{admin.username}': {password} "
          "(shown once; they must change it at next login).", "success")
    return redirect(url_for("superadmin_church_detail", church_id=admin.church_id))


@app.route("/superadmin/admins/<int:admin_id>/delete", methods=["POST"])
@role_required("superadmin")
def superadmin_delete_admin(admin_id):
    admin = User.query.get_or_404(admin_id)
    if admin.role != "admin":
        abort(403)
    church_id = admin.church_id
    username = admin.username
    db.session.delete(admin)
    db.session.commit()
    flash(f"Admin '{username}' removed.", "success")
    return redirect(url_for("superadmin_church_detail", church_id=church_id))


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

def pct_change(old, new):
    """Percentage change from old to new, or None when it can't be meaningfully computed."""
    if not old:
        return None
    return round((new - old) / old * 100)


def _attendance_series(church_id, total_members_now):
    """Present/absent series for the weekly / monthly / yearly chart toggle,
    built from one grouped query over the trailing 12 months of real attendance."""
    today = date.today()

    first_of_this_month = today.replace(day=1)
    y, m = first_of_this_month.year, first_of_this_month.month
    months = [(y, m)]
    for _ in range(11):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        months.append((y, m))
    months.reverse()
    range_start = date(months[0][0], months[0][1], 1)

    rows = (
        db.session.query(Attendance.service_date, func.count(func.distinct(Attendance.member_id)))
        .join(Member)
        .filter(
            Attendance.church_id == church_id, Member.is_child.is_(False),
            Attendance.service_date >= range_start, Attendance.service_date <= today,
        )
        .group_by(Attendance.service_date).all()
    )
    present_by_date = dict(rows)

    weekly = {"labels": [], "present": [], "absent": []}
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        p = present_by_date.get(d, 0)
        weekly["labels"].append(d.strftime("%d %b"))
        weekly["present"].append(p)
        weekly["absent"].append(max(total_members_now - p, 0))

    monthly = {"labels": [], "present": [], "absent": []}
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        p = present_by_date.get(d, 0)
        monthly["labels"].append(d.strftime("%d %b"))
        monthly["present"].append(p)
        monthly["absent"].append(max(total_members_now - p, 0))

    yearly = {"labels": [], "present": [], "absent": []}
    for (yy, mm) in months:
        start = date(yy, mm, 1)
        end = date(yy + 1, 1, 1) if mm == 12 else date(yy, mm + 1, 1)
        month_dates = [d for d in present_by_date if start <= d < end]
        present_sum = sum(present_by_date[d] for d in month_dates)
        possible = total_members_now * len(month_dates)
        yearly["labels"].append(start.strftime("%b %Y"))
        yearly["present"].append(present_sum)
        yearly["absent"].append(max(possible - present_sum, 0))

    return {"weekly": weekly, "monthly": monthly, "yearly": yearly}


@app.route("/admin")
@role_required(*STAFF)
def admin_dashboard():
    church = current_user.church
    today = date.today()
    if "first_timers" in church_features(church):
        first_timer_rows(church)  # promotes anyone who has qualified since the last visit
    all_members = Member.query.filter_by(church_id=church.id, left_at=None, is_child=False).all()
    total_members = len(all_members)
    all_editable = bool(all_members) and all(m.is_editable for m in all_members)
    left_count = Member.query.filter(Member.church_id == church.id, Member.left_at.isnot(None)).count()
    child_count = Member.query.filter_by(church_id=church.id, left_at=None, is_child=True).count()
    todays_attendance = (
        Attendance.query.join(Member).filter(
            Attendance.church_id == church.id, Attendance.service_date == today, Member.is_child.is_(False),
        ).order_by(Attendance.marked_at.desc()).all()
    )
    present_today = len({a.member_id for a in todays_attendance})  # present if at any service today
    absent_today = max(total_members - present_today, 0)
    birthdays = (
        Member.query.filter(
            Member.church_id == church.id,
            Member.left_at.is_(None),
            Member.is_child.is_(False),
            Member.date_of_birth.isnot(None),
            extract("month", Member.date_of_birth) == today.month,
        ).order_by(extract("day", Member.date_of_birth)).all()
    )

    # --- Real trend indicators for the stat tiles ---
    thirty_days_ago = today - timedelta(days=30)
    total_members_30d_ago = Member.query.filter(
        Member.church_id == church.id, Member.is_child.is_(False), Member.joined_at <= thirty_days_ago,
    ).count()
    total_members_trend = pct_change(total_members_30d_ago, total_members)

    a_week_ago = today - timedelta(days=7)
    present_last_week = db.session.query(func.count(func.distinct(Attendance.member_id))).join(Member).filter(
        Attendance.church_id == church.id, Attendance.service_date == a_week_ago, Member.is_child.is_(False),
    ).scalar() or 0
    present_trend = pct_change(present_last_week, present_today)
    absent_last_week = max(total_members - present_last_week, 0)
    absent_trend = pct_change(absent_last_week, absent_today)

    last_month_date = (today.replace(day=1) - timedelta(days=1))
    birthdays_last_month = Member.query.filter(
        Member.church_id == church.id, Member.left_at.is_(None), Member.is_child.is_(False),
        Member.date_of_birth.isnot(None),
        extract("month", Member.date_of_birth) == last_month_date.month,
    ).count()
    birthdays_delta = len(birthdays) - birthdays_last_month

    chart_series = _attendance_series(church.id, total_members)
    present_pct = round(present_today / total_members * 100) if total_members else 0
    absent_pct = 100 - present_pct if total_members else 0

    return render_template(
        "admin/dashboard.html", church=church, today=today,
        total_members=total_members, present_today=present_today, absent_today=absent_today,
        todays_attendance=todays_attendance, birthdays=birthdays,
        all_editable=all_editable, left_count=left_count, child_count=child_count,
        total_members_trend=total_members_trend, present_trend=present_trend,
        absent_trend=absent_trend, birthdays_delta=birthdays_delta,
        chart_series=chart_series, present_pct=present_pct, absent_pct=absent_pct,
        services=church_services(church), multi=multi_service(church), current_service=current_service(church),
    )


@app.route("/admin/members/toggle-edit-all", methods=["POST"])
@role_required("admin")
def admin_toggle_edit_all():
    church = current_user.church
    members = Member.query.filter_by(church_id=church.id, left_at=None, is_child=False).all()
    if not members:
        flash("There are no members yet.", "info")
        return redirect(url_for("admin_dashboard"))
    new_state = not all(m.is_editable for m in members)
    for m in members:
        m.is_editable = new_state
    db.session.commit()
    state_txt = "unlocked" if new_state else "locked"
    flash(f"All {len(members)} member record(s) are now {state_txt} for editing.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/attendance/mark", methods=["POST"])
@role_required(*STAFF)
def mark_attendance():
    church = current_user.church
    raw = request.form.get("member_numbers", "") or request.form.get("member_number", "")
    # Accept numbers separated by spaces, commas, and/or newlines — so pasting
    # or typing several at once (e.g. a whole family) works in one go.
    numbers = []
    seen = set()
    for tok in re.split(r"[,\s]+", raw.strip()):
        tok = tok.strip().upper()
        if tok and tok not in seen:
            seen.add(tok)
            numbers.append(tok)

    if not numbers:
        flash("Enter at least one member number.", "danger")
        return redirect(url_for("admin_dashboard"))

    today = date.today()
    service = chosen_service(church, request.form.get("service_id"))
    marked, already, left_church, is_child_list, not_found = [], [], [], [], []

    for number in numbers:
        member = Member.query.filter_by(church_id=church.id, member_number=number).first()
        if not member:
            not_found.append(number)
            continue
        if member.left_at:
            left_church.append(member.full_name)
            continue
        if member.is_child:
            is_child_list.append(member.full_name)
            continue
        existing = Attendance.query.filter_by(member_id=member.id, service_date=today, service_id=service.id).first()
        if existing:
            already.append(member.full_name)
            continue
        db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=today, service_id=service.id))
        marked.append(member.full_name)

    if marked:
        db.session.commit()

    if len(numbers) == 1:
        # Keep single-entry messaging exactly as before.
        if marked:
            flash(f"Marked present: {marked[0]} ({numbers[0]}).", "success")
        elif already:
            flash(f"{already[0]} was already marked present today.", "info")
        elif left_church:
            flash(f"{left_church[0]} is marked as no longer in the church. Restore them from Members Who Left first if they're attending again.", "warning")
        elif is_child_list:
            flash(f"{is_child_list[0]} is registered in the Children's church. Mark their attendance from the Children page instead.", "warning")
        else:
            flash(f"No member found with number '{numbers[0]}'. Is this a first timer?", "warning")
        return redirect(url_for("admin_dashboard"))

    if marked:
        flash(f"Marked present: {', '.join(marked)} ({len(marked)}).", "success")
    if already:
        flash(f"Already marked present today: {', '.join(already)}.", "info")
    if left_church:
        flash(f"No longer in the church (restore first if attending again): {', '.join(left_church)}.", "warning")
    if is_child_list:
        flash(f"In the Children's church — mark attendance from the Children page instead: {', '.join(is_child_list)}.", "warning")
    if not_found:
        flash(f"No member found for: {', '.join(not_found)}.", "warning")
    return redirect(url_for("admin_dashboard"))


def _attendance_checklist_view(is_child, redirect_endpoint, template):
    church = current_user.church
    today = date.today()
    label = "child" if is_child else "member"

    if request.method == "POST":
        service = chosen_service(church, request.form.get("service_id"))
        checked_ids = {int(v) for v in request.form.getlist("member_ids") if v.isdigit()}
        shown_ids = {int(v) for v in request.form.getlist("shown_ids") if v.isdigit()}
        already_present_ids = {
            r[0] for r in db.session.query(Attendance.member_id).filter(
                Attendance.church_id == church.id, Attendance.service_date == today,
                Attendance.service_id == service.id,
            ).all()
        }
        to_mark = checked_ids - already_present_ids
        # Members shown on this page that were present but got unticked should be removed.
        to_unmark = (already_present_ids & shown_ids) - checked_ids
        marked_count = 0
        if to_mark:
            # Only mark members that actually belong to this church, are active, and
            # are in the right category — never trust checkbox values from the form alone.
            eligible = Member.query.filter(
                Member.id.in_(to_mark), Member.church_id == church.id,
                Member.left_at.is_(None), Member.is_child.is_(is_child),
            ).all()
            for member in eligible:
                db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=today,
                                          service_id=service.id))
                marked_count += 1

        unmarked_count = 0
        if to_unmark:
            unmarked_count = Attendance.query.filter(
                Attendance.member_id.in_(to_unmark), Attendance.church_id == church.id,
                Attendance.service_date == today, Attendance.service_id == service.id,
            ).delete(synchronize_session=False)

        if marked_count or unmarked_count:
            db.session.commit()

        if marked_count and unmarked_count:
            flash(f"Marked {marked_count} {label}(s) present and removed {unmarked_count} for today.", "success")
        elif marked_count:
            flash(f"Marked {marked_count} {label}(s) present for today.", "success")
        elif unmarked_count:
            flash(f"Removed {unmarked_count} {label}(s) from today's attendance.", "success")
        else:
            flash(f"No changes — attendance for today is unchanged.", "info")
        return redirect(url_for(redirect_endpoint, service_id=service.id))

    service = chosen_service(church, request.args.get("service_id"))
    q = request.args.get("q", "").strip()
    query = Member.query.filter_by(church_id=church.id, left_at=None, is_child=is_child)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Member.full_name.ilike(like), Member.member_number.ilike(like)))
    members = query.order_by(Member.full_name).all()

    present_ids = {
        r[0] for r in db.session.query(Attendance.member_id).filter(
            Attendance.church_id == church.id, Attendance.service_date == today,
            Attendance.service_id == service.id,
        ).all()
    }
    return render_template(
        template, members=members, q=q, present_ids=present_ids, today=today,
        service=service, services=church_services(church), multi=multi_service(church),
    )


@app.route("/admin/attendance/checklist", methods=["GET", "POST"])
@role_required(*STAFF)
def admin_attendance_checklist():
    return _attendance_checklist_view(False, "admin_attendance_checklist", "admin/attendance_checklist.html")


@app.route("/admin/children/attendance/checklist", methods=["GET", "POST"])
@role_required(*STAFF)
@feature_required("children")
def admin_children_checklist():
    return _attendance_checklist_view(True, "admin_children_checklist", "admin/children_checklist.html")


# ---------------------------------------------------------------------------
# QR self check-in
# ---------------------------------------------------------------------------
# The QR code points at /checkin/<church code>/<token>. The token is permanent
# so the code can be printed once and stuck on chairs; the check-in page itself
# always works on today's date. If the link leaks, an admin can replace it,
# which makes every old sticker stop working.

def checkin_token(church):
    if not church.checkin_secret:
        church.checkin_secret = secrets.token_hex(32)
        db.session.commit()
    msg = f"{church.id}:checkin".encode()
    return hmac.new(church.checkin_secret.encode(), msg, hashlib.sha256).hexdigest()[:20]


def normalize_member_number(raw, church_code):
    """Accept 'UP-0001', 'up0001', 'UP 0001' or just '1' / '0001' and return 'UP-0001'."""
    value = re.sub(r"[\s-]+", "", (raw or "").upper())
    if value.isdigit():
        return f"{church_code}-{int(value):04d}"
    if value.startswith(church_code) and value[len(church_code):].isdigit():
        return f"{church_code}-{int(value[len(church_code):]):04d}"
    return (raw or "").strip().upper()


@app.route("/admin/qr-checkin")
@role_required(*STAFF)
@feature_required("qr_checkin")
def admin_qr_checkin():
    church = current_user.church
    checkin_url = church_base_url(church) + url_for(
        "public_checkin", church_code=church.code, token=checkin_token(church),
    )
    return render_template(
        "admin/qr_checkin.html", church=church, checkin_url=checkin_url,
        services=church_services(church), multi=multi_service(church), current_service=current_service(church),
    )


@app.route("/admin/qr-checkin/service", methods=["POST"])
@role_required(*STAFF)
@feature_required("qr_checkin")
def admin_set_qr_service():
    church = current_user.church
    service = chosen_service(church, request.form.get("service_id"))
    church.current_service_id = service.id
    db.session.commit()
    flash(f"QR check-ins now count for {service.name}.", "success")
    return redirect(url_for("admin_qr_checkin"))


@app.route("/admin/qr-checkin/replace", methods=["POST"])
@role_required("admin")
@feature_required("qr_checkin")
def admin_replace_qr():
    church = current_user.church
    church.checkin_secret = secrets.token_hex(32)
    db.session.commit()
    flash("A new QR code was created. The old printed codes no longer work — print and stick up the new one.", "success")
    return redirect(url_for("admin_qr_checkin"))


@app.route("/checkin/<church_code>/<token>", methods=["GET", "POST"])
def public_checkin(church_code, token):
    church = Church.query.filter_by(code=church_code.upper()).first_or_404()
    if (g.tenant and g.tenant.id != church.id) or "qr_checkin" not in church_features(church):
        abort(404)
    today = date.today()
    if not hmac.compare_digest(token, checkin_token(church)):
        return render_template("checkin.html", church=church, expired=True), 410

    if request.method == "POST":
        number = normalize_member_number(request.form.get("member_number"), church.code)
        member = Member.query.filter_by(church_id=church.id, member_number=number).first() if number else None
        if not member or member.left_at:
            flash(f"We couldn't find member number '{number}'. Please check it, or see an usher if you're new.", "danger")
            return redirect(request.path)

        service = current_service(church)
        if not Attendance.query.filter_by(member_id=member.id, service_date=today, service_id=service.id).first():
            db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=today,
                                      service_id=service.id))
            try:
                db.session.commit()
            except IntegrityError:  # double-tap on submit — they're already marked, which is fine
                db.session.rollback()
        # Thank-you screen, which then forwards to the login page. Sign out any
        # admin session on this device first, otherwise /login would bounce
        # straight on to the dashboard instead of showing the login screen.
        logout_user()
        return render_template(
            "checkin.html", church=church, expired=False, today=today,
            done=True, first_name=member.full_name.split()[0],
            service_name=service.name if multi_service(church) else None,
        )

    return render_template(
        "checkin.html", church=church, expired=False, today=today,
        service_name=current_service(church).name if multi_service(church) else None,
    )


def _members_list_view(is_child, endpoint, template):
    church = current_user.church
    q = request.args.get("q", "").strip()
    query = Member.query.filter_by(church_id=church.id, left_at=None, is_child=is_child)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Member.full_name.ilike(like), Member.member_number.ilike(like)))
    members = query.order_by(Member.full_name).all()
    left_count = Member.query.filter(
        Member.church_id == church.id, Member.left_at.isnot(None), Member.is_child.is_(is_child)
    ).count()
    # Numbering is shared across adults and children, so only offer a reset
    # when the WHOLE church (both categories) has no member records at all.
    has_no_members = Member.query.filter_by(church_id=church.id).first() is None
    return render_template(
        template, members=members, q=q, left_count=left_count,
        has_no_members=has_no_members, next_number_preview=f"{church.code}-{church.next_seq:04d}",
    )


@app.route("/admin/members")
@role_required(*STAFF)
def admin_members():
    return _members_list_view(False, "admin_members", "admin/members_list.html")


@app.route("/admin/children")
@role_required(*STAFF)
@feature_required("children")
def admin_children():
    return _members_list_view(True, "admin_children", "admin/children_list.html")


@app.route("/admin/children/move-in", methods=["GET", "POST"])
@role_required("admin")
@feature_required("children")
def admin_move_to_children():
    church = current_user.church

    if request.method == "POST":
        checked_ids = {int(v) for v in request.form.getlist("member_ids") if v.isdigit()}
        moved = 0
        if checked_ids:
            # Only reclassify members that actually belong to this church and are
            # currently adults — never trust checkbox values from the form alone.
            eligible = Member.query.filter(
                Member.id.in_(checked_ids), Member.church_id == church.id,
                Member.left_at.is_(None), Member.is_child.is_(False),
            ).all()
            for member in eligible:
                member.is_child = True
                moved += 1
            if moved:
                db.session.commit()

        if moved:
            flash(f"Moved {moved} member(s) to the Children's church.", "success")
        else:
            flash("No members were selected to move.", "info")
        return redirect(url_for("admin_children"))

    q = request.args.get("q", "").strip()
    query = Member.query.filter_by(church_id=church.id, left_at=None, is_child=False)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Member.full_name.ilike(like), Member.member_number.ilike(like)))
    members = query.order_by(Member.full_name).all()
    return render_template("admin/move_to_children.html", members=members, q=q)


@app.route("/admin/members/reset-numbering", methods=["POST"])
@role_required("admin")
def admin_reset_numbering():
    church = current_user.church
    back_to = "admin_children" if request.form.get("from") == "children" else "admin_members"
    if Member.query.filter_by(church_id=church.id).first():
        flash("Numbering can only be reset when there are no member records at all (active or left, adult or child) for this church.", "danger")
        return redirect(url_for(back_to))
    church.next_seq = 1
    db.session.commit()
    flash(f"Member numbering reset — the next registered member will be {church.code}-0001.", "success")
    return redirect(url_for(back_to))


@app.route("/admin/members/<int:member_id>/move-out", methods=["POST"])
@role_required("admin")
def admin_move_member_out(member_id):
    church = current_user.church
    member = Member.query.filter_by(id=member_id, church_id=church.id).first_or_404()
    member.left_at = datetime.utcnow()
    is_child = member.is_child
    db.session.commit()
    flash(f"{member.full_name} was moved to Members Who Left.", "success")
    return redirect(url_for("admin_children" if is_child else "admin_members"))


@app.route("/admin/members/left")
@role_required("admin")
def admin_members_left():
    church = current_user.church
    members = (
        Member.query.filter(Member.church_id == church.id, Member.left_at.isnot(None))
        .order_by(Member.left_at.desc()).all()
    )
    return render_template("admin/members_left.html", members=members)


@app.route("/admin/members/<int:member_id>/restore", methods=["POST"])
@role_required("admin")
def admin_restore_member(member_id):
    church = current_user.church
    member = Member.query.filter_by(id=member_id, church_id=church.id).first_or_404()
    member.left_at = None
    db.session.commit()
    flash(f"{member.full_name} was restored to the active {'children' if member.is_child else 'member'} list.", "success")
    return redirect(url_for("admin_members_left"))


IMPORT_REQUIRED_COLUMN = "full_name"
IMPORT_OPTIONAL_COLUMNS = [
    "gender", "dob_day", "dob_month", "phone", "email", "address", "status", "member_number",
]
IMPORT_ALLOWED_EXTENSIONS = {"csv", "xlsx"}


def _cell_to_str(value):
    """Normalize a spreadsheet cell to a stripped string (Excel hands back
    whole numbers as floats, e.g. 25.0 for a day-of-month cell)."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def extract_import_rows(file_storage, ext):
    """Read an uploaded .csv or .xlsx file into (fieldnames, [(row_num, row_dict), ...]).
    Returns (None, None, error_message) if the file couldn't be read."""
    raw_bytes = file_storage.read()

    if ext == "csv":
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, None, "Could not read that file. Please save it as UTF-8 CSV and try again."
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
        reader.fieldnames = fieldnames
        rows = [
            (row_num, {k: (v or "").strip() for k, v in row.items()})
            for row_num, row in enumerate(reader, start=2)
        ]
        return fieldnames, rows, None

    # .xlsx
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), read_only=True, data_only=True)
    except Exception:
        return None, None, "Could not read that Excel file. Please make sure it's a valid .xlsx file."
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None)
    if not header:
        return None, None, "That Excel file has no data."
    fieldnames = [_cell_to_str(h).lower() for h in header]
    rows = []
    for row_num, raw_row in enumerate(rows_iter, start=2):
        row = {}
        for idx, key in enumerate(fieldnames):
            if not key:
                continue
            row[key] = _cell_to_str(raw_row[idx]) if idx < len(raw_row) else ""
        rows.append((row_num, row))
    return fieldnames, rows, None


@app.route("/admin/members/import/template")
@role_required("admin")
def admin_import_template():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([IMPORT_REQUIRED_COLUMN] + IMPORT_OPTIONAL_COLUMNS)
    writer.writerow([
        "John Doe", "Male", "25", "August", "08012345678",
        "john@example.com", "12 Church Street", "member", "",
    ])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=member_import_template.csv"
    return resp


@app.route("/admin/members/import", methods=["GET", "POST"])
@role_required("admin")
def admin_import_members():
    church = current_user.church
    results = None

    if request.method == "POST":
        file = request.files.get("import_file")
        if not file or file.filename == "":
            flash("Please choose a CSV or Excel file to upload.", "danger")
            return redirect(url_for("admin_import_members"))
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in IMPORT_ALLOWED_EXTENSIONS:
            flash("Only .csv or .xlsx files are supported.", "danger")
            return redirect(url_for("admin_import_members"))

        fieldnames, rows, error = extract_import_rows(file, ext)
        if error:
            flash(error, "danger")
            return redirect(url_for("admin_import_members"))

        if IMPORT_REQUIRED_COLUMN not in fieldnames:
            flash(f"Your file must include a '{IMPORT_REQUIRED_COLUMN}' column. Download the template below for the expected format.", "danger")
            return render_template("admin/members_import.html", results=None)

        imported = 0
        errors = []
        seen_numbers = set()

        for row_num, row in rows:
            full_name = (row.get("full_name") or "").strip()
            if not full_name:
                errors.append((row_num, "Missing full_name — row skipped."))
                continue

            number = (row.get("member_number") or "").strip().upper()
            if number:
                if number in seen_numbers or Member.query.filter_by(church_id=church.id, member_number=number).first():
                    errors.append((row_num, f"Member number '{number}' is already in use — row skipped."))
                    continue
            else:
                # Keep generating until we land on one that isn't already
                # claimed elsewhere in this same file.
                while True:
                    number = church.next_member_number()
                    if number not in seen_numbers:
                        break
            seen_numbers.add(number)

            status = (row.get("status") or "member").strip().lower()
            if status not in ("member", "first_timer"):
                status = "member"

            member = Member(
                church_id=church.id,
                member_number=number,
                full_name=full_name,
                gender=(row.get("gender") or "").strip(),
                date_of_birth=build_birthday(row.get("dob_day"), row.get("dob_month")),
                phone=(row.get("phone") or "").strip(),
                email=(row.get("email") or "").strip(),
                address=(row.get("address") or "").strip(),
                status=status,
                is_editable=False,
            )
            db.session.add(member)
            try:
                # Commit row-by-row so one bad row can't roll back the whole batch.
                db.session.commit()
                imported += 1
            except IntegrityError:
                db.session.rollback()
                errors.append((row_num, f"Member number '{number}' already exists — row skipped."))

        results = {"imported": imported, "errors": errors}
        if imported:
            flash(f"Imported {imported} member(s).", "success")
        if errors:
            flash(f"{len(errors)} row(s) were skipped — see details below.", "warning")
        if not imported and not errors:
            flash("The file had no data rows to import.", "info")

    return render_template("admin/members_import.html", results=results)


def _new_member_view(is_child, list_endpoint, template):
    church = current_user.church
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Full name is required.", "danger")
            return redirect(request.path)
        dob_day = request.form.get("dob_day", "")
        dob_month = request.form.get("dob_month", "")
        birthday = build_birthday(dob_day, dob_month)
        if (dob_day or dob_month) and birthday is None:
            flash("That birthday day/month combination wasn't valid, so it was left blank.", "warning")
        member = Member(
            church_id=church.id,
            full_name=full_name,
            gender=request.form.get("gender", ""),
            date_of_birth=birthday,
            phone=request.form.get("phone", "").strip(),
            email=request.form.get("email", "").strip(),
            address=request.form.get("address", "").strip(),
            emergency_contact_name=request.form.get("emergency_contact_name", "").strip()[:120] or None,
            emergency_contact_phone=request.form.get("emergency_contact_phone", "").strip()[:50] or None,
            status=request.form.get("status", "first_timer"),
            is_editable=False,
            is_child=is_child,
        )
        member.member_number = church.next_member_number()
        fname = save_upload(request.files.get("photo"))
        if fname:
            member.photo_filename = fname
        db.session.add(member)
        db.session.flush()
        if request.form.get("mark_present"):
            db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=date.today(),
                                      service_id=current_service(church).id))
        db.session.commit()
        flash(f"{member.full_name} registered with number {member.member_number}.", "success")
        return redirect(url_for(list_endpoint))
    return render_template(template, member=None, is_child=is_child)


@app.route("/admin/members/new", methods=["GET", "POST"])
@role_required(*STAFF)
def admin_new_member():
    return _new_member_view(False, "admin_members", "admin/member_form.html")


@app.route("/admin/children/new", methods=["GET", "POST"])
@role_required(*STAFF)
@feature_required("children")
def admin_new_child():
    return _new_member_view(True, "admin_children", "admin/child_form.html")


def _edit_member_view(member_id, is_child, list_endpoint, edit_endpoint, template):
    church = current_user.church
    member = Member.query.filter_by(id=member_id, church_id=church.id, is_child=is_child).first_or_404()
    if request.method == "POST":
        if not member.is_editable:
            flash("This member's details are locked. Enable editing first.", "danger")
            return redirect(url_for(edit_endpoint, member_id=member.id))
        member.full_name = request.form.get("full_name", "").strip() or member.full_name
        member.gender = request.form.get("gender", "")
        dob_day = request.form.get("dob_day", "")
        dob_month = request.form.get("dob_month", "")
        birthday = build_birthday(dob_day, dob_month)
        if (dob_day or dob_month) and birthday is None:
            flash("That birthday day/month combination wasn't valid, so it was left blank.", "warning")
        member.date_of_birth = birthday
        member.phone = request.form.get("phone", "").strip()
        member.email = request.form.get("email", "").strip()
        member.address = request.form.get("address", "").strip()
        if "emergency_contact_name" in request.form:  # the children's form has no such fields
            member.emergency_contact_name = request.form.get("emergency_contact_name", "").strip()[:120] or None
            member.emergency_contact_phone = request.form.get("emergency_contact_phone", "").strip()[:50] or None
        fname = save_upload(request.files.get("photo"))
        if fname:
            member.photo_filename = fname
        db.session.commit()
        flash("Member details updated.", "success")
        return redirect(url_for(list_endpoint))
    return render_template(template, member=member, is_child=is_child)


@app.route("/admin/members/<int:member_id>/edit", methods=["GET", "POST"])
@role_required(*STAFF)
def admin_edit_member(member_id):
    return _edit_member_view(member_id, False, "admin_members", "admin_edit_member", "admin/member_form.html")


@app.route("/admin/children/<int:member_id>/edit", methods=["GET", "POST"])
@role_required(*STAFF)
@feature_required("children")
def admin_edit_child(member_id):
    return _edit_member_view(member_id, True, "admin_children", "admin_edit_child", "admin/child_form.html")


@app.route("/admin/members/<int:member_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_member(member_id):
    church = current_user.church
    member = Member.query.filter_by(id=member_id, church_id=church.id).first_or_404()
    name = member.full_name
    is_child = member.is_child
    Attendance.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    AbsenteeEmail.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    AbsenteeSMS.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    MessageLog.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    DepartmentMember.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    MeetingAttendance.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    LeaveRequest.query.filter_by(member_id=member.id).delete(synchronize_session=False)
    # Keep their program registrations as visitor-style records, but unlink the deleted member.
    ProgramRegistration.query.filter_by(member_id=member.id).update({"member_id": None}, synchronize_session=False)
    db.session.delete(member)
    db.session.commit()
    flash(f"{name} was removed from the church list.", "success")
    return redirect(url_for("admin_children" if is_child else "admin_members"))


DEFAULT_ABSENTEE_SUBJECT = "We missed you at {church}"
DEFAULT_ABSENTEE_BODY = (
    "Dear {first_name},\n\n"
    "We noticed you haven't been with us at {church} recently, and we just wanted "
    "you to know that you were missed. We hope all is well with you and your family.\n\n"
    "If there's anything you'd like us to pray about with you, or any way we can help, "
    "please reply to this email — we'd love to hear from you.\n\n"
    "We look forward to seeing you soon.\n\n"
    "God bless,\n{pastor}\n{church}"
)
DEFAULT_ABSENTEE_SMS = (
    "Dear {first_name}, we missed you at {church} recently. We hope all is well "
    "with you. We look forward to seeing you soon. God bless, {pastor}"
)
SMS_MAX_CHARS = 612  # eBulkSMS limit: 4 pages
EMAIL_PLACEHOLDERS = ["{name}", "{first_name}", "{member_number}", "{church}", "{pastor}", "{last_attended}"]


def mail_configured():
    return bool(MAIL_SERVER and MAIL_FROM)


def sms_configured():
    return bool(EBULKSMS_USERNAME and EBULKSMS_API_KEY)


def sms_sender_name(church):
    """eBulkSMS allows at most 11 letters/digits; fall back to the church name."""
    name = EBULKSMS_SENDER or re.sub(r"[^A-Za-z0-9 ]", "", church.name)
    return " ".join(name.split())[:11].strip() or "Church"


def normalize_ng_phone(phone):
    """Turn '0803 123 4567' / '+2348031234567' / '2348031234567' into '2348031234567', else None."""
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    elif len(digits) == 10 and digits[0] in "789":
        digits = "234" + digits
    return digits if digits.startswith("234") and len(digits) == 13 else None


# Statuses that mean the whole account is unusable, so there's no point
# trying the next member — stop the batch and tell the admin why.
SMS_GATEWAY_ERRORS = {
    "AUTH_FAILURE": "eBulkSMS rejected the username / API key.",
    "MISSING_USERNAME": "The eBulkSMS username isn't set.",
    "MISSING_APIKEY": "The eBulkSMS API key isn't set.",
    "INSUFFICIENT_CREDIT": "The eBulkSMS account is out of credit — top up and press Send SMS again.",
    "INVALID_SENDER": "eBulkSMS rejected the sender name (max 11 letters/digits, no symbols).",
    "MISSING_SENDER": "eBulkSMS rejected the sender name (max 11 letters/digits, no symbols).",
}


def send_ebulksms(sender, phone, message):
    """Send one SMS. Returns the eBulkSMS status string ('SUCCESS' on success)."""
    payload = {"SMS": {
        "auth": {"username": EBULKSMS_USERNAME, "apikey": EBULKSMS_API_KEY},
        "message": {"sender": sender, "messagetext": message, "flash": "0"},
        "recipients": {"gsm": [{"msidn": phone, "msgid": uuid.uuid4().hex[:30]}]},
        "dndsender": 1 if EBULKSMS_DND else 0,
    }}
    req = urllib.request.Request(
        EBULKSMS_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    return (data.get("response") or {}).get("status", "UNKNOWN_ERROR")


def fill_email_template(template, member, church, last_attended):
    # Plain replace (not str.format) so a stray "{" typed by an admin can't break sending.
    values = {
        "{name}": member.full_name,
        "{first_name}": member.full_name.split()[0],
        "{member_number}": member.member_number,
        "{church}": church.name,
        "{pastor}": church.pastor_name or f"The {church.name} family",
        "{last_attended}": last_attended.strftime("%d %B %Y") if last_attended else "a while ago",
    }
    for key, val in values.items():
        template = template.replace(key, val)
    return template


def _absentees(church, start, end, days=(), service_ids=()):
    """[(member, last_attended_before_start), ...] for adults with no attendance in [start, end].

    With days / service_ids given, only attendance on those weekdays / at those services counts,
    so someone who came only on a Sunday is absent from a Wednesday-only report."""
    attended_query = db.session.query(Attendance.member_id).filter(
        Attendance.church_id == church.id,
        Attendance.service_date >= start,
        Attendance.service_date <= end,
    )
    attended_ids = {r[0] for r in apply_attendance_filter(attended_query, days, service_ids).all()}
    absentees = Member.query.filter_by(church_id=church.id, left_at=None, is_child=False).filter(
        ~Member.id.in_(attended_ids) if attended_ids else True
    ).order_by(Member.full_name).all()

    results = []
    for m in absentees:
        last = db.session.query(func.max(Attendance.service_date)).filter(
            Attendance.member_id == m.id, Attendance.service_date < start
        ).scalar()
        results.append((m, last))
    return results


@app.route("/admin/reports/absentees")
@role_required(*STAFF)
def admin_absentee_report():
    church = current_user.church
    today = date.today()
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    results = None
    emailed = {}
    texted = {}

    days, picked = attendance_filter(church)
    if start and end:
        results = _absentees(church, start, end, days, picked)
        emailed = {
            e.member_id: e.sent_at for e in AbsenteeEmail.query.filter_by(
                church_id=church.id, period_start=start, period_end=end,
            ).all()
        }
        texted = {
            t.member_id: t.sent_at for t in AbsenteeSMS.query.filter_by(
                church_id=church.id, period_start=start, period_end=end,
            ).all()
        }

        if request.args.get("export"):
            if current_user.role != "admin":
                abort(403)
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Member Number", "Full Name", "Phone", "Email", "Last Attended"])
            for m, last in results:
                writer.writerow([m.member_number, m.full_name, m.phone or "", m.email or "",
                                  last.strftime("%Y-%m-%d") if last else "Never"])
            resp = Response(buf.getvalue(), mimetype="text/csv")
            resp.headers["Content-Disposition"] = f"attachment; filename=absentees_{start}_{end}.csv"
            return resp
    else:
        start = start or today.replace(day=1)
        end = end or today

    return render_template(
        "admin/absentee_report.html",
        start=start.isoformat(), end=end.isoformat(), results=results, emailed=emailed, texted=texted,
        mail_ready=mail_configured(), placeholders=EMAIL_PLACEHOLDERS,
        email_subject=church.absentee_email_subject or DEFAULT_ABSENTEE_SUBJECT,
        email_body=church.absentee_email_body or DEFAULT_ABSENTEE_BODY,
        sms_ready=sms_configured(), sms_body=church.absentee_sms_body or DEFAULT_ABSENTEE_SMS,
        sms_max=SMS_MAX_CHARS, sms_sender=sms_sender_name(church),
        days=days, picked=picked, services=church_services(church), weekday_names=WEEKDAY_NAMES,
        filter_on="services" in church_features(church),
    )


# Keep each request well under gunicorn's 30s worker timeout. Anything left
# over is picked up by pressing Send again — already-emailed members are skipped.
EMAIL_BATCH_SECONDS = 20


@app.route("/admin/reports/absentees/email", methods=["POST"])
@role_required("admin")
@feature_required("absentee_email")
def admin_email_absentees():
    church = current_user.church
    start = parse_date(request.form.get("start"))
    end = parse_date(request.form.get("end"))
    if not (start and end):
        flash("Generate the absentee report first.", "danger")
        return redirect(url_for("admin_absentee_report"))
    back = redirect(url_for("admin_absentee_report", start=start.isoformat(), end=end.isoformat()))

    subject = request.form.get("subject", "").strip()
    body = request.form.get("body", "").strip()
    if not subject or not body:
        flash("The email needs both a subject and a message.", "danger")
        return back
    church.absentee_email_subject = subject
    church.absentee_email_body = body
    db.session.commit()

    if not mail_configured():
        flash("Email isn't set up on the server yet (MAIL_SERVER / MAIL_USERNAME / MAIL_PASSWORD). Your message was saved.", "warning")
        return back

    selected = {int(v) for v in request.form.getlist("member_ids") if v.isdigit()}
    already = {
        r[0] for r in db.session.query(AbsenteeEmail.member_id).filter_by(
            church_id=church.id, period_start=start, period_end=end,
        ).all()
    }
    # Recompute the absentee list server-side — never trust ticked ids alone.
    targets = [
        (m, last) for m, last in _absentees(church, start, end)
        if m.id in selected and m.id not in already and m.email
    ]
    if not targets:
        flash("No one to email — select absentees who have an email address and haven't been emailed for this period.", "info")
        return back

    sent, failed = 0, []
    deadline = time.monotonic() + EMAIL_BATCH_SECONDS
    try:
        if MAIL_USE_SSL:
            smtp = smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT, timeout=15)
        else:
            smtp = smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=15)
            smtp.starttls()
        if MAIL_USERNAME:
            smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
    except (smtplib.SMTPException, OSError) as exc:
        app.logger.exception("SMTP connection failed")
        flash(f"Couldn't connect to the mail server: {exc}", "danger")
        return back

    with smtp:
        for member, last in targets:
            if time.monotonic() > deadline:
                break
            msg = EmailMessage()
            msg["Subject"] = fill_email_template(subject, member, church, last)
            msg["From"] = f"{church.name} <{MAIL_FROM}>"
            msg["To"] = member.email
            if church.email:
                msg["Reply-To"] = church.email
            msg.set_content(fill_email_template(body, member, church, last))
            try:
                smtp.send_message(msg)
            except (smtplib.SMTPException, OSError) as exc:
                app.logger.warning("Absentee email to %s failed: %s", member.email, exc)
                failed.append(member.full_name)
                continue
            db.session.add(AbsenteeEmail(
                member_id=member.id, church_id=church.id, period_start=start, period_end=end,
            ))
            db.session.commit()  # record each send immediately so a crash can't cause a re-send
            sent += 1

    remaining = len(targets) - sent - len(failed)
    if sent:
        flash(f"Sent {sent} email(s).", "success")
    if failed:
        flash(f"Couldn't send to: {', '.join(failed)}. Check their email addresses.", "danger")
    if remaining:
        flash(f"{remaining} email(s) still to go — press Send again to continue (nobody gets it twice).", "warning")
    return back


@app.route("/admin/reports/absentees/sms", methods=["POST"])
@role_required("admin")
@feature_required("absentee_sms")
def admin_sms_absentees():
    church = current_user.church
    start = parse_date(request.form.get("start"))
    end = parse_date(request.form.get("end"))
    if not (start and end):
        flash("Generate the absentee report first.", "danger")
        return redirect(url_for("admin_absentee_report"))
    back = redirect(url_for("admin_absentee_report", start=start.isoformat(), end=end.isoformat()))

    body = request.form.get("sms_body", "").strip()
    if not body:
        flash("The SMS needs a message.", "danger")
        return back
    church.absentee_sms_body = body
    db.session.commit()

    if not sms_configured():
        flash("SMS isn't set up on the server yet (EBULKSMS_USERNAME / EBULKSMS_API_KEY). Your message was saved.", "warning")
        return back

    selected = {int(v) for v in request.form.getlist("member_ids") if v.isdigit()}
    already = {
        r[0] for r in db.session.query(AbsenteeSMS.member_id).filter_by(
            church_id=church.id, period_start=start, period_end=end,
        ).all()
    }
    # Recompute the absentee list server-side — never trust ticked ids alone.
    targets = [
        (m, last) for m, last in _absentees(church, start, end)
        if m.id in selected and m.id not in already and m.phone
    ]
    if not targets:
        flash("No one to text — select absentees who have a phone number and haven't been texted for this period.", "info")
        return back

    sender = sms_sender_name(church)
    sent, failed = 0, []
    deadline = time.monotonic() + EMAIL_BATCH_SECONDS
    for member, last in targets:
        if time.monotonic() > deadline:
            break
        phone = normalize_ng_phone(member.phone)
        if not phone:
            failed.append(f"{member.full_name} (invalid number)")
            continue
        message = fill_email_template(body, member, church, last)[:SMS_MAX_CHARS]
        try:
            status = send_ebulksms(sender, phone, message)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            app.logger.exception("eBulkSMS request failed")
            flash(f"Couldn't reach eBulkSMS: {exc}", "danger")
            break
        if status in SMS_GATEWAY_ERRORS:
            flash(SMS_GATEWAY_ERRORS[status], "danger")
            break
        if status != "SUCCESS":
            app.logger.warning("Absentee SMS to %s failed: %s", phone, status)
            failed.append(f"{member.full_name} ({status.replace('_', ' ').lower()})")
            continue
        db.session.add(AbsenteeSMS(
            member_id=member.id, church_id=church.id, period_start=start, period_end=end,
        ))
        db.session.commit()  # record each send immediately so a crash can't cause a re-send
        sent += 1

    remaining = len(targets) - sent - len(failed)
    if sent:
        flash(f"Sent {sent} SMS.", "success")
    if failed:
        flash(f"Couldn't text: {', '.join(failed)}.", "danger")
    if remaining:
        flash(f"{remaining} SMS still to go — press Send SMS again to continue (nobody gets it twice).", "warning")
    return back


# --- Birthday and first-timer messages ---------------------------------------
# Nothing here sends by itself. A staff member (admin or user) ticks the people and presses
# Send. Only the admin can change the wording; users send the saved wording.

MESSAGE_DEFAULTS = {
    ("birthday", "sms"): (None, "Happy birthday, {first_name}! Everyone at {church} celebrates you today. "
                                "May God bless you with another year of joy and favour. {pastor}"),
    ("birthday", "email"): ("Happy birthday, {first_name}!",
                            "Dear {first_name},\n\nEveryone at {church} wishes you a very happy birthday. "
                            "We thank God for your life and pray that He fills the year ahead with joy, health "
                            "and favour.\n\nGod bless you,\n{pastor}\n{church}"),
    ("first_timer", "sms"): (None, "Dear {first_name}, thank you for worshipping with us at {church}. "
                                   "We would love to see you again. God bless, {pastor}"),
    ("first_timer", "email"): ("Welcome to {church}, {first_name}",
                               "Dear {first_name},\n\nIt was a joy to have you with us at {church}. We hope you "
                               "felt at home, and we would love to see you again.\n\nIf there is anything we can "
                               "pray about with you, please reply to this email.\n\nGod bless you,\n{pastor}\n{church}"),
}
MESSAGE_PLACEHOLDERS = ["{name}", "{first_name}", "{member_number}", "{church}", "{pastor}"]


def message_template(church, kind, channel):
    """(subject, body) saved for this church, or the built-in wording."""
    row = MessageTemplate.query.filter_by(church_id=church.id, kind=kind, channel=channel).first()
    if row:
        return row.subject or "", row.body
    subject, body = MESSAGE_DEFAULTS[(kind, channel)]
    return subject or "", body


def message_templates(church, kind):
    sms = message_template(church, kind, "sms")
    email = message_template(church, kind, "email")
    return {"sms_body": sms[1], "email_subject": email[0], "email_body": email[1]}


def save_message_templates(church, kind, form):
    """Save the wording typed on the page. Empty fields are ignored."""
    wanted = {
        "sms": (None, form.get("sms_body", "").strip()[:SMS_MAX_CHARS]),
        "email": (form.get("email_subject", "").strip()[:200], form.get("email_body", "").strip()),
    }
    for channel, (subject, body) in wanted.items():
        if not body or (channel == "email" and not subject):
            continue
        row = MessageTemplate.query.filter_by(church_id=church.id, kind=kind, channel=channel).first()
        if row is None:
            row = MessageTemplate(church_id=church.id, kind=kind, channel=channel, body=body)
            db.session.add(row)
        row.subject, row.body = subject, body
    db.session.commit()


def deliver_messages(church, kind, channel, members, period_key):
    """Send each member the church's saved message by email or SMS and record every send.

    Returns [(flash category, text)]. Anyone already sent this message for this period is skipped."""
    subject, body = message_template(church, kind, channel)
    word = "email" if channel == "email" else "SMS"
    if channel == "email" and not mail_configured():
        return [("warning", "Email isn't set up on the server yet (MAIL_SERVER / MAIL_USERNAME / MAIL_PASSWORD).")]
    if channel == "sms" and not sms_configured():
        return [("warning", "SMS isn't set up on the server yet (EBULKSMS_USERNAME / EBULKSMS_API_KEY).")]

    contact = (lambda m: m.email) if channel == "email" else (lambda m: m.phone)
    done = {
        r[0] for r in db.session.query(MessageLog.member_id).filter_by(
            church_id=church.id, kind=kind, channel=channel, period_key=period_key,
        ).all()
    }
    already = [m.full_name for m in members if m.id in done]
    no_contact = [m.full_name for m in members if m.id not in done and not contact(m)]
    targets = [m for m in members if m.id not in done and contact(m)]

    notes, sent, failed, fatal = [], 0, [], False
    deadline = time.monotonic() + EMAIL_BATCH_SECONDS

    def record(member):
        db.session.add(MessageLog(
            church_id=church.id, member_id=member.id, kind=kind, channel=channel,
            period_key=period_key, sent_by=current_user.id,
        ))
        db.session.commit()  # record each send immediately so a crash can't cause a re-send

    if targets and channel == "email":
        try:
            if MAIL_USE_SSL:
                smtp = smtplib.SMTP_SSL(MAIL_SERVER, MAIL_PORT, timeout=15)
            else:
                smtp = smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=15)
                smtp.starttls()
            if MAIL_USERNAME:
                smtp.login(MAIL_USERNAME, MAIL_PASSWORD)
        except (smtplib.SMTPException, OSError) as exc:
            app.logger.exception("SMTP connection failed")
            return [("danger", f"Couldn't connect to the mail server: {exc}")]
        with smtp:
            for member in targets:
                if time.monotonic() > deadline:
                    break
                msg = EmailMessage()
                msg["Subject"] = fill_email_template(subject, member, church, None)
                msg["From"] = f"{church.name} <{MAIL_FROM}>"
                msg["To"] = member.email
                if church.email:
                    msg["Reply-To"] = church.email
                msg.set_content(fill_email_template(body, member, church, None))
                try:
                    smtp.send_message(msg)
                except (smtplib.SMTPException, OSError) as exc:
                    app.logger.warning("%s email to %s failed: %s", kind, member.email, exc)
                    failed.append(member.full_name)
                    continue
                record(member)
                sent += 1
    elif targets:
        sender = sms_sender_name(church)
        for member in targets:
            if time.monotonic() > deadline:
                break
            phone = normalize_ng_phone(member.phone)
            if not phone:
                failed.append(f"{member.full_name} (invalid number)")
                continue
            try:
                status = send_ebulksms(sender, phone, fill_email_template(body, member, church, None)[:SMS_MAX_CHARS])
            except (urllib.error.URLError, OSError, ValueError) as exc:
                app.logger.exception("eBulkSMS request failed")
                notes.append(("danger", f"Couldn't reach eBulkSMS: {exc}"))
                fatal = True
                break
            if status in SMS_GATEWAY_ERRORS:
                notes.append(("danger", SMS_GATEWAY_ERRORS[status]))
                fatal = True
                break
            if status != "SUCCESS":
                app.logger.warning("%s SMS to %s failed: %s", kind, phone, status)
                failed.append(f"{member.full_name} ({status.replace('_', ' ').lower()})")
                continue
            record(member)
            sent += 1

    if sent:
        notes.insert(0, ("success", f"Sent {sent} {word}."))
    if failed:
        notes.append(("danger", f"Couldn't send to: {', '.join(failed)}."))
    if no_contact:
        notes.append(("warning", f"No {'email address' if channel == 'email' else 'phone number'} for: {', '.join(no_contact)}."))
    if already:
        notes.append(("info", f"Already sent this one, so skipped: {', '.join(already)}."))
    remaining = len(targets) - sent - len(failed)
    if remaining and not fatal:
        notes.append(("warning", f"{remaining} still to go. Press Send again to continue; nobody gets it twice."))
    if not notes:
        notes.append(("info", "Nobody to send to."))
    return notes


def _selected_ids(form):
    return {int(v) for v in form.getlist("member_ids") if v.isdigit()}


def birthday_members(church, when):
    """Adult members whose birthday falls today, in the next 7 days, or this month."""
    today = date.today()
    if when == "week":
        window = {((today + timedelta(days=i)).month, (today + timedelta(days=i)).day) for i in range(7)}
        keep = lambda dob: (dob.month, dob.day) in window
    elif when == "month":
        keep = lambda dob: dob.month == today.month
    else:
        keep = lambda dob: (dob.month, dob.day) == (today.month, today.day)
    members = Member.query.filter(
        Member.church_id == church.id, Member.left_at.is_(None), Member.is_child.is_(False),
        Member.date_of_birth.isnot(None),
    ).all()
    return sorted((m for m in members if keep(m.date_of_birth)), key=lambda m: (m.date_of_birth.month, m.date_of_birth.day, m.full_name))


@app.route("/admin/birthdays")
@role_required(*STAFF)
@feature_required("birthday_messages")
def admin_birthdays():
    church = current_user.church
    when = request.args.get("when", "today")
    if when not in ("today", "week", "month"):
        when = "today"
    sent = {
        (r.member_id, r.channel) for r in MessageLog.query.filter_by(
            church_id=church.id, kind="birthday", period_key=str(date.today().year)).all()
    }
    return render_template(
        "admin/birthdays.html", members=birthday_members(church, when), when=when, sent=sent,
        templates=message_templates(church, "birthday"), sms_ready=sms_configured(), mail_ready=mail_configured(),
        placeholders=MESSAGE_PLACEHOLDERS, sms_max=SMS_MAX_CHARS,
    )


@app.route("/admin/birthdays/send", methods=["POST"])
@role_required(*STAFF)
@feature_required("birthday_messages")
def admin_send_birthdays():
    church = current_user.church
    when = request.form.get("when", "today")
    back = redirect(url_for("admin_birthdays", when=when))
    action = request.form.get("action")
    if current_user.role == "admin":
        save_message_templates(church, "birthday", request.form)  # users send the saved wording; they cannot change it
        if action == "save":
            flash("Birthday wording saved.", "success")
            return back
    elif action == "save":
        abort(403)
    if action not in ("sms", "email"):
        return back
    eligible = {m.id: m for m in birthday_members(church, when)}
    members = [eligible[i] for i in sorted(_selected_ids(request.form)) if i in eligible]
    if not members:
        flash("Tick at least one person first.", "warning")
        return back
    for category, text in deliver_messages(church, "birthday", action, members, str(date.today().year)):
        flash(text, category)
    return back


@app.route("/admin/first-timers/message", methods=["POST"])
@role_required(*STAFF)
@feature_required("first_timers")
@feature_required("first_timer_messages")
def admin_message_first_timers():
    church = current_user.church
    back = redirect(url_for("admin_first_timers"))
    action = request.form.get("action")
    if current_user.role == "admin":
        save_message_templates(church, "first_timer", request.form)
        if action == "save":
            flash("First-timer wording saved.", "success")
            return back
    elif action == "save":
        abort(403)
    if action not in ("sms", "email"):
        return back
    rows, _promoted = first_timer_rows(church)
    eligible = {r["member"].id: r["member"] for r in rows}
    members = [eligible[i] for i in sorted(_selected_ids(request.form)) if i in eligible]
    if not members:
        flash("Tick at least one first timer first.", "warning")
        return back
    for category, text in deliver_messages(church, "first_timer", action, members, date.today().isoformat()):
        flash(text, category)
    return back


@app.route("/admin/church-profile")
@role_required(*STAFF)
def admin_church_profile():
    church = current_user.church
    return render_template("admin/church_profile.html", church=church)


# --- First-timer tracking ----------------------------------------------------
# A first timer is followed for `first_timer_months` from their start date.
# Once that period is over they are promoted to permanent member if they have
# attended at least `first_timer_min_services` services since they started;
# otherwise they stay on the list flagged "needs follow-up" and are promoted
# the moment they reach the minimum.

DEFAULT_FIRST_TIMER_MONTHS = 3
DEFAULT_FIRST_TIMER_MIN_SERVICES = 4


def add_months(d, months):
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def first_timer_rows(church):
    """Promote first timers who qualify, then describe everyone still being tracked."""
    months = church.first_timer_months or DEFAULT_FIRST_TIMER_MONTHS
    minimum = church.first_timer_min_services or DEFAULT_FIRST_TIMER_MIN_SERVICES
    today = date.today()
    members = Member.query.filter_by(
        church_id=church.id, status="first_timer", left_at=None, is_child=False,
    ).order_by(Member.joined_at.desc()).all()

    rows, promoted = [], []
    for m in members:
        if m.first_timer_start is None:
            m.first_timer_start = (m.joined_at or datetime.utcnow()).date()
        end = add_months(m.first_timer_start, months)
        attended = Attendance.query.filter(
            Attendance.member_id == m.id, Attendance.service_date >= m.first_timer_start,
        ).count()
        if today >= end and attended >= minimum:
            m.status = "member"
            m.promoted_at = datetime.utcnow()
            promoted.append(m.full_name)
            continue
        rows.append({
            "member": m, "start": m.first_timer_start, "end": end, "attended": attended,
            "minimum": minimum, "days_left": (end - today).days if end >= today else None,
            "needs_followup": today > end,
        })
    db.session.commit()
    return rows, promoted


@app.route("/admin/first-timers")
@role_required(*STAFF)
@feature_required("first_timers")
def admin_first_timers():
    church = current_user.church
    rows, promoted = first_timer_rows(church)
    if promoted:
        flash(f"Promoted to permanent member: {', '.join(promoted)}.", "success")
    only_followup = request.args.get("filter") == "followup"
    if only_followup:
        rows = [r for r in rows if r["needs_followup"]]
    messaging = "first_timer_messages" in church_features(church)
    today_key = date.today().isoformat()
    sent_today = {
        (r.member_id, r.channel) for r in MessageLog.query.filter_by(
            church_id=church.id, kind="first_timer", period_key=today_key).all()
    } if messaging else set()
    return render_template(
        "admin/first_timers.html", rows=rows, only_followup=only_followup,
        months=church.first_timer_months or DEFAULT_FIRST_TIMER_MONTHS,
        minimum=church.first_timer_min_services or DEFAULT_FIRST_TIMER_MIN_SERVICES,
        messaging=messaging, sent=sent_today,
        templates=message_templates(church, "first_timer") if messaging else None,
        sms_ready=sms_configured(), mail_ready=mail_configured(),
        placeholders=MESSAGE_PLACEHOLDERS, sms_max=SMS_MAX_CHARS,
    )


@app.route("/admin/first-timers/<int:member_id>/promote", methods=["POST"])
@role_required("admin")
@feature_required("first_timers")
def admin_promote_first_timer(member_id):
    member = Member.query.filter_by(
        id=member_id, church_id=current_user.church_id, status="first_timer", is_child=False,
    ).first_or_404()
    member.status = "member"
    member.promoted_at = datetime.utcnow()
    db.session.commit()
    flash(f"{member.full_name} is now a permanent member.", "success")
    return redirect(url_for("admin_first_timers"))


@app.route("/admin/first-timers/settings", methods=["POST"])
@role_required("admin")
@feature_required("first_timers")
def admin_first_timer_settings():
    try:
        months = int(request.form.get("months", ""))
        minimum = int(request.form.get("minimum", ""))
    except ValueError:
        months = minimum = 0
    if not (1 <= months <= 24 and 1 <= minimum <= 100):
        flash("Enter whole numbers: 1 to 24 months and 1 to 100 services.", "danger")
    else:
        current_user.church.first_timer_months = months
        current_user.church.first_timer_min_services = minimum
        db.session.commit()
        flash("First-timer settings saved.", "success")
    return redirect(url_for("admin_first_timers"))


# --- Services and the attendance report --------------------------------------

def _weekday_or_none(raw):
    return int(raw) if str(raw).isdigit() and 0 <= int(raw) <= 6 else None


@app.route("/admin/services", methods=["GET", "POST"])
@role_required("admin")
@feature_required("services")
def admin_services():
    church = current_user.church
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        if not name:
            flash("Give the service a name.", "danger")
        elif any(s.name.lower() == name.lower() for s in church_services(church)):
            flash("You already have a service with that name.", "danger")
        else:
            db.session.add(Service(church_id=church.id, name=name, weekday=_weekday_or_none(request.form.get("weekday"))))
            db.session.commit()
            flash(f"Service '{name}' added.", "success")
        return redirect(url_for("admin_services"))
    services = church_services(church)
    used = {r[0] for r in db.session.query(Attendance.service_id).filter_by(church_id=church.id).distinct().all()}
    return render_template("admin/services.html", services=services, used=used, weekday_names=WEEKDAY_NAMES,
                           current_service=current_service(church))


@app.route("/admin/services/<int:service_id>/rename", methods=["POST"])
@role_required("admin")
@feature_required("services")
def admin_rename_service(service_id):
    service = Service.query.filter_by(id=service_id, church_id=current_user.church_id).first_or_404()
    name = request.form.get("name", "").strip()[:100]
    if name:
        service.name = name
        service.weekday = _weekday_or_none(request.form.get("weekday"))
        db.session.commit()
        flash("Service updated.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@role_required("admin")
@feature_required("services")
def admin_delete_service(service_id):
    church = current_user.church
    service = Service.query.filter_by(id=service_id, church_id=church.id).first_or_404()
    if Attendance.query.filter_by(service_id=service.id).first() or ServiceLog.query.filter_by(service_id=service.id).first():
        flash("This service has attendance or service records, so it can't be deleted. Rename it instead.", "warning")
    elif len(church_services(church)) <= 1:
        flash("A church needs at least one service.", "warning")
    else:
        if church.current_service_id == service.id:
            church.current_service_id = None
        db.session.delete(service)
        db.session.commit()
        flash("Service deleted.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/reports/attendance")
@role_required(*STAFF)
@feature_required("services")
def admin_attendance_report():
    church = current_user.church
    today = date.today()
    start = parse_date(request.args.get("start"), today - timedelta(days=30))
    end = parse_date(request.args.get("end"), today)
    days, picked = attendance_filter(church)
    services = {s.id: s for s in church_services(church)}

    base = db.session.query(Attendance).join(Member).filter(
        Attendance.church_id == church.id, Member.is_child.is_(False),
        Attendance.service_date >= start, Attendance.service_date <= end,
    )
    records = apply_attendance_filter(base, days, picked).order_by(
        Attendance.service_date.desc(), Attendance.service_id).all()

    # One row per date and service, then one per member.
    per_service, per_member = {}, {}
    for a in records:
        per_service[(a.service_date, a.service_id)] = per_service.get((a.service_date, a.service_id), 0) + 1
        per_member.setdefault(a.member_id, set()).add((a.service_date, a.service_id))
    service_rows = [
        {"date": d, "weekday": WEEKDAY_NAMES[d.weekday()], "service": services[sid].name, "present": n}
        for (d, sid), n in sorted(per_service.items(), reverse=True)
    ]
    members = {m.id: m for m in Member.query.filter(Member.id.in_(list(per_member))).all()} if per_member else {}
    member_rows = sorted(
        ({"member": members[mid], "count": len(rows)} for mid, rows in per_member.items() if mid in members),
        key=lambda r: (-r["count"], r["member"].full_name),
    )

    if request.args.get("export"):
        if current_user.role != "admin":
            abort(403)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Date", "Day", "Service", "Member Number", "Full Name"])
        for a in records:
            writer.writerow([a.service_date.isoformat(), WEEKDAY_NAMES[a.service_date.weekday()],
                             services[a.service_id].name, a.member.member_number, a.member.full_name])
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f"attachment; filename=attendance_{start}_{end}.csv"
        return resp

    return render_template(
        "admin/attendance_report.html", start=start.isoformat(), end=end.isoformat(), days=days, picked=picked,
        service_rows=service_rows, member_rows=member_rows, total_checkins=len(records),
        services=list(services.values()), weekday_names=WEEKDAY_NAMES,
    )


# --- Service Analytics: the service log and its report -----------------------
# One record per service: sermon topic, Bible passages, preacher, and the minister who handled
# each segment. The segments (opening prayer, intercession and so on) are the church's own list.

DEFAULT_SEGMENTS = [
    "Opening Prayer / Call to Worship",
    "Intercession / Warfare Sessions",
    "Testimony Session Coordination",
    "Pastoral Announcements & Church Notices",
]


def church_segments(church):
    segments = ServiceSegment.query.filter_by(church_id=church.id).order_by(ServiceSegment.id).all()
    if not segments:
        segments = [ServiceSegment(church_id=church.id, name=n) for n in DEFAULT_SEGMENTS]
        db.session.add_all(segments)
        db.session.commit()
    return segments


def _own_log_or_404(log_id):
    return ServiceLog.query.filter_by(id=log_id, church_id=current_user.church_id).first_or_404()


def log_attendance_counts(church, start, end):
    """{(date, service id): people present}, adults counted once per service."""
    rows = db.session.query(
        Attendance.service_date, Attendance.service_id, func.count(func.distinct(Attendance.member_id)),
    ).join(Member).filter(
        Attendance.church_id == church.id, Member.is_child.is_(False),
        Attendance.service_date >= start, Attendance.service_date <= end,
    ).group_by(Attendance.service_date, Attendance.service_id).all()
    return {(d, sid): n for d, sid, n in rows}


def search_logs(church, start, end, q=""):
    query = ServiceLog.query.filter(
        ServiceLog.church_id == church.id, ServiceLog.service_date >= start, ServiceLog.service_date <= end)
    if q:
        like = f"%{q}%"
        by_minister = db.session.query(ServiceLogMinister.log_id).filter(ServiceLogMinister.minister.ilike(like))
        query = query.filter(db.or_(
            ServiceLog.sermon_topic.ilike(like), ServiceLog.bible_passages.ilike(like),
            ServiceLog.preacher.ilike(like), ServiceLog.notes.ilike(like), ServiceLog.id.in_(by_minister),
        ))
    return query.order_by(ServiceLog.service_date.desc(), ServiceLog.service_id).all()


def _log_from_form(church, log):
    """Fill a ServiceLog from the posted form. Returns an error message, or None when saved."""
    service_date = parse_date(request.form.get("service_date"))
    if not service_date:
        return "Choose the date of the service."
    service = chosen_service(church, request.form.get("service_id"))
    clash = ServiceLog.query.filter(
        ServiceLog.church_id == church.id, ServiceLog.service_date == service_date,
        ServiceLog.service_id == service.id, ServiceLog.id != (log.id or 0)).first()
    if clash:
        return f"A record for {service.name} on {service_date.strftime('%d %B %Y')} already exists. Edit that one instead."
    log.church_id = church.id
    log.service_date, log.service_id = service_date, service.id
    log.sermon_topic = request.form.get("sermon_topic", "").strip()[:200] or None
    log.bible_passages = request.form.get("bible_passages", "").strip()[:300] or None
    log.preacher = request.form.get("preacher", "").strip()[:120] or None
    log.notes = request.form.get("notes", "").strip() or None
    if log.id is None:
        log.created_by = current_user.id
        db.session.add(log)
        db.session.flush()
    existing = {m.segment_id: m for m in log.ministers}
    for segment in church_segments(church):
        name = request.form.get(f"minister_{segment.id}", "").strip()[:120]
        row = existing.get(segment.id)
        if name and row:
            row.minister = name
        elif name:
            log.ministers.append(ServiceLogMinister(segment_id=segment.id, minister=name))
        elif row:
            log.ministers.remove(row)
    db.session.commit()
    return None


def _log_form_page(log):
    church = current_user.church
    values = {m.segment_id: m.minister for m in log.ministers} if log else {}
    return render_template(
        "admin/service_log_form.html", log=log, segments=church_segments(church), values=values,
        services=church_services(church), multi=multi_service(church),
        current_service=(log.service if log else current_service(church)), today=date.today(),
    )


@app.route("/admin/service-log")
@role_required(*STAFF)
@feature_required("service_analytics")
def admin_service_log():
    church = current_user.church
    today = date.today()
    start = parse_date(request.args.get("start"), today - timedelta(days=90))
    end = parse_date(request.args.get("end"), today)
    q = request.args.get("q", "").strip()
    logs = search_logs(church, start, end, q)
    counts = log_attendance_counts(church, start, end)
    return render_template(
        "admin/service_log.html", logs=logs, counts=counts, start=start.isoformat(), end=end.isoformat(), q=q,
        multi=multi_service(church), segments=church_segments(church),
    )


@app.route("/admin/service-log/new", methods=["GET", "POST"])
@role_required(*STAFF)
@feature_required("service_analytics")
def admin_new_service_log():
    church = current_user.church
    if request.method == "POST":
        log = ServiceLog()
        error = _log_from_form(church, log)
        if error:
            db.session.rollback()
            flash(error, "danger")
        else:
            flash("Service record saved.", "success")
            return redirect(url_for("admin_service_log"))
    return _log_form_page(None)


@app.route("/admin/service-log/<int:log_id>/edit", methods=["GET", "POST"])
@role_required(*STAFF)
@feature_required("service_analytics")
def admin_edit_service_log(log_id):
    church = current_user.church
    log = _own_log_or_404(log_id)
    if request.method == "POST":
        error = _log_from_form(church, log)
        if error:
            db.session.rollback()
            flash(error, "danger")
        else:
            flash("Service record updated.", "success")
            return redirect(url_for("admin_service_log"))
        log = _own_log_or_404(log_id)
    return _log_form_page(log)


@app.route("/admin/service-log/<int:log_id>/delete", methods=["POST"])
@role_required("admin")
@feature_required("service_analytics")
def admin_delete_service_log(log_id):
    log = _own_log_or_404(log_id)
    db.session.delete(log)
    db.session.commit()
    flash("Service record deleted.", "success")
    return redirect(url_for("admin_service_log"))


@app.route("/admin/service-log/report")
@role_required(*STAFF)
@feature_required("service_analytics")
def admin_service_report():
    church = current_user.church
    today = date.today()
    start = parse_date(request.args.get("start"), today - timedelta(days=30))
    end = parse_date(request.args.get("end"), today)
    q = request.args.get("q", "").strip()
    segments = church_segments(church)
    logs = sorted(search_logs(church, start, end, q), key=lambda l: (l.service_date, l.service_id))
    counts = log_attendance_counts(church, start, end)
    attendance = [counts.get((l.service_date, l.service_id), 0) for l in logs]

    if request.args.get("export"):
        if current_user.role != "admin":
            abort(403)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Date", "Day", "Service", "Sermon Topic", "Bible Passages", "Preacher"]
                        + [s.name for s in segments] + ["Attendance", "Notes"])
        for log, present in zip(logs, attendance):
            by_segment = {m.segment_id: m.minister for m in log.ministers}
            writer.writerow([log.service_date.isoformat(), WEEKDAY_NAMES[log.service_date.weekday()], log.service.name,
                             log.sermon_topic or "", log.bible_passages or "", log.preacher or ""]
                            + [by_segment.get(s.id, "") for s in segments] + [present, log.notes or ""])
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f"attachment; filename=service_report_{start}_{end}.csv"
        return resp

    preachers = {}
    for log in logs:
        if log.preacher:
            preachers[log.preacher] = preachers.get(log.preacher, 0) + 1
    total = sum(attendance)
    return render_template(
        "admin/service_report.html", church=church, logs=logs, attendance=attendance, segments=segments,
        start=start.isoformat(), end=end.isoformat(), q=q, total=total,
        average=round(total / len(logs)) if logs else 0,
        preachers=sorted(preachers.items(), key=lambda kv: (-kv[1], kv[0])),
    )


@app.route("/admin/service-log/segments", methods=["GET", "POST"])
@role_required("admin")
@feature_required("service_analytics")
def admin_service_segments():
    church = current_user.church
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        if not name:
            flash("Give the segment a name.", "danger")
        elif any(s.name.lower() == name.lower() for s in church_segments(church)):
            flash("You already have a segment with that name.", "danger")
        else:
            db.session.add(ServiceSegment(church_id=church.id, name=name))
            db.session.commit()
            flash(f"Segment '{name}' added.", "success")
        return redirect(url_for("admin_service_segments"))
    return render_template("admin/service_segments.html", segments=church_segments(church))


@app.route("/admin/service-log/segments/<int:segment_id>/rename", methods=["POST"])
@role_required("admin")
@feature_required("service_analytics")
def admin_rename_segment(segment_id):
    segment = ServiceSegment.query.filter_by(id=segment_id, church_id=current_user.church_id).first_or_404()
    name = request.form.get("name", "").strip()[:120]
    if name:
        segment.name = name
        db.session.commit()
        flash("Segment renamed.", "success")
    return redirect(url_for("admin_service_segments"))


@app.route("/admin/service-log/segments/<int:segment_id>/delete", methods=["POST"])
@role_required("admin")
@feature_required("service_analytics")
def admin_delete_segment(segment_id):
    segment = ServiceSegment.query.filter_by(id=segment_id, church_id=current_user.church_id).first_or_404()
    ServiceLogMinister.query.filter_by(segment_id=segment.id).delete(synchronize_session=False)
    db.session.delete(segment)
    db.session.commit()
    flash("Segment removed, along with the names logged against it.", "success")
    return redirect(url_for("admin_service_segments"))


# --- Departments and units ---------------------------------------------------
# The admin creates departments and assigns members to them. Each department can have unit-head
# logins that open only that department: roster, meetings and attendance, punctuality, leave
# requests and reports to the pastoral office. Workers need no login: they send leave requests
# through the department's public link, proved by member number and phone.

def dept_access(fn):
    """The church admin, or the unit head of this department. Passes the Department in as `dept`."""
    @wraps(fn)
    @login_required
    def wrapped(dept_id, *args, **kwargs):
        if "departments" not in church_features(current_user.church):
            abort(404)
        dept = Department.query.filter_by(id=dept_id, church_id=current_user.church_id).first_or_404()
        if current_user.role == "unit_head":
            if current_user.department_id != dept.id:
                abort(403)
        elif current_user.role != "admin":
            abort(403)
        return fn(dept, *args, **kwargs)
    return wrapped


def _admin_only():
    if current_user.role != "admin":
        abort(403)


def roster(dept):
    """The unit's active members, alphabetically."""
    members = [dm.member for dm in DepartmentMember.query.filter_by(department_id=dept.id).all()
               if dm.member.left_at is None]
    return sorted(members, key=lambda m: m.full_name.lower())


def dept_counts(dept):
    return {
        "roster": len(roster(dept)),
        "pending_leave": LeaveRequest.query.filter_by(department_id=dept.id, status="pending").count(),
        "unreviewed": UnitReport.query.filter_by(department_id=dept.id, reviewed_at=None).count(),
    }


def delete_department(dept):
    meeting_ids = [r[0] for r in db.session.query(DepartmentMeeting.id).filter_by(department_id=dept.id).all()]
    if meeting_ids:
        MeetingAttendance.query.filter(MeetingAttendance.meeting_id.in_(meeting_ids)).delete(synchronize_session=False)
    DepartmentMeeting.query.filter_by(department_id=dept.id).delete(synchronize_session=False)
    LeaveRequest.query.filter_by(department_id=dept.id).delete(synchronize_session=False)
    UnitReport.query.filter_by(department_id=dept.id).delete(synchronize_session=False)
    DepartmentMember.query.filter_by(department_id=dept.id).delete(synchronize_session=False)
    User.query.filter_by(department_id=dept.id, role="unit_head").delete(synchronize_session=False)
    db.session.delete(dept)


@app.route("/admin/departments", methods=["GET", "POST"])
@role_required("admin")
@feature_required("departments")
def admin_departments():
    church = current_user.church
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        if not name:
            flash("Give the department a name.", "danger")
        elif Department.query.filter(Department.church_id == church.id, func.lower(Department.name) == name.lower()).first():
            flash("You already have a department with that name.", "danger")
        else:
            dept = Department(church_id=church.id, name=name, leave_token=secrets.token_urlsafe(12),
                              description=request.form.get("description", "").strip() or None)
            db.session.add(dept)
            db.session.commit()
            flash(f"Department '{name}' created. Add its members and unit head next.", "success")
            return redirect(url_for("dept_manage", dept_id=dept.id))
        return redirect(url_for("admin_departments"))
    departments = Department.query.filter_by(church_id=church.id).order_by(Department.name).all()
    return render_template("admin/departments.html", departments=departments,
                           counts={d.id: dept_counts(d) for d in departments})


@app.route("/dept/<int:dept_id>")
@dept_access
def dept_home(dept):
    today = date.today()
    meetings = DepartmentMeeting.query.filter_by(department_id=dept.id).order_by(DepartmentMeeting.meeting_date.desc()).limit(5).all()
    pending = LeaveRequest.query.filter_by(department_id=dept.id, status="pending").order_by(LeaveRequest.start_date).all()
    reports = UnitReport.query.filter_by(department_id=dept.id).order_by(UnitReport.submitted_at.desc()).limit(5).all()
    heads = User.query.filter_by(department_id=dept.id, role="unit_head").all()
    return render_template("dept/overview.html", dept=dept, counts=dept_counts(dept), meetings=meetings,
                           pending=pending, reports=reports, heads=heads, today=today)


@app.route("/dept/<int:dept_id>/manage", methods=["GET", "POST"])
@dept_access
def dept_manage(dept):
    _admin_only()
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        if name:
            dept.name = name
            dept.description = request.form.get("description", "").strip() or None
            db.session.commit()
            flash("Department updated.", "success")
        return redirect(url_for("dept_manage", dept_id=dept.id))
    heads = User.query.filter_by(department_id=dept.id, role="unit_head").order_by(User.username).all()
    leave_url = church_base_url(current_user.church) + url_for("leave_request", token=dept.leave_token)
    return render_template("dept/manage.html", dept=dept, heads=heads, leave_url=leave_url, counts=dept_counts(dept))


@app.route("/dept/<int:dept_id>/delete", methods=["POST"])
@dept_access
def dept_delete(dept):
    _admin_only()
    name = dept.name
    delete_department(dept)
    db.session.commit()
    flash(f"Department '{name}' and its records were deleted.", "success")
    return redirect(url_for("admin_departments"))


@app.route("/dept/<int:dept_id>/heads/new", methods=["POST"])
@dept_access
def dept_new_head(dept):
    _admin_only()
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    if not username:
        flash("Username is required.", "danger")
    elif User.query.filter_by(username=username).first():
        flash("That username is already taken.", "danger")
    else:
        password = temp_password()
        head = User(username=username, role="unit_head", church_id=dept.church_id, department_id=dept.id, full_name=full_name)
        head.set_password(password, temporary=True)
        db.session.add(head)
        db.session.commit()
        flash(f"Unit head '{username}' created. Temporary password: {password} "
              "(shown once; they must change it at first login).", "success")
    return redirect(url_for("dept_manage", dept_id=dept.id))


def _own_head_or_404(dept, user_id):
    return User.query.filter_by(id=user_id, department_id=dept.id, role="unit_head").first_or_404()


@app.route("/dept/<int:dept_id>/heads/<int:user_id>/reset-password", methods=["POST"])
@dept_access
def dept_reset_head(dept, user_id):
    _admin_only()
    head = _own_head_or_404(dept, user_id)
    password = temp_password()
    head.set_password(password, temporary=True)
    db.session.commit()
    flash(f"New temporary password for '{head.username}': {password} (shown once; they must change it at next login).", "success")
    return redirect(url_for("dept_manage", dept_id=dept.id))


@app.route("/dept/<int:dept_id>/heads/<int:user_id>/delete", methods=["POST"])
@dept_access
def dept_delete_head(dept, user_id):
    _admin_only()
    head = _own_head_or_404(dept, user_id)
    db.session.delete(head)
    db.session.commit()
    flash(f"Unit head '{head.username}' removed.", "success")
    return redirect(url_for("dept_manage", dept_id=dept.id))


# --- Roster ---

@app.route("/dept/<int:dept_id>/roster")
@dept_access
def dept_roster(dept):
    return render_template("dept/roster.html", dept=dept, members=roster(dept))


@app.route("/dept/<int:dept_id>/roster/add", methods=["POST"])
@dept_access
def dept_roster_add(dept):
    _admin_only()
    church = current_user.church
    numbers = [n for n in re.split(r"[,\s]+", request.form.get("member_numbers", "").strip().upper()) if n]
    added, missing, already = [], [], []
    for raw in numbers:
        member = Member.query.filter_by(church_id=church.id, member_number=normalize_member_number(raw, church.code),
                                        left_at=None, is_child=False).first()
        if not member:
            missing.append(raw)
        elif DepartmentMember.query.filter_by(department_id=dept.id, member_id=member.id).first():
            already.append(member.full_name)
        else:
            db.session.add(DepartmentMember(department_id=dept.id, member_id=member.id))
            added.append(member.full_name)
    db.session.commit()
    if added:
        flash(f"Added to {dept.name}: {', '.join(added)}.", "success")
    if already:
        flash(f"Already in the unit: {', '.join(already)}.", "info")
    if missing:
        flash(f"No active adult member found for: {', '.join(missing)}.", "warning")
    if not numbers:
        flash("Enter at least one member number.", "warning")
    return redirect(url_for("dept_roster", dept_id=dept.id))


@app.route("/dept/<int:dept_id>/roster/<int:member_id>/remove", methods=["POST"])
@dept_access
def dept_roster_remove(dept, member_id):
    _admin_only()
    DepartmentMember.query.filter_by(department_id=dept.id, member_id=member_id).delete(synchronize_session=False)
    db.session.commit()
    flash("Removed from the unit.", "success")
    return redirect(url_for("dept_roster", dept_id=dept.id))


# --- Meetings, attendance and minutes ---

def _own_meeting_or_404(dept, meeting_id):
    return DepartmentMeeting.query.filter_by(id=meeting_id, department_id=dept.id).first_or_404()


def members_on_leave(dept, on_date):
    return {
        r[0] for r in db.session.query(LeaveRequest.member_id).filter(
            LeaveRequest.department_id == dept.id, LeaveRequest.status == "approved",
            LeaveRequest.start_date <= on_date, LeaveRequest.end_date >= on_date).all()
    }


@app.route("/dept/<int:dept_id>/meetings", methods=["GET", "POST"])
@dept_access
def dept_meetings(dept):
    if request.method == "POST":
        meeting_date = parse_date(request.form.get("meeting_date"))
        title = request.form.get("title", "").strip()[:150] or "Unit meeting"
        if not meeting_date:
            flash("Choose the date of the meeting.", "danger")
        else:
            meeting = DepartmentMeeting(department_id=dept.id, church_id=dept.church_id, meeting_date=meeting_date,
                                        title=title, created_by=current_user.id)
            db.session.add(meeting)
            db.session.commit()
            return redirect(url_for("dept_meeting", dept_id=dept.id, meeting_id=meeting.id))
    meetings = DepartmentMeeting.query.filter_by(department_id=dept.id).order_by(DepartmentMeeting.meeting_date.desc()).all()
    marked = {m.id: len(m.attendance) for m in meetings}
    return render_template("dept/meetings.html", dept=dept, meetings=meetings, marked=marked, today=date.today(),
                           total=len(roster(dept)))


@app.route("/dept/<int:dept_id>/meetings/<int:meeting_id>", methods=["GET", "POST"])
@dept_access
def dept_meeting(dept, meeting_id):
    meeting = _own_meeting_or_404(dept, meeting_id)
    members = roster(dept)
    if request.method == "POST":
        new_date = parse_date(request.form.get("meeting_date"))
        meeting.meeting_date = new_date or meeting.meeting_date
        meeting.title = request.form.get("title", "").strip()[:150] or meeting.title
        meeting.minutes = request.form.get("minutes", "").strip() or None
        existing = {a.member_id: a for a in meeting.attendance}
        for member in members:
            status = request.form.get(f"status_{member.id}", "")
            note = request.form.get(f"note_{member.id}", "").strip()[:200] or None
            row = existing.get(member.id)
            if status not in MEETING_STATUSES:
                if row:
                    meeting.attendance.remove(row)  # cleared back to "not marked"
                continue
            if row:
                row.status, row.note = status, note
            else:
                meeting.attendance.append(MeetingAttendance(member_id=member.id, status=status, note=note))
        db.session.commit()
        if request.form.get("action") == "send_minutes":
            if not meeting.minutes:
                flash("Type the minutes first, then send them.", "warning")
            else:
                db.session.add(UnitReport(
                    department_id=dept.id, church_id=dept.church_id, kind="minutes", submitted_by=current_user.id,
                    title=f"{meeting.title}, {meeting.meeting_date.strftime('%d %b %Y')}", body=meeting.minutes))
                db.session.commit()
                flash("Meeting saved, and the minutes were sent to the pastoral office.", "success")
        else:
            flash("Meeting saved.", "success")
        return redirect(url_for("dept_meeting", dept_id=dept.id, meeting_id=meeting.id))
    marks = {a.member_id: a for a in meeting.attendance}
    return render_template("dept/meeting.html", dept=dept, meeting=meeting, members=members, marks=marks,
                           on_leave=members_on_leave(dept, meeting.meeting_date), statuses=MEETING_STATUSES)


@app.route("/dept/<int:dept_id>/meetings/<int:meeting_id>/delete", methods=["POST"])
@dept_access
def dept_meeting_delete(dept, meeting_id):
    meeting = _own_meeting_or_404(dept, meeting_id)
    db.session.delete(meeting)
    db.session.commit()
    flash("Meeting deleted.", "success")
    return redirect(url_for("dept_meetings", dept_id=dept.id))


# --- Worker performance (punctuality and attendance) ---

@app.route("/dept/<int:dept_id>/performance")
@dept_access
def dept_performance(dept):
    today = date.today()
    start = parse_date(request.args.get("start"), today - timedelta(days=90))
    end = parse_date(request.args.get("end"), today)
    meetings = DepartmentMeeting.query.filter(
        DepartmentMeeting.department_id == dept.id, DepartmentMeeting.meeting_date >= start,
        DepartmentMeeting.meeting_date <= end).all()
    meeting_ids = [m.id for m in meetings]
    tally = {}
    if meeting_ids:
        for a in MeetingAttendance.query.filter(MeetingAttendance.meeting_id.in_(meeting_ids)).all():
            tally.setdefault(a.member_id, {s: 0 for s in MEETING_STATUSES})[a.status] += 1
    rows = []
    for member in roster(dept):
        t = tally.get(member.id, {s: 0 for s in MEETING_STATUSES})
        counted = t["present"] + t["late"] + t["absent"]  # excused meetings do not count against anyone
        rows.append({
            "member": member, **t, "counted": counted,
            "attendance_rate": round((t["present"] + t["late"]) / counted * 100) if counted else None,
            "punctuality_rate": round(t["present"] / (t["present"] + t["late"]) * 100) if (t["present"] + t["late"]) else None,
        })
    rows.sort(key=lambda r: (r["attendance_rate"] is None, r["attendance_rate"] if r["attendance_rate"] is not None else 0, r["member"].full_name))

    if request.args.get("export"):
        _admin_only()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Number", "Name", "Present", "Late", "Absent", "Excused", "Attendance %", "Punctuality %"])
        for r in rows:
            writer.writerow([r["member"].member_number, r["member"].full_name, r["present"], r["late"], r["absent"],
                             r["excused"], r["attendance_rate"] if r["attendance_rate"] is not None else "",
                             r["punctuality_rate"] if r["punctuality_rate"] is not None else ""])
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f"attachment; filename=performance_{dept.id}_{start}_{end}.csv"
        return resp
    return render_template("dept/performance.html", dept=dept, rows=rows, meetings=len(meetings),
                           start=start.isoformat(), end=end.isoformat())


# --- Leave and absence requests ---

@app.route("/dept/<int:dept_id>/leave")
@dept_access
def dept_leave(dept):
    show = request.args.get("show", "pending")
    query = LeaveRequest.query.filter_by(department_id=dept.id)
    if show == "pending":
        query = query.filter_by(status="pending")
    requests_ = query.order_by(LeaveRequest.created_at.desc()).all()
    leave_url = church_base_url(current_user.church) + url_for("leave_request", token=dept.leave_token)
    return render_template("dept/leave.html", dept=dept, requests=requests_, show=show, leave_url=leave_url)


@app.route("/dept/<int:dept_id>/leave/<int:request_id>/decide", methods=["POST"])
@dept_access
def dept_leave_decide(dept, request_id):
    leave = LeaveRequest.query.filter_by(id=request_id, department_id=dept.id).first_or_404()
    decision = request.form.get("decision")
    if decision not in ("approved", "declined"):
        abort(400)
    leave.status = decision
    leave.decided_by = current_user.id
    leave.decided_at = datetime.utcnow()
    leave.decision_note = request.form.get("note", "").strip()[:300] or None
    db.session.commit()
    flash(f"{leave.member.full_name}'s request was {decision}.", "success")
    return redirect(url_for("dept_leave", dept_id=dept.id))


@app.route("/leave/<token>", methods=["GET", "POST"])
def leave_request(token):
    """Public page where a worker asks for leave. No login: proved by member number and phone."""
    dept = Department.query.filter_by(leave_token=token).first_or_404()
    church = Church.query.get(dept.church_id)
    if (g.tenant and g.tenant.id != church.id) or "departments" not in church_features(church):
        abort(404)
    context = {"dept": dept, "church": church, "mine": None, "done": False}
    if request.method == "POST":
        number = normalize_member_number(request.form.get("member_number"), church.code)
        phone = request.form.get("phone", "")
        member = Member.query.filter_by(church_id=church.id, member_number=number, left_at=None).first() if number else None
        in_unit = member and DepartmentMember.query.filter_by(department_id=dept.id, member_id=member.id).first()
        if not (member and in_unit and member.phone and phone_key(member.phone) == phone_key(phone)):
            flash("We couldn't match that member number and phone number to a worker in this unit. Check them, or ask your unit head.", "danger")
        elif request.form.get("action") == "check":
            context["mine"] = LeaveRequest.query.filter_by(department_id=dept.id, member_id=member.id).order_by(
                LeaveRequest.created_at.desc()).limit(10).all()
        else:
            start = parse_date(request.form.get("start_date"))
            end = parse_date(request.form.get("end_date"), start)
            reason = request.form.get("reason", "").strip()[:500]
            if not start or not reason or end < start or (end - start).days > 366:
                flash("Give the first day, the last day (not before the first) and a reason.", "danger")
            elif LeaveRequest.query.filter_by(department_id=dept.id, member_id=member.id, start_date=start,
                                              end_date=end, status="pending").first():
                flash("You already have a pending request for those dates.", "info")
            else:
                db.session.add(LeaveRequest(department_id=dept.id, church_id=church.id, member_id=member.id,
                                            start_date=start, end_date=end, reason=reason))
                db.session.commit()
                context["done"] = True
                context["first_name"] = member.full_name.split()[0]
    return render_template("leave_request.html", form=request.form if request.method == "POST" else {}, **context)


# --- Reports and financial accounts to the pastoral office ---

REPORT_KINDS = {"minutes": "Minutes", "report": "Report", "financial": "Financial account"}


@app.route("/dept/<int:dept_id>/reports", methods=["GET", "POST"])
@dept_access
def dept_reports(dept):
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:200]
        body = request.form.get("body", "").strip()
        kind = request.form.get("kind") if request.form.get("kind") in REPORT_KINDS else "report"
        if not title or not body:
            flash("Give the report a title and write its content.", "danger")
        else:
            db.session.add(UnitReport(department_id=dept.id, church_id=dept.church_id, kind=kind, title=title,
                                      body=body, submitted_by=current_user.id))
            db.session.commit()
            flash("Sent to the pastoral office.", "success")
        return redirect(url_for("dept_reports", dept_id=dept.id))
    reports = UnitReport.query.filter_by(department_id=dept.id).order_by(UnitReport.submitted_at.desc()).all()
    return render_template("dept/reports.html", dept=dept, reports=reports, kinds=REPORT_KINDS)


@app.route("/dept/<int:dept_id>/reports/<int:report_id>/review", methods=["POST"])
@dept_access
def dept_report_review(dept, report_id):
    _admin_only()
    report = UnitReport.query.filter_by(id=report_id, department_id=dept.id).first_or_404()
    report.reviewed_at = None if report.reviewed_at else datetime.utcnow()
    db.session.commit()
    return redirect(url_for("dept_reports", dept_id=dept.id))


# --- Special programs --------------------------------------------------------
# Separate from weekly services: the admin creates a program, shares its QR
# code or link, and anyone (member or not) registers on a public page.

MARITAL_STATUSES = ["Single", "Married", "Divorced", "Widowed", "Prefer not to say"]


def program_public_url(program):
    return church_base_url(program.church) + url_for("program_register", token=program.token)


def program_state(program):
    """Return ('open', '') or ('closed' | 'full', message shown on the public page)."""
    if not program.is_open:
        return "closed", "Registration for this program is closed."
    if program.deadline and date.today() > program.deadline:
        return "closed", f"Registration closed on {program.deadline.strftime('%d %B %Y')}."
    if program.cap and program.registrations.count() >= program.cap:
        return "full", "Program full. We are no longer taking registrations."
    return "open", ""


def phone_key(phone):
    return normalize_ng_phone(phone) or re.sub(r"\D", "", phone or "")


def _own_program_or_404(program_id):
    return Program.query.filter_by(id=program_id, church_id=current_user.church_id).first_or_404()


def _program_from_form(program):
    name = request.form.get("name", "").strip()
    if not name:
        flash("The program needs a name.", "danger")
        return False
    cap_raw = request.form.get("cap", "").strip()
    if cap_raw and (not cap_raw.isdigit() or int(cap_raw) < 1):
        flash("The cap must be a whole number of 1 or more, or left empty.", "danger")
        return False
    program.name = name
    program.program_date = parse_date(request.form.get("program_date"))
    program.deadline = parse_date(request.form.get("deadline"))
    program.cap = int(cap_raw) if cap_raw else None
    program.description = request.form.get("description", "").strip()
    program.extra_question = request.form.get("extra_question", "").strip()[:200] or None
    return True


@app.route("/admin/programs")
@role_required(*STAFF)
@feature_required("programs")
def admin_programs():
    programs = Program.query.filter_by(church_id=current_user.church_id).order_by(
        Program.program_date.desc(), Program.created_at.desc()).all()
    states = {p.id: program_state(p)[0] for p in programs}
    counts = {p.id: p.registrations.count() for p in programs}
    return render_template("admin/programs.html", programs=programs, states=states, counts=counts)


@app.route("/admin/programs/new", methods=["GET", "POST"])
@role_required("admin")
@feature_required("programs")
def admin_new_program():
    if request.method == "POST":
        program = Program(church_id=current_user.church_id, token=secrets.token_urlsafe(12))
        if _program_from_form(program):
            db.session.add(program)
            db.session.commit()
            flash("Program created. Share its QR code or link so people can register.", "success")
            return redirect(url_for("admin_program_detail", program_id=program.id))
    return render_template("admin/program_form.html", program=None)


@app.route("/admin/programs/<int:program_id>/edit", methods=["GET", "POST"])
@role_required("admin")
@feature_required("programs")
def admin_edit_program(program_id):
    program = _own_program_or_404(program_id)
    if request.method == "POST" and _program_from_form(program):
        db.session.commit()
        flash("Program updated.", "success")
        return redirect(url_for("admin_program_detail", program_id=program.id))
    return render_template("admin/program_form.html", program=program)


@app.route("/admin/programs/<int:program_id>/toggle", methods=["POST"])
@role_required("admin")
@feature_required("programs")
def admin_toggle_program(program_id):
    program = _own_program_or_404(program_id)
    program.is_open = not program.is_open
    db.session.commit()
    flash("Registration is now open." if program.is_open else "Registration is now closed.", "success")
    return redirect(url_for("admin_program_detail", program_id=program.id))


@app.route("/admin/programs/<int:program_id>/delete", methods=["POST"])
@role_required("admin")
@feature_required("programs")
def admin_delete_program(program_id):
    program = _own_program_or_404(program_id)
    program.registrations.delete(synchronize_session=False)
    db.session.delete(program)
    db.session.commit()
    flash("Program and its registrations were deleted.", "success")
    return redirect(url_for("admin_programs"))


@app.route("/admin/programs/<int:program_id>/registrations/<int:reg_id>/delete", methods=["POST"])
@role_required("admin")
@feature_required("programs")
def admin_delete_registration(program_id, reg_id):
    program = _own_program_or_404(program_id)
    reg = program.registrations.filter_by(id=reg_id).first_or_404()
    db.session.delete(reg)
    db.session.commit()
    flash(f"Removed {reg.full_name}'s registration.", "success")
    return redirect(url_for("admin_program_detail", program_id=program.id))


@app.route("/admin/programs/<int:program_id>")
@role_required(*STAFF)
@feature_required("programs")
def admin_program_detail(program_id):
    program = _own_program_or_404(program_id)
    q = request.args.get("q", "").strip()
    kind = request.args.get("kind", "")
    query = program.registrations
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(
            ProgramRegistration.full_name.ilike(like), ProgramRegistration.phone.ilike(like),
            ProgramRegistration.email.ilike(like),
        ))
    if kind == "member":
        query = query.filter(ProgramRegistration.is_member.is_(True))
    elif kind == "visitor":
        query = query.filter(ProgramRegistration.is_member.is_(False))
    regs = query.order_by(ProgramRegistration.registered_at.desc()).all()

    if request.args.get("export"):
        if current_user.role != "admin":
            abort(403)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Registered", "Name", "Phone", "Email", "Gender", "Address", "Birthday",
                         "Marital Status", "Occupation", "Member", "Member Number", program.extra_question or "Extra answer"])
        member_numbers = {m.id: m.member_number for m in Member.query.filter(
            Member.id.in_([r.member_id for r in regs if r.member_id])).all()}
        for r in regs:
            writer.writerow([
                (r.registered_at + NIGERIA_OFFSET).strftime("%Y-%m-%d %H:%M"), r.full_name, r.phone, r.email or "",
                r.gender or "", r.address or "", r.date_of_birth.strftime("%d %B") if r.date_of_birth else "",
                r.marital_status or "", r.occupation or "", "Yes" if r.is_member else "No",
                member_numbers.get(r.member_id, ""), r.extra_answer or "",
            ])
        resp = Response(buf.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = f"attachment; filename=program_{program.id}_registrations.csv"
        return resp

    total = program.registrations.count()
    members = program.registrations.filter(ProgramRegistration.is_member.is_(True)).count()
    state, state_message = program_state(program)
    return render_template(
        "admin/program_detail.html", program=program, regs=regs, q=q, kind=kind, total=total,
        members=members, visitors=total - members, state=state, state_message=state_message,
        public_url=program_public_url(program),
    )


def _program_for_public(token):
    program = Program.query.filter_by(token=token).first_or_404()
    if g.tenant and g.tenant.id != program.church_id:
        abort(404)
    if "programs" not in church_features(program.church):
        abort(404)
    return program


def _matching_member(program, number_raw, phone):
    """The church member with this number, but only if the phone given matches theirs."""
    number = normalize_member_number(number_raw, program.church.code)
    member = Member.query.filter_by(church_id=program.church_id, member_number=number, left_at=None).first()
    if member and member.phone and phone_key(member.phone) == phone_key(phone):
        return member
    return None


@app.route("/program/<token>/lookup", methods=["POST"])
def program_lookup(token):
    """Fill the form for a member. Needs their number AND phone, so numbers can't be used to browse people."""
    program = _program_for_public(token)
    member = _matching_member(program, request.form.get("member_number"), request.form.get("phone"))
    if not member:
        return {"error": "We couldn't match that member number and phone number."}, 404
    dob = member.date_of_birth
    return {
        "full_name": member.full_name, "email": member.email or "", "gender": member.gender or "",
        "address": member.address or "", "dob_day": dob.day if dob else "", "dob_month": dob.month if dob else "",
    }


@app.route("/program/<token>", methods=["GET", "POST"])
def program_register(token):
    program = _program_for_public(token)
    church = program.church
    state, state_message = program_state(program)
    form = request.form if request.method == "POST" else {}

    if request.method == "POST" and state == "open":
        full_name = form.get("full_name", "").strip()
        phone = form.get("phone", "").strip()
        key = phone_key(phone)
        is_member = form.get("is_member") == "yes"
        member = None
        error = None
        if not full_name or not phone:
            error = "Please enter your full name and phone number."
        elif len(key) < 7:
            error = "That phone number doesn't look right."
        elif program.registrations.filter_by(phone_key=key).first():
            error = "This phone number is already registered for this program."
        elif is_member:
            member = _matching_member(program, form.get("member_number"), phone)
            if not member:
                error = "We couldn't match that member number with your phone number. Check them, or choose 'I'm not a member'."
        if error:
            flash(error, "danger")
        else:
            birthday = build_birthday(form.get("dob_day"), form.get("dob_month"))
            db.session.add(ProgramRegistration(
                program_id=program.id, church_id=church.id, full_name=full_name, phone=phone, phone_key=key,
                email=form.get("email", "").strip() or (member.email if member else None),
                gender=form.get("gender", "") or (member.gender if member else None),
                address=form.get("address", "").strip() or (member.address if member else None),
                date_of_birth=birthday or (member.date_of_birth if member else None),
                marital_status=form.get("marital_status", "")[:30] or None,
                occupation=form.get("occupation", "").strip()[:120] or None,
                is_member=bool(member), member_id=member.id if member else None,
                extra_answer=form.get("extra_answer", "").strip()[:300] or None,
            ))
            try:
                db.session.commit()
            except IntegrityError:  # double submit: same phone twice
                db.session.rollback()
                flash("This phone number is already registered for this program.", "danger")
            else:
                return render_template("program_register.html", program=program, church=church,
                                       state=state, state_message="", done=True,
                                       first_name=full_name.split()[0], form={}, marital_statuses=MARITAL_STATUSES)
    return render_template("program_register.html", program=program, church=church, state=state,
                           state_message=state_message, done=False, form=form,
                           marital_statuses=MARITAL_STATUSES)


# --- The church admin's own users (ushers, volunteers) ----------------------

@app.route("/admin/users")
@role_required("admin")
def admin_users():
    users = User.query.filter_by(church_id=current_user.church_id, role="user").order_by(User.created_at).all()
    return render_template("admin/users.html", users=users)


@app.route("/admin/users/new", methods=["GET", "POST"])
@role_required("admin")
def admin_new_user():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        if not username:
            flash("Username is required.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("That username is already taken.", "danger")
        else:
            password = temp_password()
            user = User(username=username, role="user", church_id=current_user.church_id, full_name=full_name)
            user.set_password(password, temporary=True)
            db.session.add(user)
            db.session.commit()
            flash(f"User '{username}' created. Temporary password: {password} "
                  "(shown once; they must change it at first login).", "success")
            return redirect(url_for("admin_users"))
    return render_template("admin/user_form.html")


def _own_user_or_404(user_id):
    return User.query.filter_by(id=user_id, church_id=current_user.church_id, role="user").first_or_404()


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@role_required("admin")
def admin_reset_user_password(user_id):
    user = _own_user_or_404(user_id)
    password = temp_password()
    user.set_password(password, temporary=True)
    db.session.commit()
    flash(f"New temporary password for '{user.username}': {password} "
          "(shown once; they must change it at next login).", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_user(user_id):
    user = _own_user_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash(f"User '{user.username}' removed.", "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("errors/404.html"), 404


# ---------------------------------------------------------------------------
# Bootstrap: create tables & seed the first super admin
# ---------------------------------------------------------------------------

def seed_superadmin():
    if User.query.filter_by(role="superadmin").first():
        return
    username = os.environ.get("SUPERADMIN_USERNAME", "superadmin")
    password = os.environ.get("SUPERADMIN_PASSWORD")
    generated = False
    if not password:
        password = secrets.token_urlsafe(9)
        generated = True
    sa = User(username=username, role="superadmin", full_name="Super Admin")
    sa.set_password(password)
    db.session.add(sa)
    db.session.commit()
    print("=" * 70)
    print(f" First run: created super admin account")
    print(f"   Username: {username}")
    if generated:
        print(f"   Password: {password}  (auto-generated — save this now!)")
    else:
        print(f"   Password: (from SUPERADMIN_PASSWORD env var)")
    print(" Log in and change these credentials as soon as possible.")
    print("=" * 70)


@app.cli.command("reset-superadmin")
@click.option("--username", default=None, help="Super admin username (default: SUPERADMIN_USERNAME env var, or 'superadmin').")
@click.option("--password", default=None, help="New password. If omitted, a random one is generated and printed.")
def reset_superadmin_command(username, password):
    """Create or reset a super admin's login. Safe to run any time.

    Run this against your PRODUCTION database if you're ever locked out:
      - On Render: use the Shell tab (if your plan has one), or
      - Locally: set DATABASE_URL to your production connection string, then run
            flask reset-superadmin
    """
    username = username or os.environ.get("SUPERADMIN_USERNAME", "superadmin")
    generated = False
    if not password:
        password = secrets.token_urlsafe(9)
        generated = True

    user = User.query.filter_by(username=username).first()
    if user:
        user.role = "superadmin"
        user.set_password(password)
        action = "reset"
    else:
        user = User(username=username, role="superadmin", full_name="Super Admin")
        user.set_password(password)
        db.session.add(user)
        action = "created"
    db.session.commit()

    click.echo("=" * 70)
    click.echo(f" Super admin {action}: {username}")
    if generated:
        click.echo(f"   Password: {password}  (save this now!)")
    else:
        click.echo("   Password: (as provided)")
    click.echo("=" * 70)


# There's no migration framework (Alembic/Flask-Migrate) in this single-file
# app, and db.create_all() only creates tables that don't exist yet — it
# never alters an existing one. So when a column is added to a model after
# a database is already in use, this patches it in on startup: safe to run
# every time, and it never touches existing rows (new columns just come back
# as NULL/default for rows that already existed).
_SCHEMA_PATCHES = [
    ("member", "left_at", "DATETIME"),
    ("member", "is_child", "BOOLEAN NOT NULL DEFAULT 0"),
    ("church", "checkin_secret", "VARCHAR(64)"),
    ("church", "absentee_email_subject", "VARCHAR(200)"),
    ("church", "absentee_email_body", "TEXT"),
    ("church", "absentee_sms_body", "TEXT"),
    ("church", "logo_data", "TEXT"),
    ("church", "logo_mimetype", "VARCHAR(50)"),
    ("church", "subdomain", "VARCHAR(63)"),
    ("church", "features", "TEXT"),
    ("member", "emergency_contact_name", "VARCHAR(120)"),
    ("member", "emergency_contact_phone", "VARCHAR(50)"),
    ("user", "department_id", "INTEGER"),
    ("church", "first_timer_months", "INTEGER"),
    ("church", "first_timer_min_services", "INTEGER"),
    ("church", "current_service_id", "INTEGER"),
    ("member", "first_timer_start", "DATE"),
    ("member", "promoted_at", "DATETIME"),
    ("user", "must_change_password", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def ensure_schema():
    inspector = inspect(db.engine)
    if "member" not in inspector.get_table_names():
        return  # fresh database; db.create_all() already made it fully up to date
    for table, column, coltype in _SCHEMA_PATCHES:
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing_cols:
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {coltype}'))
            print(f"Schema update: added '{column}' column to '{table}' table.")
    _migrate_attendance_services()


def _migrate_attendance_services():
    """Give existing attendance a service, and allow one record per member per date per service."""
    inspector = inspect(db.engine)
    if "attendance" not in inspector.get_table_names():
        return
    if "service_id" in {c["name"] for c in inspector.get_columns("attendance")}:
        return
    for church in Church.query.all():
        if not Service.query.filter_by(church_id=church.id).first():
            db.session.add(Service(church_id=church.id, name="Main Service"))
    db.session.commit()
    default_service = "(SELECT MIN(s.id) FROM service s WHERE s.church_id = {}.church_id)"
    with db.engine.begin() as conn:
        if db.engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE attendance DROP CONSTRAINT IF EXISTS uq_member_service_date"))
            conn.execute(text("ALTER TABLE attendance ADD COLUMN service_id INTEGER REFERENCES service(id)"))
            conn.execute(text("UPDATE attendance SET service_id = " + default_service.format("attendance")))
            conn.execute(text("ALTER TABLE attendance ALTER COLUMN service_id SET NOT NULL"))
            conn.execute(text("ALTER TABLE attendance ADD CONSTRAINT uq_member_date_service "
                              "UNIQUE (member_id, service_date, service_id)"))
        else:
            # SQLite can't change a constraint in place, so rebuild the table.
            conn.execute(text("ALTER TABLE attendance RENAME TO attendance_old"))
            Attendance.__table__.create(conn)
            conn.execute(text(
                "INSERT INTO attendance (id, member_id, church_id, service_date, marked_at, service_id) "
                "SELECT a.id, a.member_id, a.church_id, a.service_date, a.marked_at, "
                + default_service.format("a") + " FROM attendance_old a"))
            conn.execute(text("DROP TABLE attendance_old"))
    print("Schema update: attendance now records which service each check-in was for.")


with app.app_context():
    db.create_all()
    ensure_schema()
    seed_superadmin()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
