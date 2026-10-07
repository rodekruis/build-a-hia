FROM python:3.12-slim

# OpenGL, X11 client and GLib libraries needed by Docling's PDF and image processing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libxcb1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
COPY wsgi.py ./
RUN uv sync --frozen --no-dev --no-editable

# Every job execution starts a fresh replica, so models must be in the image.
RUN .venv/bin/python -m build_a_hia.worker prefetch-models
ENV HF_HUB_OFFLINE=1

EXPOSE 8000
# Jobs override the command, e.g. "/app/.venv/bin/hia-worker convert".
CMD [".venv/bin/gunicorn", "wsgi:app", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120"]
