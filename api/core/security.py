"""Autenticação JWT recebida do Supabase Auth."""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable

import jwt
from flask import g, jsonify, request


def _token_from_request() -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _decode_token(token: str) -> dict[str, Any]:
    secret = os.getenv("JWT_SECRET") or os.getenv("SUPABASE_JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET ou SUPABASE_JWT_SECRET não configurado no backend.")
    if os.getenv("JWT_SECRET"):
        return jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    return jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated",
                      options={"require": ["exp", "sub"]})


def require_auth(view: Callable[..., Any]) -> Callable[..., Any]:
    """Exige um Bearer JWT válido e disponibiliza as claims em ``flask.g``."""

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        token = _token_from_request()
        if not token:
            return jsonify({"success": False, "message": "Token de autenticação não informado."}), 401
        try:
            claims = _decode_token(token)
        except jwt.ExpiredSignatureError:
            return jsonify({"success": False, "message": "Token expirado."}), 401
        except (jwt.InvalidTokenError, RuntimeError):
            return jsonify({"success": False, "message": "Token inválido."}), 401

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.isascii() or not subject.isdecimal() or len(subject) > 18:
            return jsonify({"success": False, "message": "Token inválido."}), 401

        g.auth_user = claims
        g.auth_user_id = claims.get("sub")
        return view(*args, **kwargs)

    return wrapped
