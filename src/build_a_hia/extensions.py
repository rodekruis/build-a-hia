"""Flask extension instances, initialised on the app in `create_app`."""

from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect()
