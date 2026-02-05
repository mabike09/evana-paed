# app/routes/auth.py
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import current_user, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from ..forms import LoginForm, UserForm
from ..extensions import db, login_manager
from ..models import User
from ..utils import landing_endpoint_for  # <-- NEW import

bp = Blueprint("auth", __name__)

@bp.route("/login", methods=["GET", "POST"])
def login():
    # If already authenticated, send to their role landing
    if current_user.is_authenticated:
        endpoint = landing_endpoint_for(current_user) or "patients.patients_list"
        return redirect(url_for(endpoint))

    form = LoginForm()
    if form.validate_on_submit():
        u = User.query.filter_by(username=form.username.data.strip()).first()
        if not u or not u.check_password(form.password.data):
            flash("Invalid username or password.", "danger")
        else:
            login_user(u)
            flash(f"Welcome back, {u.username}!", "success")

            # Respect ?next= if present and not pointing back to login
            next_url = request.args.get("next")
            if next_url and not next_url.endswith(url_for("auth.login")):
                return redirect(next_url)

            # Role-based landing (lab users -> lab.queue; doctors -> doctors_queue; else patients_list)
            endpoint = landing_endpoint_for(u) or "patients.patients_list"
            return redirect(url_for(endpoint))

    return render_template("login.html", form=form)

@bp.route("/logout")
def logout():
    logout_user()
    flash("Signed out.", "info")
    return redirect(url_for("auth.login"))

@bp.route("/users/new", methods=["GET", "POST"])
def users_new():
    from ..permissions import roles_required
    @roles_required("admin")
    def impl():
        form = UserForm()
        if form.validate_on_submit():
            if User.query.filter_by(username=form.username.data.strip()).first():
                flash("That username is already taken.", "danger")
                return render_template("users_new.html", form=form)
            if User.query.filter_by(email=form.email.data.strip()).first():
                flash("That email is already registered.", "danger")
                return render_template("users_new.html", form=form)
            try:
                u = User(username=form.username.data.strip(),
                         email=form.email.data.strip(),
                         role=form.role.data)
                u.set_password(form.password.data)
                db.session.add(u); db.session.commit()
                flash(f"User '{u.username}' created.", "success")
                return redirect(url_for("auth.users_list"))
            except IntegrityError:
                db.session.rollback()
                flash("Could not create user due to a database constraint.", "danger")
        return render_template("users_new.html", form=form)
    return impl()

@bp.route("/users")
def users_list():
    from ..permissions import roles_required
    @roles_required("admin")
    def impl():
        users = User.query.order_by(User.id.desc()).all()
        return render_template("users_list.html", users=users)
    return impl()
