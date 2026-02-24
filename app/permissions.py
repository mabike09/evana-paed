# app/permissions.py
from functools import wraps
from flask import abort
from flask_login import current_user
from .extensions import login_manager

ROLE_ALIASES = {
    "receptionist": "reception",
}


def _canonical_role(role):
    return ROLE_ALIASES.get((role or "").strip().lower(), (role or "").strip().lower())

def roles_required(*roles):
    """Admins have access everywhere."""
    allowed_roles = {_canonical_role(role) for role in roles}

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            user_role = _canonical_role(getattr(current_user, "role", None))
            if user_role != "admin" and user_role not in allowed_roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator
