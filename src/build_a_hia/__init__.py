"""Build a HIA: turn sources into a reviewable draft for a Helpful Information App."""

from flask import Flask

from .config import Config
from .extensions import csrf
from .services.container import build_services
from .services.settings import Settings


def create_app(config_class: type[Config] = Config) -> Flask:
    """Create and configure the Flask application.

    Args:
        config_class: Config class to load settings from.

    Returns:
        The configured app with CSRF protection, services, blueprints, error handlers and
        security-header middleware registered.

    Raises:
        RuntimeError: If `SECRET_KEY` is not set.
    """
    app = Flask(__name__)
    app.config.from_object(config_class)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be set before starting the application")

    csrf.init_app(app)
    app.extensions["hia_services"] = build_services(Settings.from_mapping(app.config))

    from .errors import register_error_handlers
    from .main.routes import main_bp
    from .middleware import register_middleware
    from .workspace.drafting import drafting_bp
    from .workspace.review import review_bp
    from .workspace.routes import workspace_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(workspace_bp)
    app.register_blueprint(drafting_bp)
    app.register_blueprint(review_bp)
    register_error_handlers(app)
    register_middleware(app)
    return app
