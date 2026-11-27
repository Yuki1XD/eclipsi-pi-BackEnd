from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from unicodedata import normalize
from uuid import UUID

from flask import Blueprint, g, jsonify, request

from api.core.database import get_db
from api.core.security import require_auth
from api.services.profiles import has_role
from .patient import _doctor, create_appointment

bp = Blueprint("scheduling_v1", __name__, url_prefix="/api/v1")


def payload():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def problem(message, status):
    return jsonify(success=False, message=message), status


def parse_time(value):
    when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if when.utcoffset() is None:
        raise ValueError("Data sem fuso horário")
    return when


@bp.get("/professionals/<int:professional_id>/availability")
@require_auth
def availability(professional_id):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """select s.id, s.data_hora from public.disponibilidades s
                   join public.users u on u.id = s.professional_id
                   where s.professional_id = %s and s.ativo and s.data_hora > now()
                     and u.role in ('medico', 'profissional')
                     and not exists (
                       select 1 from public.consultas c
                       where c.medico_id = s.professional_id and c.data_hora = s.data_hora
                         and lower(c.status) is distinct from 'cancelada')
                   order by s.data_hora""",
                (professional_id,),
            )
            return jsonify(success=True, slots=[
                {"id": str(slot), "data_hora": when.isoformat()} for slot, when in cur.fetchall()
            ])
    finally:
        conn.close()


@bp.post("/professional/availability")
@require_auth
def add_availability():
    try:
        when = parse_time(payload().get("data_hora"))
        if when <= datetime.now(timezone.utc):
            raise ValueError("Data passada")
    except (ValueError, OverflowError):
        return problem("Informe uma data futura com fuso horário.", 400)
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            if not has_role(cur, g.auth_user, "medico", "profissional"):
                return problem("Acesso restrito a profissionais.", 403)
            cur.execute(
                """insert into public.disponibilidades (professional_id, data_hora)
                   values (%s, %s)
                   on conflict (professional_id, data_hora) do update set ativo = true
                   returning id, data_hora""",
                (int(g.auth_user_id), when),
            )
            slot, starts_at = cur.fetchone()
            return jsonify(success=True, slot={"id": str(slot), "data_hora": starts_at.isoformat()}), 201
    finally:
        conn.close()


@bp.delete("/professional/availability/<uuid:slot_id>")
@require_auth
def remove_availability(slot_id: UUID):
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            if not has_role(cur, g.auth_user, "medico", "profissional"):
                return problem("Acesso restrito a profissionais.", 403)
            cur.execute(
                """update public.disponibilidades set ativo = false
                   where id = %s and professional_id = %s returning id""",
                (str(slot_id), int(g.auth_user_id)),
            )
            if not cur.fetchone():
                return problem("Horário não encontrado.", 404)
            return jsonify(success=True)
    finally:
        conn.close()


bp.add_url_rule("/patient/appointments", view_func=create_appointment, methods=["POST"])


@bp.post("/patient/triage")
@require_auth
def save_triage():
    data = payload()
    fields = ("motivo_principal", "abordagem_preferida", "modalidade", "urgencia")
    if any(not isinstance(data.get(field), str) or not 1 <= len(data[field].strip()) <= 160
           for field in fields):
        return problem("Informe os campos da triagem.", 400)
    try:
        budget = Decimal(str(data.get("faixa_valor_max")))
        if not budget.is_finite() or not 0 < budget <= 999999:
            raise ValueError("Valor inválido")
    except (InvalidOperation, ValueError):
        return problem("Informe um orçamento válido.", 400)
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            if not has_role(cur, g.auth_user, "paciente"):
                return problem("Acesso restrito a pacientes.", 403)
            cur.execute(
                """insert into public.triagens
                     (patient_id, motivo_principal, abordagem_preferida, modalidade, urgencia, faixa_valor_max)
                   values (%s, %s, %s, %s, %s, %s)
                   on conflict (patient_id) do update set
                     motivo_principal = excluded.motivo_principal,
                     abordagem_preferida = excluded.abordagem_preferida,
                     modalidade = excluded.modalidade, urgencia = excluded.urgencia,
                     faixa_valor_max = excluded.faixa_valor_max, atualizado_em = now()""",
                (int(g.auth_user_id), *(data[field].strip() for field in fields), budget),
            )
            return jsonify(success=True)
    finally:
        conn.close()


def folded(value):
    return "".join(char for char in normalize("NFKD", (value or "").casefold())
                   if not 0x300 <= ord(char) <= 0x36f)


def compatibility(triage, doctor):
    motive, approach, modality, budget, urgency = triage
    specialty = folded(doctor[2])
    score = 0
    reasons = []
    criteria = (
        (any(word in specialty.split() for word in folded(motive).split() if len(word) > 3),
         35, "Especialidade compatível"),
        (folded(approach) == folded(doctor[8]), 25, "Abordagem " + (doctor[8] or "")),
        (folded(modality) == folded(doctor[9]) or folded(doctor[9]) == "hibrida",
         15, "Modalidade compatível"),
        (doctor[6] is not None and doctor[6] <= budget, 20, "Dentro do orçamento"),
        (folded(urgency) in {"alta", "urgente"} and doctor[10], 5, "Horário disponível nas próximas 48 horas"),
    )
    for matched, points, reason in criteria:
        if matched:
            score += points
            reasons.append(reason)
    return score, reasons


@bp.get("/patient/doctors")
@bp.get("/patient/doctors/recommended")
@require_auth
def recommended():
    conn = get_db()
    try:
        with conn.cursor() as cur:
            if not has_role(cur, g.auth_user, "paciente"):
                return problem("Acesso restrito a pacientes.", 403)
            cur.execute(
                """select motivo_principal, abordagem_preferida, modalidade, faixa_valor_max, urgencia
                   from public.triagens where patient_id = %s""", (int(g.auth_user_id),),
            )
            triage = cur.fetchone()
            q = (request.args.get("q") or "").strip()[:100]
            cur.execute(
                """select u.id, u.nome, u.especialidade, u.registro, u.uf, u.descricao, u.valor_hora, u.foto,
                          u.abordagem, u.modalidade,
                          exists(select 1 from public.disponibilidades s
                            where s.professional_id = u.id and s.ativo
                            and s.data_hora > now() and s.data_hora <= now() + interval '48 hours'
                            and not exists(select 1 from public.consultas c
                              where c.medico_id = u.id and c.data_hora = s.data_hora
                              and lower(c.status) is distinct from 'cancelada'))
                   from public.users u where u.role in ('medico', 'profissional')
                   and concat_ws(' ', u.nome, u.especialidade, u.descricao) ilike %s
                   order by u.nome, u.id""", (f"%{q}%",),
            )
            doctors = []
            for row in cur.fetchall():
                score, reasons = compatibility(triage, row) if triage else (0, [])
                doctors.append({**_doctor(row), "abordagem": row[8], "modalidade": row[9],
                                "compatibility_score": score, "match_reasons": reasons})
            doctors.sort(key=lambda doctor: -doctor["compatibility_score"])
            return jsonify(success=True, doctors=doctors)
    finally:
        conn.close()
