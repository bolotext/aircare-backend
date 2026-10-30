from pathlib import Path

# The one .env for the whole backend, at <repo>/aircare_backend/.env — an absolute
# path so settings load the same values whatever directory the process is started from.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
