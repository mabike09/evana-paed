# app/permissions.py
from functools import wraps
from flask import abort
from flask_login import current_user
from .extensions import login_manager

def roles_required(*roles):
    """Admins have access everywhere."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role != "admin" and current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator
