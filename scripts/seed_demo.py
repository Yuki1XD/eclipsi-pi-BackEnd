"""Aplica a migração e os dados de demonstração em um banco de desenvolvimento."""

import argparse
import os
from pathlib import Path

import psycopg2

MIGRATION = Path(__file__).resolve().parents[1] / "supabase/migrations/20261124_full_mvp.sql"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", required=True, help="Autoriza inserir os perfis fictícios.")
    parser.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("Configure DATABASE_URL para o banco de demonstração.")
    conn = psycopg2.connect(dsn, sslmode="require")
    try:
        with conn.cursor() as cur:
            conn.autocommit = True
            cur.execute("set eclipsi.seed_demo = 'on'")
            cur.execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()
    print("Migração aplicada e perfis de demonstração atualizados.")


if __name__ == "__main__":
    main()
