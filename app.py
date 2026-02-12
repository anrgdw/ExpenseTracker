from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask import Flask, render_template, request, redirect, url_for, flash, session
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import os
from datetime import timedelta
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change_this_in_production")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("SESSION_COOKIE_SECURE", "1") == "1")

DB_NAME = "database.db"
DATABASE_URL = os.getenv("DATABASE_URL")
DB_TYPE = "postgres" if DATABASE_URL else "sqlite"

CHART_PALETTE = [
    "#7f8bff",
    "#6dc6ff",
    "#f5b971",
    "#8ed7b2",
    "#f28fb1",
    "#c1c7dd",
    "#9bb0ff",
]


# -----------------------------
# Database helpers
# -----------------------------
class DBConnection:
    def __init__(self, conn, db_type):
        self.conn = conn
        self.db_type = db_type

    def execute(self, query, params=None):
        params = params or []
        if self.db_type == "postgres":
            query = query.replace("?", "%s")
            cursor = self.conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(query, params)
            return cursor
        if params:
            return self.conn.execute(query, params)
        return self.conn.execute(query)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db_connection():
    if DB_TYPE == "postgres":
        sslmode = os.getenv("DB_SSLMODE", "require")
        database_url = DATABASE_URL or ""
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        if database_url and "sslmode=" in database_url:
            conn = psycopg2.connect(database_url)
        else:
            conn = psycopg2.connect(database_url, sslmode=sslmode)
        return DBConnection(conn, "postgres")

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    # Enforce foreign keys (SQLite only)
    conn.execute("PRAGMA foreign_keys = ON")
    return DBConnection(conn, "sqlite")


def init_db():
    conn = get_db_connection()
    if DB_TYPE == "postgres":
        # Users table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            );
        """)
        # Categories table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE
            );
        """)
        # Expenses table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id SERIAL PRIMARY KEY,
                date TEXT NOT NULL,
                item TEXT NOT NULL,
                category_id INTEGER REFERENCES categories(id),
                amount REAL NOT NULL,
                user_id INTEGER REFERENCES users(id)
            );
        """)
    else:
        # Users table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            );
        """)
        # Categories table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
        """)
        # Expenses table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                item TEXT NOT NULL,
                category_id INTEGER,
                amount REAL NOT NULL,
                user_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES categories(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)

    # Add user_id column for existing databases
    if DB_TYPE == "postgres":
        conn.execute(
            "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)"
        )
    else:
        columns = conn.execute("PRAGMA table_info(expenses)").fetchall()
        column_names = {col["name"] for col in columns}
        if "user_id" not in column_names:
            conn.execute(
                "ALTER TABLE expenses ADD COLUMN user_id INTEGER REFERENCES users(id)"
            )
    conn.commit()
    conn.close()


def get_categories():
    conn = get_db_connection()
    categories = conn.execute(
        "SELECT id, name FROM categories ORDER BY name"
    ).fetchall()
    conn.close()
    return categories


def _stable_index(text, length):
    if not text:
        return 0
    total = 0
    for i, ch in enumerate(str(text).lower()):
        total += (i + 1) * ord(ch)
    return total % length


def _hex_to_rgba(hex_color, alpha=0.18):
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def format_pretty_date(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d")
        except ValueError:
            try:
                dt = datetime.strptime(str(value), "%d-%m-%Y")
            except ValueError:
                return str(value)
    return f"{dt.day} {dt.strftime('%B')} {dt.year}"


def category_style(value):
    index = _stable_index(value, len(CHART_PALETTE))
    color = CHART_PALETTE[index]
    return f"background: {_hex_to_rgba(color)}; color: {color};"


@app.template_filter("pretty_date")
def pretty_date_filter(value):
    return format_pretty_date(value)


@app.template_filter("category_style")
def category_style_filter(value):
    return category_style(value)


# -----------------------------
# Auth helpers
# -----------------------------
def _initials_from_name(name, email=None):
    if name:
        parts = [part for part in name.strip().split() if part]
        initials = "".join([p[0].upper() for p in parts[:2]])
        if initials:
            return initials
    if email:
        return email.strip()[:2].upper()
    return "U"


def get_current_user():
    if not session.get("user_id"):
        return None
    return {
        "id": session.get("user_id"),
        "name": session.get("user_name"),
        "email": session.get("user_email"),
    }


@app.context_processor
def inject_current_user():
    user = get_current_user()
    initials = None
    if user:
        initials = _initials_from_name(user.get("name"), user.get("email"))
    return {"current_user": user, "user_initials": initials}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


# -----------------------------
# Authentication
# -----------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("All fields are required.", "warning")
            return render_template("register.html", name=name, email=email)

        conn = get_db_connection()
        try:
            if DB_TYPE == "postgres":
                cursor = conn.execute(
                    "INSERT INTO users (name, email, password) VALUES (?, ?, ?) RETURNING id",
                    (name, email, generate_password_hash(password)),
                )
                user_id = cursor.fetchone()["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                    (name, email, generate_password_hash(password)),
                )
                user_id = cursor.lastrowid
            conn.commit()
        except (sqlite3.IntegrityError, psycopg2.IntegrityError):
            conn.close()
            flash("An account with that email already exists.", "danger")
            return render_template("register.html", name=name, email=email)

        session.permanent = True
        session["user_id"] = user_id
        session["user_name"] = name
        session["user_email"] = email
        conn.close()
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Email and password are required.", "warning")
            return render_template("login.html", email=email)

        conn = get_db_connection()
        user = conn.execute(
            "SELECT id, name, email, password FROM users WHERE email = ?",
            (email,),
        ).fetchone()
        conn.close()

        if not user or not check_password_hash(user["password"], password):
            flash("Invalid email or password.", "danger")
            return render_template("login.html", email=email)

        session.permanent = True
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["user_email"] = user["email"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -----------------------------
# Public Home
# -----------------------------
@app.route("/home")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("home.html")


# -----------------------------
# Add Expense
# -----------------------------
@app.route("/", methods=["GET", "POST"])
@login_required
def add_expense():
    conn = get_db_connection()
    user_id = session.get("user_id")

    categories = conn.execute(
        "SELECT id, name FROM categories ORDER BY name"
    ).fetchall()

    if request.method == "POST":
        date = request.form.get("date")
        item = request.form.get("item")
        category_id = request.form.get("category_id")
        amount = request.form.get("amount")

        conn.execute(
            """
            INSERT INTO expenses (date, item, category_id, amount, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (date, item, category_id, amount, user_id),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("add_expense"))

    conn.close()
    return render_template("add_expense.html", categories=categories)


# -----------------------------
# Helper: build filter clause
# -----------------------------
def build_expense_filters(user_id, filter_type, from_date, to_date):
    conditions = ["expenses.user_id = ?"]
    params = [user_id]

    if filter_type == "week":
        if DB_TYPE == "postgres":
            conditions.append(
                "expenses.date::date >= CURRENT_DATE - INTERVAL '7 days'"
            )
        else:
            conditions.append("expenses.date >= date('now', '-7 days')")
    elif filter_type == "month":
        if DB_TYPE == "postgres":
            conditions.append(
                "expenses.date::date >= CURRENT_DATE - INTERVAL '30 days'"
            )
        else:
            conditions.append("expenses.date >= date('now', '-30 days')")
    elif filter_type == "year":
        if DB_TYPE == "postgres":
            conditions.append(
                "expenses.date::date >= CURRENT_DATE - INTERVAL '365 days'"
            )
        else:
            conditions.append("expenses.date >= date('now', '-365 days')")
    elif from_date and to_date:
        if DB_TYPE == "postgres":
            conditions.append("expenses.date::date BETWEEN ? AND ?")
        else:
            conditions.append("expenses.date BETWEEN ? AND ?")
        params.extend([from_date, to_date])

    where_clause = "WHERE " + " AND ".join(conditions)
    return where_clause, params, " AND ".join(conditions)


