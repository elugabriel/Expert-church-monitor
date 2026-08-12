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
import csv
import uuid
import secrets
import click
from functools import wraps
from datetime import datetime, date

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
from sqlalchemy import extract, func
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


@app.context_processor
def inject_globals():
    return {"now_year": datetime.utcnow().year, "app_name": APP_NAME, "month_names": MONTH_NAMES}


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

@app.route("/admin")
@role_required("admin")
def admin_dashboard():
    church = current_user.church
    today = date.today()
    all_members = Member.query.filter_by(church_id=church.id).all()
    total_members = len(all_members)
    all_editable = bool(all_members) and all(m.is_editable for m in all_members)
    todays_attendance = (
        Attendance.query.filter_by(church_id=church.id, service_date=today)
        .order_by(Attendance.marked_at.desc()).all()
    )
    present_today = len(todays_attendance)
    birthdays = (
        Member.query.filter(
            Member.church_id == church.id,
            Member.date_of_birth.isnot(None),
            extract("month", Member.date_of_birth) == today.month,
        ).order_by(extract("day", Member.date_of_birth)).all()
    )
    return render_template(
        "admin/dashboard.html", church=church, today=today,
        total_members=total_members, present_today=present_today,
        todays_attendance=todays_attendance, birthdays=birthdays,
        all_editable=all_editable,
    )


@app.route("/admin/members/toggle-edit-all", methods=["POST"])
@role_required("admin")
def admin_toggle_edit_all():
    church = current_user.church
    members = Member.query.filter_by(church_id=church.id).all()
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
    number = request.form.get("member_number", "").strip().upper()
    member = Member.query.filter_by(church_id=church.id, member_number=number).first()
    if not member:
        flash(f"No member found with number '{number}'. Is this a first timer?", "warning")
        return redirect(url_for("admin_dashboard"))
    today = date.today()
    existing = Attendance.query.filter_by(member_id=member.id, service_date=today).first()
    if existing:
        flash(f"{member.full_name} was already marked present today.", "info")
    else:
        db.session.add(Attendance(member_id=member.id, church_id=church.id, service_date=today))
        db.session.commit()
        flash(f"Marked present: {member.full_name} ({member.member_number}).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/members")
@role_required("admin")
def admin_members():
    church = current_user.church
    q = request.args.get("q", "").strip()
    query = Member.query.filter_by(church_id=church.id)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Member.full_name.ilike(like), Member.member_number.ilike(like)))
    members = query.order_by(Member.full_name).all()
    return render_template("admin/members_list.html", members=members, q=q)


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


@app.route("/admin/members/new", methods=["GET", "POST"])
@role_required("admin")
def admin_new_member():
    church = current_user.church
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Full name is required.", "danger")
            return redirect(url_for("admin_new_member"))
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
        return redirect(url_for("admin_members"))
    return render_template("admin/member_form.html", member=None)


@app.route("/admin/members/<int:member_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_edit_member(member_id):
    church = current_user.church
    member = Member.query.filter_by(id=member_id, church_id=church.id).first_or_404()
    if request.method == "POST":
        if not member.is_editable:
            flash("This member's details are locked. Enable editing first.", "danger")
            return redirect(url_for("admin_edit_member", member_id=member.id))
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
        return redirect(url_for("admin_members"))
    return render_template("admin/member_form.html", member=member)


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
        absentees = Member.query.filter_by(church_id=church.id).filter(
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


with app.app_context():
    db.create_all()
    seed_superadmin()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
