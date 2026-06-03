import functools
import os

from dotenv import load_dotenv
from flask import flash, redirect, url_for
from flask_login import current_user

load_dotenv()

ROLES = ('cook', 'chef', 'admin')


def _role_rank(role):
    try:
        return ROLES.index(role or 'cook')
    except ValueError:
        return 0


def role_required(min_role):
    """Decorator for view functions: redirects if current_user's role is below min_role."""
    def decorator(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if _role_rank(current_user.role) < _role_rank(min_role):
                flash('You do not have permission to access that page.', 'error')
                return redirect(url_for('recipes.recipes'))
            return f(*args, **kwargs)
        return wrapped
    return decorator


def require_role(min_role):
    """Call from a blueprint before_request to gate the entire blueprint."""
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    if _role_rank(current_user.role) < _role_rank(min_role):
        flash('You do not have permission to access that page.', 'error')
        return redirect(url_for('recipes.recipes'))
    return None


def get_user_by_id(cur, user_id):
    cur.execute(
        "SELECT id, username, role, active FROM users WHERE id = %s LIMIT 1",
        (user_id,),
    )
    return cur.fetchone()


def get_user_by_username(cur, username):
    cur.execute(
        "SELECT id, username, password_hash, role, active FROM users WHERE username = %s LIMIT 1",
        (username,),
    )
    return cur.fetchone()


def get_admin_config():
    """Legacy helper kept for any callers that haven't been updated yet."""
    username = os.getenv('ADMIN_USERNAME')
    password_hash = os.getenv('ADMIN_PASSWORD_HASH')
    if not username or not password_hash:
        return None
    return {'username': username, 'password_hash': password_hash}


__all__ = [
    'ROLES',
    'role_required',
    'require_role',
    'get_user_by_id',
    'get_user_by_username',
    'get_admin_config',
]