# -----------------------------
# Dashboard
# -----------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    # Filters
    user_id = session.get("user_id")
    filter_type = request.args.get("filter")
    from_date = request.args.get("from")
    to_date = request.args.get("to")

    where_clause, params, join_conditions = build_expense_filters(
        user_id, filter_type, from_date, to_date
    )
    conn = get_db_connection()

    # -------- CARDS SHOULD RESPECT FILTER (A) --------
    # Total for current filter range
    filtered_total_row = conn.execute(
        f"SELECT SUM(expenses.amount) AS total FROM expenses {where_clause}",
        params,
    ).fetchone()
    filtered_total = filtered_total_row["total"] or 0

    # For filtered average daily spend, calculate days_spanned for date range
    if where_clause and ("BETWEEN" in where_clause):
        # custom from/to range
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        days_spanned = (end - start).days + 1
    elif filter_type == "week":
        days_spanned = 7
    elif filter_type == "month":
        days_spanned = 30
    elif filter_type == "year":
        days_spanned = 365
    else:
        # no filter -> current month stats
        days_spanned = datetime.now().day

    avg_daily = round(filtered_total / days_spanned, 2) if days_spanned else 0

    # This month vs last month are still overall (if you want even this filtered,
    # you can also adjust; yahan maine cards ke liye filtered_total + avg ko use kiya)
    current_month = datetime.now().strftime("%Y-%m")
    last_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime(
        "%Y-%m"
    )

    this_month_total = (
        conn.execute(
            "SELECT SUM(expenses.amount) AS total FROM expenses WHERE expenses.user_id = ? AND substr(expenses.date,1,7)=?",
            (user_id, current_month),
        ).fetchone()["total"]
        or 0
    )

    last_month_total = (
        conn.execute(
            "SELECT SUM(expenses.amount) AS total FROM expenses WHERE expenses.user_id = ? AND substr(expenses.date,1,7)=?",
            (user_id, last_month),
        ).fetchone()["total"]
        or 0
    )

    # Percentage change based on overall months
    if last_month_total > 0:
        change_percent = round(
            ((this_month_total - last_month_total) / last_month_total) * 100, 1
        )
    else:
        change_percent = 0

    trend = "up" if change_percent > 0 else "down"
    trend_color = "red" if change_percent > 0 else "green"

    # Category totals respecting filter (A)
    category_rows = conn.execute(
        f"""
        SELECT
            categories.name AS category,
            COALESCE(SUM(expenses.amount), 0) AS total
        FROM categories
        LEFT JOIN expenses
            ON expenses.category_id = categories.id
            AND {join_conditions}
        GROUP BY categories.id, categories.name
        ORDER BY categories.name;
        """,
        params,
    ).fetchall()

    labels = [row["category"] for row in category_rows]
    values = [row["total"] for row in category_rows]

    # Top spending category in filtered data (A)
    top_category = conn.execute(
        f"""
        SELECT categories.name AS category,
               SUM(expenses.amount) AS total
        FROM expenses
        JOIN categories ON expenses.category_id = categories.id
        {where_clause}
        GROUP BY categories.name
        ORDER BY total DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    top_category_name = top_category["category"] if top_category else "N/A"
    top_category_amount = top_category["total"] if top_category else 0

    # Last 5 expenses (already respecting filter)
    recent_expenses = conn.execute(
        f"""
        SELECT
            expenses.date,
            expenses.item,
            categories.name AS category,
            expenses.amount
        FROM expenses
        LEFT JOIN categories ON expenses.category_id = categories.id
        {where_clause}
        ORDER BY expenses.date DESC
        LIMIT 5
        """,
        params,
    ).fetchall()

    # Auto dashboard summary (based on overall month trend)
    if trend == "up":
        summary_text = (
            f"Your expenses increased compared to last month, mainly due to {top_category_name} category."
        )
    else:
        summary_text = (
            "Your expenses decreased compared to last month, showing better spending control."
        )

    # Highest single expense (filtered)
    highest_expense = conn.execute(
        f"""
        SELECT item, amount
        FROM expenses
        {where_clause}
        ORDER BY amount DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    highest_item = highest_expense["item"] if highest_expense else "N/A"
    highest_amount = highest_expense["amount"] if highest_expense else 0

    # Spending trend message
    trend_message = (
        "Your spending increased this month"
        if trend == "up"
        else "Your spending decreased this month"
    )

    # Monthly expense trend respecting filter (B)
    # Idea: we still group by YYYY-MM but apply same filter condition
    monthly_data = conn.execute(
        f"""
        SELECT substr(expenses.date,1,7) AS month, SUM(amount) AS total
        FROM expenses
        {where_clause}
        GROUP BY month
        ORDER BY month
        """,
        params,
    ).fetchall()

    months = [row["month"] for row in monthly_data]
    monthly_totals = [row["total"] for row in monthly_data]

    # Highest spending month (within filtered data)
    highest_month_row = conn.execute(
        f"""
        SELECT substr(expenses.date,1,7) AS month, SUM(amount) AS total
        FROM expenses
        {where_clause}
        GROUP BY month
        ORDER BY total DESC
        LIMIT 1
        """,
        params,
    ).fetchone()

    highest_month = highest_month_row["month"] if highest_month_row else "N/A"
    highest_month_amount = (
        highest_month_row["total"] if highest_month_row else 0
    )

    # Category-wise monthly trend respecting filter (B)
    category_month_data = conn.execute(
        f"""
        SELECT
            substr(expenses.date,1,7) AS month,
            categories.name AS category,
            SUM(expenses.amount) AS total
        FROM expenses
        JOIN categories ON expenses.category_id = categories.id
        {where_clause}
        GROUP BY month, categories.name
        ORDER BY month
        """,
        params,
    ).fetchall()

    category_month_map = defaultdict(dict)
    for row in category_month_data:
        category_month_map[row["category"]][row["month"]] = row["total"]

    all_months = sorted(set(months))

    category_datasets = []
    for category, month_data in category_month_map.items():
        category_datasets.append(
            {
                "label": category,
                "data": [month_data.get(m, 0) for m in all_months],
            }
        )

    conn.close()

    return render_template(
        "dashboard.html",
        # Cards (filtered)
        this_month=filtered_total,  # filtered total instead of raw current month
        last_month=last_month_total,  # still based on real last month
        avg_daily=avg_daily,
        change_percent=abs(change_percent),
        trend=trend,
        trend_color=trend_color,
        # Category data (filtered)
        labels=labels,
        values=values,
        top_category_name=top_category_name,
        top_category_amount=top_category_amount,
        # Highest expense (filtered)
        highest_item=highest_item,
        highest_amount=highest_amount,
        # Trend & summary
        trend_message=trend_message,
        summary_text=summary_text,
        # Monthly trend (filtered)
        months=months,
        monthly_totals=monthly_totals,
        highest_month=highest_month,
        highest_month_amount=highest_month_amount,
        # Category-wise monthly datasets (filtered)
        category_month_labels=all_months,
        category_datasets=category_datasets,
        # Filtered stats
        filtered_total=filtered_total,
        # Recent expenses (filtered)
        recent_expenses=recent_expenses,
        # keep filter values in template
        filter_type=filter_type,
        from_date=from_date,
        to_date=to_date,
    )


# -----------------------------
# All Expenses
# -----------------------------
@app.route("/expenses")
@login_required
def all_expenses():
    conn = get_db_connection()
    user_id = session.get("user_id")

    filter_type = request.args.get("filter")
    from_date = request.args.get("from")
    to_date = request.args.get("to")

    where_clause, params, _ = build_expense_filters(
        user_id, filter_type, from_date, to_date
    )

    base_query = f"""
        SELECT
            expenses.id,
            expenses.date,
            expenses.item,
            categories.name AS category,
            expenses.amount
        FROM expenses
        LEFT JOIN categories ON expenses.category_id = categories.id
        {where_clause}
        ORDER BY expenses.date DESC
    """

    sum_query = f"SELECT SUM(expenses.amount) AS total FROM expenses {where_clause}"

    expenses = conn.execute(base_query, params).fetchall()
    total = conn.execute(sum_query, params).fetchone()["total"]
    conn.close()

    if total is None:
        total = 0

    return render_template(
        "all_expenses.html",
        expenses=expenses,
        total=total,
        filter_type=filter_type,
        from_date=from_date,
        to_date=to_date,
    )


# -----------------------------
# Delete Expense
# -----------------------------
@app.route("/delete/<int:id>")
@login_required
def delete_expense(id):
    conn = get_db_connection()
    user_id = session.get("user_id")
    conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (id, user_id))
    conn.commit()
    conn.close()
    return redirect(url_for("all_expenses"))


