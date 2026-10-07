"""WSGI entry point for Gunicorn (`gunicorn wsgi:app`)."""

from build_a_hia import create_app

app = create_app()
