import os

from dotenv import load_dotenv

load_dotenv()

def get_admin_config():
    username = os.getenv('ADMIN_USERNAME')
    password_hash = os.getenv('ADMIN_PASSWORD_HASH')
    if not username or not password_hash:
        return None
    return {
        'username': username,
        'password_hash': password_hash
    }

__all__ = [
    'get_admin_config',
]
