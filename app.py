"""Ponto de entrada da API Flask EcliPsi."""

from __future__ import annotations

import os

from flask import Flask, g, jsonify
from flask_cors import CORS

from api.core.config import cors_origins
from api.core.security import require_auth
from api.v1.router import register_api_routes


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app, resources={r"/api/*": {"origins": cors_origins()}})
    register_api_routes(app)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.errorhandler(500)
    def internal_error(_error):
        return jsonify(success=False, message="Erro interno."), 500

    @app.get("/health")
    def health():
        return jsonify({"success": True, "service": "eclipsi-api"}), 200

    @app.get("/api/me")
    @require_auth
    def current_user():
        conn = None
        try:
            from api.core.database import get_db

            conn = get_db()
            cur = conn.cursor()
            try:
                profile_user_id = int(g.auth_user_id)
            except (TypeError, ValueError):
                profile_user_id = g.auth_user_id
            cur.execute(
                "select id, nome, email, telefone, role, created_at, foto "
                "from public.users where id = %s",
                (profile_user_id,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({
                    "success": False,
                    "message": "Perfil não encontrado.",
                }), 404
            return jsonify({
                "success": True,
                "user": {
                    "id": str(row[0]),
                    "nome": row[1],
                    "email": row[2],
                    "telefone": row[3],
                    "role": row[4],
                    "created_at": row[5].isoformat() if row[5] else None,
                    "foto": row[6],
                },
            }), 200
        except Exception:
            return jsonify({
                "success": False,
                "message": "Não foi possível consultar o perfil.",
            }), 500
        finally:
            if conn:
                conn.close()

    @app.get("/api/v1/openapi.json")
    def openapi():
        bearer = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
        paths = {}

        def add(path, method, description, payload=None, responses=None):
            operation = {
                "responses": responses or {
                    "200": {"description": "Sucesso"},
                    "400": {"description": "Payload inválido"},
                    "401": {"description": "Não autenticado"},
                    "403": {"description": "Sem permissão"},
                    "404": {"description": "Recurso não encontrado"},
                    "500": {"description": "Erro interno"},
                }
            }
            if payload:
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": payload}},
                }
            if method in ("get",):
                operation.pop("requestBody", None)
            paths.setdefault(path, {})[method] = operation

        add("/health", "get", "Health check")
        add("/api/me", "get", "Perfil atual no PostgreSQL")
        add("/api/v1/auth/login", "post", "Login por JWT compatível", {
            "type": "object",
            "required": ["email", "senha"],
            "properties": {"email": {"type": "string"}, "senha": {"type": "string"}},
        })
        add("/api/v1/auth/register/user", "post", "Cadastro de paciente", {
            "type": "object",
            "required": ["nome", "email", "senha"],
            "properties": {"nome": {"type": "string"}, "email": {"type": "string"}, "senha": {"type": "string"}, "telefone": {"type": "string"}},
        })
        add("/api/v1/auth/register/doctor", "post", "Cadastro de profissional", {
            "type": "object",
            "required": ["nome", "email", "senha"],
            "properties": {"nome": {"type": "string"}, "email": {"type": "string"}, "senha": {"type": "string"}, "especialidade": {"type": "string"}, "registro": {"type": "string"}, "uf": {"type": "string"}, "valor_hora": {"type": "number"}},
        })
        add("/api/v1/profile/me", "get", "Perfil atual")
        add("/api/v1/profile/me", "patch", "Atualização de perfil", {
            "type": "object",
            "properties": {"nome": {"type": "string"}, "telefone": {"type": "string"}, "idade": {"type": "integer"}, "genero": {"type": "string"}, "especialidade": {"type": "string"}, "valor_hora": {"type": "number"}},
        })
        add("/api/v1/doctors", "get", "Lista de profissionais")
        add("/api/v1/appointments", "get", "Lista de consultas")
        add("/api/v1/appointments", "post", "Cria consulta", {
            "type": "object",
            "required": ["medico_id", "data_hora"],
            "properties": {"medico_id": {"type": "integer"}, "data_hora": {"type": "string", "format": "date-time"}},
        })
        add("/api/v1/appointments/{appointment_id}", "get", "Detalha consulta")
        add("/api/v1/appointments/{appointment_id}", "patch", "Atualiza consulta", {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["agendada", "concluida", "realizada", "cancelada"]}, "anotacoes": {"type": "string"}},
        })
        add("/api/v1/appointments/{appointment_id}", "delete", "Exclui consulta")

        for path, method, description in (
            ("", "get", ""),
        ):
            pass

        routes = (
            ("GET", "/api/v1/admin/stats"),
            ("GET", "/api/v1/admin/users"),
            ("PATCH", "/api/v1/admin/users/{user_id}",),
            ("DELETE", "/api/v1/admin/users/{user_id}",),
            ("GET", "/api/v1/doctor/dashboard"),
            ("GET", "/api/v1/doctor/appointments"),
            ("GET", "/api/v1/doctor/earnings"),
            ("GET", "/api/v1/professionals/{professional_id}/availability"),
            ("POST", "/api/v1/professional/availability"),
            ("DELETE", "/api/v1/professional/availability/{slot_id}"),
            ("POST", "/api/v1/patient/appointments"),
            ("POST", "/api/v1/patient/triage"),
            ("GET", "/api/v1/patient/doctors"),
            ("GET", "/api/v1/patient/doctors/recommended"),
            ("POST", "/api/v1/payments/pix/create"),
            ("POST", "/api/v1/payments/pix/{payment_id}/confirm"),
            ("POST", "/api/v1/payments/webhook/mercadopago"),
            ("POST", "/api/v1/payments"),
            ("GET", "/api/v1/payments"),
            ("PATCH", "/api/v1/payments/{payment_id}"),
            ("GET", "/api/v1/chat/conversations"),
            ("POST", "/api/v1/chat/conversations"),
            ("GET", "/api/v1/chat/conversations/{conversation_id}/messages"),
            ("POST", "/api/v1/chat/conversations/{conversation_id}/messages"),
            ("GET", "/api/v1/quiz/questions"),
            ("POST", "/api/v1/quiz/attempts"),
            ("GET", "/api/v1/quiz/history"),
            ("POST", "/api/v1/reviews"),
            ("GET", "/api/v1/reviews/doctor/{doctor_id}"),
        )
        for route in routes:
            method = route[0].lower()
            add(route[1], method, "Endpoint registrado no Flask")
        paths["/api/v1/payments/webhook/mercadopago"]["post"]["security"] = []
        return jsonify({
            "openapi": "3.0.3",
            "info": {"title": "EcliPsi API", "version": "1.0.0"},
            "servers": [{"url": "/"}],
            "components": {
                "securitySchemes": {"bearerAuth": bearer},
                "schemas": {
                    "Error": {
                        "type": "object",
                        "properties": {
                            "success": {"type": "boolean", "example": False},
                            "message": {"type": "string"},
                        },
                    }
                },
            },
            "security": [{"bearerAuth": []}],
            "paths": paths,
        }), 200

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
