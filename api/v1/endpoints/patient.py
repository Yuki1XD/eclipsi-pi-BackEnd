from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request
from psycopg2 import errors, sql
from uuid import UUID

from api.core.database import get_db
from api.core.security import require_auth
from api.schemas.domain import APPOINTMENT_STATUSES, PROFILE_UPDATE_FIELDS


bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

PROFILE_UPDATE_SETTERS = {
    "nome": "nome = %s",
    "email": "email = %s",
    "foto": "foto = %s",
    "telefone": "telefone = %s",
    "idade": "idade = %s",
    "genero": "genero = %s",
    "descricao": "descricao = %s",
    "especialidade": "especialidade = %s",
    "registro": "registro = %s",
    "uf": "uf = %s",
    "valor_hora": "valor_hora = %s",
}


def _profile_role(cur, user_id: str):
    cur.execute(
        "select id, nome, email, role from public.users where id = %s",
        (int(user_id),),
    )
    return cur.fetchone()


def _doctor(row):
    return {
        "id": row[0],
        "nome": row[1],
        "especialidade": row[2],
        "registro": row[3],
        "uf": row[4],
        "descricao": row[5],
        "valor_hora": float(row[6]) if row[6] is not None else None,
        "foto": row[7] if len(row) > 7 else None,
    }


def _appointment_row(row):
    return {
        "id": row[0],
        "data_hora": row[1].isoformat(),
        "valor": float(row[2]),
        "status": row[3],
        "anotacoes": row[4],
        "paciente_id": row[5],
        "paciente_nome": row[6],
        "medico_id": row[7],
        "medico_nome": row[8],
        "especialidade": row[9],
    }


@bp.get("/profile/me")
@require_auth
def my_profile():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """select id, nome, telefone, role, idade, genero, especialidade,
                      registro, uf, descricao, valor_hora, email, foto
               from public.users where id = %s""",
            (int(g.auth_user_id),),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "message": "Perfil não encontrado."}), 404
        return jsonify({
            "success": True,
            "profile": {
                "id": row[0],
                "nome": row[1],
                "telefone": row[2],
                "role": row[3],
                "idade": row[4],
                "genero": row[5],
                "especialidade": row[6],
                "registro": row[7],
                "uf": row[8],
                "descricao": row[9],
                "valor_hora": float(row[10]) if row[10] is not None else None,
                "email": row[11],
                "foto": row[12],
            },
        }), 200
    finally:
        conn.close()


@bp.patch("/profile/me")
@bp.put("/profile/me")
@require_auth
def update_my_profile():
    data = request.get_json() or {}
    if "role" in data or "tipo" in data:
        return jsonify({
            "success": False,
            "message": "O cargo só pode ser alterado pelo administrador.",
        }), 403

    values = {key: data[key] for key in PROFILE_UPDATE_FIELDS if key in data}
    if "nome" in values and (not isinstance(values["nome"], str) or not values["nome"].strip()):
        return jsonify({"success": False, "message": "Nome inválido."}), 400
    if not values:
        return jsonify({"success": False, "message": "Nenhum campo para atualizar."}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        set_clauses = [
            sql.SQL(PROFILE_UPDATE_SETTERS[key])
            for key in PROFILE_UPDATE_SETTERS
            if key in values
        ]
        set_values = [values[key] for key in PROFILE_UPDATE_SETTERS if key in values]
        cur.execute(
            sql.SQL("update public.users set {sets} where id = %s returning id")
            .format(sets=sql.SQL(", ").join(set_clauses)),
            (*set_values, int(g.auth_user_id)),
        )
        if not cur.fetchone():
            conn.rollback()
            return jsonify({"success": False, "message": "Perfil não encontrado."}), 404
        conn.commit()
        return jsonify({"success": True, "message": "Perfil atualizado."}), 200
    finally:
        conn.close()


@bp.get("/doctors")
@require_auth
def doctors():
    q = (request.args.get("q") or "").strip()[:100]
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """select id, nome, especialidade, registro, uf, descricao, valor_hora, foto
               from public.users
               where role in ('medico', 'profissional')
                 and (%s = '' or nome ilike %s
                      or coalesce(especialidade, '') ilike %s
                      or coalesce(descricao, '') ilike %s)
               order by nome""",
            (q, f"%{q}%", f"%{q}%", f"%{q}%"),
        )
        return jsonify({"success": True, "doctors": [_doctor(row) for row in cur.fetchall()]}), 200
    finally:
        conn.close()


@bp.post("/appointments")
@require_auth
def create_appointment():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, message="Informe os dados da consulta."), 400
    slot_id = data.get("slot_id")
    strict = slot_id is not None or request.path == "/api/v1/patient/appointments"
    try:
        if slot_id is not None:
            slot_id = str(UUID(str(slot_id)))
            doctor_id = int(data["medico_id"]) if "medico_id" in data else None
            when = None
        else:
            doctor_id = int(data["medico_id"])
            when = datetime.fromisoformat(str(data["data_hora"]).replace("Z", "+00:00"))
            if when.utcoffset() is None:
                raise ValueError("Data sem fuso horário")
            if when <= datetime.now(timezone.utc):
                status = 409 if strict else 400
                return jsonify(success=False, message="Escolha uma data futura."), status
    except (KeyError, ValueError, TypeError, OverflowError):
        return jsonify(success=False, message="Médico, horário ou slot inválido."), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        profile = _profile_role(cur, g.auth_user_id)
        if not profile or profile[3] != "paciente" or (strict and g.auth_user.get("role") != "paciente"):
            return jsonify({
                "success": False,
                "message": "Somente pacientes podem agendar consultas.",
            }), 403
        if strict:
            cur.execute(
                """select professional_id, data_hora from public.disponibilidades
                   where ativo and data_hora > now()
                     and ((%s::uuid is not null and id = %s::uuid)
                          or (%s::uuid is null and professional_id = %s and data_hora = %s))
                   for update""",
                (slot_id, slot_id, slot_id, doctor_id, when),
            )
            slot = cur.fetchone()
            if not slot or (doctor_id is not None and doctor_id != slot[0]):
                return jsonify(success=False, message="Horário indisponível."), 409
            doctor_id, when = slot
        cur.execute(
            "select id, valor_hora from public.users "
            "where id = %s and role in ('medico', 'profissional')",
            (doctor_id,),
        )
        doctor = cur.fetchone()
        if not doctor:
            return jsonify({"success": False, "message": "Profissional não encontrado."}), 404
        cur.execute(
            """insert into public.consultas
               (paciente_id, medico_id, data_hora, valor)
               values (%s, %s, %s, %s)
               returning id, data_hora, valor, status""",
            (int(g.auth_user_id), doctor_id, when, doctor[1] or 0),
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({
            "success": True,
            "appointment": {
                "id": row[0],
                "data_hora": row[1].isoformat(),
                "valor": float(row[2]),
                "status": row[3],
            },
        }), 201
    except errors.UniqueViolation:
        conn.rollback()
        return jsonify(success=False, message="Este horário já está ocupado."), 409
    finally:
        conn.close()


@bp.get("/appointments")
@require_auth
def list_appointments():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """select c.id, c.data_hora, c.valor, c.status, c.anotacoes,
                      p.id, p.nome, d.id, d.nome, d.especialidade
               from public.consultas c
               join public.users p on p.id = c.paciente_id
               join public.users d on d.id = c.medico_id
               where c.paciente_id = %s or c.medico_id = %s
               order by c.data_hora""",
            (int(g.auth_user_id), int(g.auth_user_id)),
        )
        return jsonify({
            "success": True,
            "appointments": [_appointment_row(row) for row in cur.fetchall()],
        }), 200
    finally:
        conn.close()


