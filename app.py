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
import csv
import uuid
import secrets
import click
from functools import wraps
from datetime import datetime, date, timedelta

from flask import (
    Flask, request, redirect, url_for, flash, render_template,
    abort, Response, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user, login_required,
    current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
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
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_db_url = os.environ.get("DATABASE_URL", "sqlite:///church.db")
# Render / some providers hand out "postgres://" which SQLAlchemy 1.4+ rejects.
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = _db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}

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
    address = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    email = db.Column(db.String(120))
    pastor_name = db.Column(db.String(120))
    description = db.Column(db.Text)
    logo_filename = db.Column(db.String(255))
    next_seq = db.Column(db.Integer, default=1, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    role = db.Column(db.String(20), nullable=False)  # 'superadmin' or 'admin'
    full_name = db.Column(db.String(120))
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    church = db.relationship("Church", backref="admins")

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

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

    church = db.relationship("Church", backref="members")

    __table_args__ = (
        db.UniqueConstraint("church_id", "member_number", name="uq_church_member_number"),
    )


class Attendance(db.Model):
    __tablename__ = "attendance"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    church_id = db.Column(db.Integer, db.ForeignKey("church.id"), nullable=False)
    service_date = db.Column(db.Date, nullable=False, default=date.today)
    marked_at = db.Column(db.DateTime, default=datetime.utcnow)

    member = db.relationship("Member", backref="attendances")

    __table_args__ = (
        db.UniqueConstraint("member_id", "service_date", name="uq_member_service_date"),
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

AVATAR_PALETTE = ["#2f5cf6", "#0f9d76", "#e8474f", "#f2a93b", "#7c5cf6", "#0891b2", "#c2410c", "#65a30d"]


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
    if current_user.is_authenticated and current_user.role == "admin" and current_user.church_id:
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
        "today": date.today(),
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role == "superadmin":
            return redirect(url_for("superadmin_dashboard"))
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
            login_user(user)
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("index"))
        flash("Invalid username or password.", "danger")
    return render_template("auth_login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ---------------------------------------------------------------------------
# Superadmin routes
# ---------------------------------------------------------------------------

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
        if not name or not code:
            flash("Name and code are required.", "danger")
        elif Church.query.filter_by(code=code).first():
            flash("That church code is already in use.", "danger")
        else:
            church = Church(
                name=name, code=code,
                pastor_name=request.form.get("pastor_name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=request.form.get("email", "").strip(),
                address=request.form.get("address", "").strip(),
                description=request.form.get("description", "").strip(),
            )
            logo = request.files.get("logo")
            fname = save_upload(logo)
            if fname:
                church.logo_filename = fname
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
        church.name = request.form.get("name", "").strip() or church.name
        church.pastor_name = request.form.get("pastor_name", "").strip()
        church.phone = request.form.get("phone", "").strip()
        church.email = request.form.get("email", "").strip()
        church.address = request.form.get("address", "").strip()
        church.description = request.form.get("description", "").strip()
        logo = request.files.get("logo")
        fname = save_upload(logo)
        if fname:
            church.logo_filename = fname
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
    member_ids = [m.id for m in church.members]
    Attendance.query.filter(Attendance.member_id.in_(member_ids)).delete(synchronize_session=False)
    Member.query.filter_by(church_id=church.id).delete(synchronize_session=False)
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
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        if not username or not password:
            flash("Username and password are required.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("That username is already taken.", "danger")
        else:
            admin = User(username=username, role="admin", church_id=church.id, full_name=full_name)
            admin.set_password(password)
            db.session.add(admin)
            db.session.commit()
            flash(f"Admin '{username}' created for {church.name}.", "success")
            return redirect(url_for("superadmin_church_detail", church_id=church.id))
    return render_template("superadmin/admin_form.html", church=church)


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
        db.session.query(Attendance.service_date, func.count(Attendance.id))
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
@role_required("admin")
def admin_dashboard():
    church = current_user.church
    today = date.today()
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
    present_today = len(todays_attendance)
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
    present_last_week = Attendance.query.join(Member).filter(
        Attendance.church_id == church.id, Attendance.service_date == a_week_ago, Member.is_child.is_(False),
    ).count()
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
@role_required("admin")
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
        existing = Attendance.query.filter_by(member_id=member.id, service_date=today).first()
        if existing:
            already.append(member.full_name)
            continue
        db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=today))
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
        checked_ids = {int(v) for v in request.form.getlist("member_ids") if v.isdigit()}
        shown_ids = {int(v) for v in request.form.getlist("shown_ids") if v.isdigit()}
        already_present_ids = {
            r[0] for r in db.session.query(Attendance.member_id).filter(
                Attendance.church_id == church.id, Attendance.service_date == today,
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
                db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=today))
                marked_count += 1

        unmarked_count = 0
        if to_unmark:
            unmarked_count = Attendance.query.filter(
                Attendance.member_id.in_(to_unmark), Attendance.church_id == church.id,
                Attendance.service_date == today,
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
        return redirect(url_for(redirect_endpoint))

    q = request.args.get("q", "").strip()
    query = Member.query.filter_by(church_id=church.id, left_at=None, is_child=is_child)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Member.full_name.ilike(like), Member.member_number.ilike(like)))
    members = query.order_by(Member.full_name).all()

    present_ids = {
        r[0] for r in db.session.query(Attendance.member_id).filter(
            Attendance.church_id == church.id, Attendance.service_date == today,
        ).all()
    }
    return render_template(template, members=members, q=q, present_ids=present_ids, today=today)


@app.route("/admin/attendance/checklist", methods=["GET", "POST"])
@role_required("admin")
def admin_attendance_checklist():
    return _attendance_checklist_view(False, "admin_attendance_checklist", "admin/attendance_checklist.html")


@app.route("/admin/children/attendance/checklist", methods=["GET", "POST"])
@role_required("admin")
def admin_children_checklist():
    return _attendance_checklist_view(True, "admin_children_checklist", "admin/children_checklist.html")


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
@role_required("admin")
def admin_members():
    return _members_list_view(False, "admin_members", "admin/members_list.html")


@app.route("/admin/children")
@role_required("admin")
def admin_children():
    return _members_list_view(True, "admin_children", "admin/children_list.html")


@app.route("/admin/children/move-in", methods=["GET", "POST"])
@role_required("admin")
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
            db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=date.today()))
        db.session.commit()
        flash(f"{member.full_name} registered with number {member.member_number}.", "success")
        return redirect(url_for(list_endpoint))
    return render_template(template, member=None, is_child=is_child)


