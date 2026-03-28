import re

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth import bp
from app.extensions import db
from app.models.company import Company
from app.models.user import User


def _unique_company_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "company"
    candidate = base
    suffix = 2
    while Company.query.filter_by(slug=candidate).first() is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


@bp.get("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.projects"))
    return render_template("auth/landing.html")


@bp.get("/register")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.projects"))
    return render_template("auth/register.html")


@bp.post("/register")
def register_post():
    company_name = (request.form.get("company_name") or "").strip()
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""

    if not company_name or not name or not email or not password:
        flash("Alle Pflichtfelder ausfüllen.", "error")
        return render_template("auth/register.html", form_data=request.form)

    if len(password) < 8:
        flash("Passwort muss mindestens 8 Zeichen haben.", "error")
        return render_template("auth/register.html", form_data=request.form)

    if password != password_confirm:
        flash("Passwörter stimmen nicht überein.", "error")
        return render_template("auth/register.html", form_data=request.form)

    if User.query.filter_by(email=email).first():
        flash("Diese E-Mail-Adresse ist bereits registriert.", "error")
        return render_template("auth/register.html", form_data=request.form)

    company = Company(name=company_name, slug=_unique_company_slug(company_name))
    db.session.add(company)
    db.session.flush()  # get company.id

    user = User(name=name, email=email, company_id=company.id, role="admin")
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    login_user(user, remember=True)
    flash(f"Willkommen, {user.name}! Firma \"{company.name}\" wurde angelegt.", "success")
    return redirect(url_for("dashboard.projects"))


@bp.get("/login")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.projects"))
    return render_template("auth/login.html")


@bp.post("/login")
def login_post():
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        flash("E-Mail oder Passwort falsch.", "error")
        return render_template("auth/login.html", form_email=email)

    login_user(user, remember=remember)
    next_page = request.args.get("next")
    # Safety: only allow relative redirects
    if next_page and next_page.startswith("/"):
        return redirect(next_page)
    return redirect(url_for("dashboard.projects"))


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Erfolgreich abgemeldet.", "success")
    return redirect(url_for("auth.landing"))
