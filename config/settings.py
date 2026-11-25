import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
# This application does not use authentication, sessions or signed user data.
# Set a private key before any public deployment.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-assessment-development-only")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
INSTALLED_APPS = ["django.contrib.staticfiles", "fuel"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
    }
]
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "var" / "static"
USE_TZ = True
TIME_ZONE = "UTC"
DATA_UPLOAD_MAX_MEMORY_SIZE = 16_384
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "fuel-routes",
        "TIMEOUT": 3600,
        "OPTIONS": {"MAX_ENTRIES": 128},
    }
}
OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
ROUTE_CACHE_TTL = int(os.environ.get("ROUTE_CACHE_TTL", "3600"))
STATION_DATA_PATH = Path(os.environ.get("STATION_DATA_PATH", BASE_DIR / "data" / "stations.json"))
CITY_DATA_PATH = BASE_DIR / "data" / "cities.json"
CITY_OVERRIDES_PATH = BASE_DIR / "data" / "city_overrides.json"
COUNTRY_DATA_PATH = BASE_DIR / "data" / "us-boundary.geojson"
SECURE_CONTENT_TYPE_NOSNIFF = True
# OSM's browser tile policy requires a genuine Referer. Send only the origin
# cross-site, never the map ID or any request details in a path/query string.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
