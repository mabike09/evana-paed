# app/routes/auth.py
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import current_user, login_user, logout_user
from sqlalchemy.exc import IntegrityError
from ..forms import LoginForm, UserEditForm, UserForm
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
        elif not u.is_active:
            flash("This user account is deactivated. Please contact an administrator.", "danger")
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


@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
def users_edit(user_id):
    from ..permissions import roles_required
    @roles_required("admin")
    def impl():
        user = User.query.get_or_404(user_id)
        form = UserEditForm(obj=user)
        if form.validate_on_submit():
            username = form.username.data.strip()
            email = form.email.data.strip()
            username_owner = User.query.filter(User.username == username, User.id != user.id).first()
            email_owner = User.query.filter(User.email == email, User.id != user.id).first()
            if username_owner:
                flash("That username is already taken.", "danger")
                return render_template("users_edit.html", form=form, user=user)
            if email_owner:
                flash("That email is already registered.", "danger")
                return render_template("users_edit.html", form=form, user=user)
            if user.id == current_user.id and not form.is_active.data:
                flash("You cannot deactivate your own account.", "danger")
                return render_template("users_edit.html", form=form, user=user)

            user.username = username
            user.email = email
            user.role = form.role.data
            user.is_active = bool(form.is_active.data)
            if form.password.data:
                user.set_password(form.password.data)
            try:
                db.session.commit()
                flash(f"User '{user.username}' updated.", "success")
                return redirect(url_for("auth.users_list"))
            except IntegrityError:
                db.session.rollback()
                flash("Could not update user due to a database constraint.", "danger")
        return render_template("users_edit.html", form=form, user=user)
    return impl()


@bp.route("/users/<int:user_id>/activate", methods=["POST"])
def users_activate(user_id):
    from ..permissions import roles_required
    @roles_required("admin")
    def impl():
        user = User.query.get_or_404(user_id)
        user.is_active = True
        db.session.commit()
        flash(f"User '{user.username}' activated.", "success")
        return redirect(url_for("auth.users_list"))
    return impl()


@bp.route("/users/<int:user_id>/deactivate", methods=["POST"])
def users_deactivate(user_id):
    from ..permissions import roles_required
    @roles_required("admin")
    def impl():
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash("You cannot deactivate your own account.", "danger")
        else:
            user.is_active = False
            db.session.commit()
            flash(f"User '{user.username}' deactivated.", "success")
        return redirect(url_for("auth.users_list"))
    return impl()


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
def users_delete(user_id):
    from ..permissions import roles_required
    @roles_required("admin")
    def impl():
        user = User.query.get_or_404(user_id)
        if user.id == current_user.id:
            flash("You cannot delete your own account.", "danger")
        else:
            username = user.username
            try:
                db.session.delete(user)
                db.session.commit()
                flash(f"User '{username}' deleted.", "success")
            except IntegrityError:
                db.session.rollback()
                flash("Could not delete this user because existing records reference the account. Deactivate the user instead.", "danger")
        return redirect(url_for("auth.users_list"))
    return impl()
