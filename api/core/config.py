"""Configurações centralizadas da API Flask."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
ENV_FILE = BACKEND_DIR / ".env"

load_dotenv(ENV_FILE)


def cors_origins() -> list[str]:
    return ["https://eclipsi.dev", "https://www.eclipsi.dev", "http://localhost:8081"]


def database_config() -> dict[str, str | None]:
    """Dados de conexão PostgreSQL/Supabase lidos do ambiente."""
    return {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME", "postgres"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "sslmode": "require",
    }


def database_url() -> str | None:
    """Retorna a DSN direta do ambiente, quando configurada."""
    return os.getenv("DATABASE_URL")