@app.route("/admin/members/new", methods=["GET", "POST"])
@role_required("admin")
def admin_new_member():
    return _new_member_view(False, "admin_members", "admin/member_form.html")


@app.route("/admin/children/new", methods=["GET", "POST"])
@role_required("admin")
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
        fname = save_upload(request.files.get("photo"))
        if fname:
            member.photo_filename = fname
        db.session.commit()
        flash("Member details updated.", "success")
        return redirect(url_for(list_endpoint))
    return render_template(template, member=member, is_child=is_child)


@app.route("/admin/members/<int:member_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_edit_member(member_id):
    return _edit_member_view(member_id, False, "admin_members", "admin_edit_member", "admin/member_form.html")


@app.route("/admin/children/<int:member_id>/edit", methods=["GET", "POST"])
@role_required("admin")
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
    db.session.delete(member)
    db.session.commit()
    flash(f"{name} was removed from the church list.", "success")
    return redirect(url_for("admin_children" if is_child else "admin_members"))


@app.route("/admin/reports/absentees")
@role_required("admin")
def admin_absentee_report():
    church = current_user.church
    today = date.today()
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    results = None

    if start and end:
        attended_ids = {
            r[0] for r in db.session.query(Attendance.member_id).filter(
                Attendance.church_id == church.id,
                Attendance.service_date >= start,
                Attendance.service_date <= end,
            ).all()
        }
        absentees = Member.query.filter_by(church_id=church.id, left_at=None, is_child=False).filter(
            ~Member.id.in_(attended_ids) if attended_ids else True
        ).order_by(Member.full_name).all()

        results = []
        for m in absentees:
            last = db.session.query(func.max(Attendance.service_date)).filter(
                Attendance.member_id == m.id, Attendance.service_date < start
            ).scalar()
            results.append((m, last))

        if request.args.get("export"):
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
        start=start.isoformat(), end=end.isoformat(), results=results,
    )


@app.route("/admin/church-profile")
@role_required("admin")
def admin_church_profile():
    church = current_user.church
    return render_template("admin/church_profile.html", church=church)


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
]


def ensure_schema():
    inspector = inspect(db.engine)
    if "member" not in inspector.get_table_names():
        return  # fresh database; db.create_all() already made it fully up to date
    for table, column, coltype in _SCHEMA_PATCHES:
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        if column not in existing_cols:
            with db.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
            print(f"Schema update: added '{column}' column to '{table}' table.")


with app.app_context():
    db.create_all()
    ensure_schema()
    seed_superadmin()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
