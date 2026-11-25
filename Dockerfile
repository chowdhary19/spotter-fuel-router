FROM python:3.13-slim
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY . .
RUN .venv/bin/python manage.py collectstatic --noinput \
    && useradd --uid 10001 --create-home appuser
USER appuser
EXPOSE 8000
# One process is deliberate: local cache and public-provider rate gate are shared.
CMD [".venv/bin/gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "60", "--no-control-socket", "--access-logfile", "-"]