@bp.get("/appointments/<int:appointment_id>")
@require_auth
def get_appointment(appointment_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """select c.id, c.data_hora, c.valor, c.status, c.anotacoes,
                      p.id, p.nome, d.id, d.nome, d.especialidade
               from public.consultas c
               join public.users p on p.id = c.paciente_id
               join public.users d on d.id = c.medico_id
               where c.id = %s and (c.paciente_id = %s or c.medico_id = %s)""",
            (appointment_id, int(g.auth_user_id), int(g.auth_user_id)),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "message": "Consulta não encontrada."}), 404
        return jsonify({"success": True, "appointment": _appointment_row(row)}), 200
    finally:
        conn.close()


@bp.patch("/appointments/<int:appointment_id>")
@require_auth
def update_appointment(appointment_id):
    data = request.get_json() or {}
    status = data.get("status")
    anotacoes = data.get("anotacoes")

    if status is not None and status not in APPOINTMENT_STATUSES:
        return jsonify({"success": False, "message": "Status inválido."}), 400
    if status is None and anotacoes is None:
        return jsonify({"success": False, "message": "Informe status ou anotacoes."}), 400

    conn = get_db()
    try:
        cur = conn.cursor()
        profile = _profile_role(cur, g.auth_user_id)
        if not profile:
            return jsonify({"success": False, "message": "Perfil não encontrado."}), 404
        role = profile[3]

        cur.execute(
            "select id, paciente_id, medico_id, status "
            "from public.consultas where id = %s",
            (appointment_id,),
        )
        appointment = cur.fetchone()
        if not appointment:
            return jsonify({"success": False, "message": "Consulta não encontrada."}), 404

        patient_id, doctor_id, current_status = appointment[1], appointment[2], appointment[3]
        user_id = int(g.auth_user_id)
        linked = user_id in (patient_id, doctor_id)
        if not linked:
            return jsonify({"success": False, "message": "Consulta sem permissão."}), 403
        if str(current_status).lower() in ("concluida", "realizada", "cancelada"):
            return jsonify({
                "success": False,
                "message": "Consulta concluída ou cancelada não pode ser alterada.",
            }), 400

        if status in ("concluida", "realizada"):
            if role not in ("profissional", "medico") or user_id != doctor_id:
                return jsonify({
                    "success": False,
                    "message": "Somente o profissional vinculado pode concluir a consulta.",
                }), 403
        elif status == "cancelada":
            if user_id not in (patient_id, doctor_id):
                return jsonify({"success": False, "message": "Sem permissão para cancelar."}), 403

        assignments = []
        values = []
        if status is not None:
            assignments.append("status = %s")
            values.append(status)
        if anotacoes is not None:
            assignments.append("anotacoes = %s")
            values.append(anotacoes)
        cur.execute(
            "update public.consultas set "
            + ", ".join(assignments)
            + " where id = %s returning id, status, anotacoes",
            (*values, appointment_id),
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({
            "success": True,
            "appointment": {"id": row[0], "status": row[1], "anotacoes": row[2]},
        }), 200
    finally:
        conn.close()


@bp.delete("/appointments/<int:appointment_id>")
@require_auth
def delete_appointment(appointment_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        profile = _profile_role(cur, g.auth_user_id)
        role = profile[3] if profile else None
        cur.execute(
            "select paciente_id, medico_id from public.consultas where id = %s",
            (appointment_id,),
        )
        appointment = cur.fetchone()
        if not appointment:
            return jsonify({"success": False, "message": "Consulta não encontrada."}), 404

        user_id = int(g.auth_user_id)
        if role != "admin" and user_id not in (appointment[0], appointment[1]):
            return jsonify({"success": False, "message": "Consulta sem permissão."}), 403
        cur.execute(
            "delete from public.consultas where id = %s returning id",
            (appointment_id,),
        )
        cur.fetchone()
        conn.commit()
        return jsonify({"success": True, "message": "Consulta excluída."}), 200
    finally:
        conn.close()
