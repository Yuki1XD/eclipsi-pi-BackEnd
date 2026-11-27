"""Consultas reutilizáveis relacionadas a perfis e papéis de acesso."""

from __future__ import annotations

from typing import Any


def role_for_user(cursor: Any, user_id: str) -> str | None:
    cursor.execute("select role from public.users where id = %s", (int(user_id),))
    row = cursor.fetchone()
    return row[0] if row else None


def is_admin(cursor: Any, user_id: str) -> bool:
    return role_for_user(cursor, user_id) == "admin"


def doctor_profile(cursor: Any, user_id: str) -> Any | None:
    cursor.execute(
        """select id, nome, especialidade, valor_hora
           from public.users where id = %s and role in ('medico', 'profissional')""",
        (int(user_id),),
    )
    return cursor.fetchone()


def has_role(cursor: Any, claims: dict, *roles: str) -> bool:
    """Require both the signed role and the current database role."""
    return claims.get("role") in roles and role_for_user(cursor, claims["sub"]) in roles
