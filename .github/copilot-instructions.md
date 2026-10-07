# Flask Project Conventions

Follow the Rode Kruis Flask and shared Python best practices: https://github.com/rodekruis/python-knowledge-base/tree/main/flask

- Use Python 3.12+, `uv`, Ruff, ty, and pytest. Commit `uv.lock` and use `uv run` for project commands.
- Keep the `src/build_a_hia/` layout, `create_app()` factory, deferred extension initialization, and blueprints.
- Keep route handlers thin. Put business logic in services that do not import Flask.
- Use config classes and read application settings through Flask config. Never commit secrets; `.env` is local-only and `example.env` is the template.
- Validate form input with WTForms and CSRF protection. Use Post/Redirect/Get after successful form submissions.
- Keep WSGI entrypoint `wsgi.py` minimal. Production runs through Gunicorn, never Flask's development server.
- Add focused pytest coverage for behavior changes. Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and `uv run python -m pytest` before submitting.
- Do not log secrets or personal data, expose internal errors, or add unreviewed dependencies.