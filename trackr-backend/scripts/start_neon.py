"""Run the existing FastAPI backend with the environment pulled by `neon deploy`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    backend = Path(__file__).resolve().parents[1]
    root = backend.parent
    neon_env = root / (".env" if (root / ".env").is_file() else ".env.local")
    if not neon_env.is_file():
        raise SystemExit("Neon environment is missing. Run `neon deploy` from the project root.")
    neon_values = dotenv_values(neon_env)
    branch = os.environ.get("NEON_BRANCH", neon_values.get("NEON_BRANCH", ""))
    default_app_env = backend / (".env.vercel" if branch == "production" else ".env")
    app_env = Path(os.environ.get("TRACKR_APP_ENV_FILE", str(default_app_env)))
    if not app_env.is_file():
        raise SystemExit("The selected application environment file does not exist.")

    # Keep the app's existing authentication, encryption, and storage configuration.
    # Neon overrides the local database; explicit shell configuration has priority.
    environment = {
        key: value
        for source in (dotenv_values(app_env), neon_values, os.environ)
        for key, value in source.items()
        if value is not None
    }
    if not environment.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is missing. Run `neon env pull` from the project root.")
    os.chdir(backend)
    os.execvpe(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            environment.get("TRACKR_API_PORT", "8001"),
        ],
        environment,
    )


if __name__ == "__main__":
    main()