# -----------------------------
# Edit Expense
# -----------------------------
@app.route("/edit/<int:id>", methods=["GET", "POST"])
@login_required
def edit_expense(id):
    conn = get_db_connection()
    user_id = session.get("user_id")

    if request.method == "POST":
        date = request.form.get("date")
        item = request.form.get("item")
        category_id = request.form.get("category_id")
        amount = request.form.get("amount")

        cursor = conn.execute(
            """
            UPDATE expenses
            SET date = ?, item = ?, category_id = ?, amount = ?
            WHERE id = ? AND user_id = ?
            """,
            (date, item, category_id, amount, id, user_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            conn.close()
            flash("Expense not found.", "danger")
            return redirect(url_for("all_expenses"))
        conn.close()
        return redirect(url_for("all_expenses"))

    expense = conn.execute(
        """
        SELECT expenses.id, date, item, amount, category_id
        FROM expenses
        WHERE expenses.id = ? AND expenses.user_id = ?
        """,
        (id, user_id),
    ).fetchone()
    conn.close()
    if not expense:
        flash("Expense not found.", "danger")
        return redirect(url_for("all_expenses"))

    categories = get_categories()

    return render_template(
        "edit_expense.html",
        expense=expense,
        categories=categories,
    )


# -----------------------------
# Categories + Reassignment (D)
# -----------------------------
@app.route("/categories")
@login_required
def categories_view():
    conn = get_db_connection()
    user_id = session.get("user_id")
    rows = conn.execute(
        """
        SELECT categories.id,
               categories.name,
               COUNT(expenses.id) AS usage_count
        FROM categories
        LEFT JOIN expenses
            ON categories.id = expenses.category_id
            AND expenses.user_id = ?
        GROUP BY categories.id
        ORDER BY categories.name
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return render_template("categories.html", categories=rows)


@app.route("/add-category", methods=["POST"])
@login_required
def add_category():
    name = request.form.get("name", "").strip()

    if not name:
        flash("Category name cannot be empty.", "warning")
        return redirect(url_for("categories_view"))

    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO categories (name) VALUES (?)",
            (name,),
        )
        conn.commit()
        flash("Category added successfully.", "success")
    except sqlite3.IntegrityError:
        flash("Category already exists.", "danger")
    finally:
        conn.close()

    return redirect(url_for("categories_view"))


@app.route("/delete-category/<int:id>", methods=["GET", "POST"])
@login_required
def delete_category(id):
    conn = get_db_connection()
    user_id = session.get("user_id")

    # Check usage
    usage_total = conn.execute(
        "SELECT COUNT(*) AS c FROM expenses WHERE category_id = ?",
        (id,),
    ).fetchone()["c"]
    usage_for_user = conn.execute(
        "SELECT COUNT(*) AS c FROM expenses WHERE category_id = ? AND user_id = ?",
        (id, user_id),
    ).fetchone()["c"]

    # If no usage, just delete
    if usage_total == 0 and request.method == "GET":
        conn.execute("DELETE FROM categories WHERE id = ?", (id,))
        conn.commit()
        conn.close()
        flash("Category deleted.", "success")
        return redirect(url_for("categories_view"))

    # If in use: show reassignment form (GET)
    if request.method == "GET":
        if usage_for_user == 0:
            conn.close()
            flash("Category is in use and cannot be deleted.", "warning")
            return redirect(url_for("categories_view"))
        categories = conn.execute(
            "SELECT id, name FROM categories WHERE id != ? ORDER BY name",
            (id,),
        ).fetchall()
        cat = conn.execute(
            "SELECT id, name FROM categories WHERE id = ?",
            (id,),
        ).fetchone()
        conn.close()
        if not cat:
            flash("Category not found.", "danger")
            return redirect(url_for("categories_view"))
        flash("Category is in use. Please reassign its expenses before delete.", "warning")
        return render_template(
            "reassign_category.html",
            category=cat,
            categories=categories,
            usage=usage_for_user,
        )

    # POST: reassign and delete
    new_category_id = request.form.get("new_category_id")

    if not new_category_id:
        conn.close()
        flash("Select a category to reassign.", "warning")
        return redirect(url_for("delete_category", id=id))

    # Reassign all expenses from old category to new_category_id
    conn.execute(
        """
        UPDATE expenses
        SET category_id = ?
        WHERE category_id = ? AND user_id = ?
        """,
        (new_category_id, id, user_id),
    )
    conn.commit()

    usage_total = conn.execute(
        "SELECT COUNT(*) AS c FROM expenses WHERE category_id = ?",
        (id,),
    ).fetchone()["c"]
    if usage_total == 0:
        conn.execute("DELETE FROM categories WHERE id = ?", (id,))
        conn.commit()
        flash("Expenses reassigned and category deleted.", "success")
    else:
        flash(
            "Expenses reassigned. Category is still in use and cannot be deleted.",
            "warning",
        )
    conn.close()
    return redirect(url_for("categories_view"))


# -----------------------------
# Main
# -----------------------------

# if __name__ == "__main__":
#     init_db()
#     app.run(debug=True)

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
