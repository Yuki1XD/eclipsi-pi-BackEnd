from binascii import crc_hqx
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

from flask import Blueprint, g, jsonify

from api.core.database import get_db
from api.core.security import require_auth
from api.services.profiles import has_role
from .scheduling import payload, problem

bp = Blueprint("pix_v1", __name__, url_prefix="/api/v1/payments")
CENT = Decimal("0.01")


def access_token():
    return os.getenv("MERCADO_PAGO_ACCESS_TOKEN", "").strip()


def tlv(tag, value):
    return f"{tag}{len(value.encode('utf-8')):02d}{value}"


def mock_pix(amount, reference):
    # Dynamic BR Code with a reserved .invalid domain. No bank can resolve a charge.
    merchant = tlv("00", "br.gov.bcb.pix") + tlv("25", f"pix-demo.invalid/{reference.hex}")
    code = (
        tlv("00", "01") + tlv("01", "12") + tlv("26", merchant)
        + tlv("52", "0000") + tlv("53", "986") + tlv("54", f"{amount:.2f}")
        + tlv("58", "BR") + tlv("59", "ECLIPSI DEMO") + tlv("60", "RECIFE")
        + tlv("62", tlv("05", "***")) + "6304"
    )
    return code + f"{crc_hqx(code.encode(), 0xFFFF):04X}"


class ProviderError(Exception):
    pass


def provider(token, path="", *, data=None, key=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if data is not None:
        headers.update({"Content-Type": "application/json", "X-Idempotency-Key": str(key)})
        body = json.dumps(data).encode()
    request = Request(f"https://api.mercadopago.com/v1/payments{path}", data=body, headers=headers)
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
    except (URLError, TimeoutError, ValueError) as exc:
        raise ProviderError from exc
    if not isinstance(result, dict):
        raise ProviderError
    return result


def payment_response(row, code=200):
    return jsonify(
        payment_id=str(row[0]), consulta_id=row[1], valor=float(row[2]), status=row[3],
        qr_code=row[4], qr_code_base64=row[5], mode=row[6],
    ), code


@bp.post("/pix/create")
@require_auth
def create_pix():
    value = payload().get("consulta_id")
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal() or len(str(value)) > 18:
        return problem("Informe o identificador da consulta.", 400)
    appointment_id = int(value)
    token = access_token()
    mode = "mercadopago" if token else "sandbox_mock"
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            if not has_role(cur, g.auth_user, "paciente"):
                return problem("Acesso restrito a pacientes.", 403)
            cur.execute(
                """select c.status, d.valor_hora, p.email from public.consultas c
                   join public.users d on d.id = c.medico_id
                   join public.users p on p.id = c.paciente_id
                   where c.id = %s and c.paciente_id = %s for update of c""",
                (appointment_id, int(g.auth_user_id)),
            )
            appointment = cur.fetchone()
            if not appointment:
                return problem("Consulta não encontrada.", 404)
            if str(appointment[0]).lower() == "cancelada":
                return problem("A consulta foi cancelada.", 409)
            cur.execute(
                """select pix_id, consulta_id, valor, status, codigo_pix, pix_qr_base64, pix_mode
                   from public.pagamentos where consulta_id = %s and pix_mode = %s""",
                (appointment_id, mode),
            )
            existing = cur.fetchone()
            if existing:
                return payment_response(existing)
            amount = Decimal(str(appointment[1] or 0))
            if not amount.is_finite() or not 0 < amount <= 999999:
                return problem("Valor da consulta indisponível.", 409)
            amount = amount.quantize(CENT, rounding=ROUND_HALF_UP)
            if not amount:
                return problem("Valor da consulta indisponível.", 409)
            reference = uuid5(NAMESPACE_URL, f"eclipsi.dev/pix/{mode}/{appointment_id}")
            provider_id = qr_base64 = None
            if token:
                result = provider(token, key=reference, data={
                    "transaction_amount": float(amount),
                    "payment_method_id": "pix",
                    "external_reference": str(reference),
                    "description": "Consulta EcliPsi",
                    "payer": {"email": appointment[2]},
                })
                point = result.get("point_of_interaction")
                details = point.get("transaction_data") if isinstance(point, dict) else None
                provider_id = str(result.get("id", ""))
                if not isinstance(details, dict) or not provider_id.isascii() or not provider_id.isdecimal():
                    raise ProviderError
                qr, qr_base64 = details.get("qr_code"), details.get("qr_code_base64")
                if not isinstance(qr, str) or not qr or not isinstance(qr_base64, str) or not qr_base64:
                    raise ProviderError
            else:
                qr = mock_pix(amount, reference)
            cur.execute(
                """insert into public.pagamentos
                     (pix_id, consulta_id, valor, metodo, status, codigo_pix, pix_qr_base64, pix_mode, pix_provider_id)
                   values (%s, %s, %s, 'pix', 'pendente', %s, %s, %s, %s)
                   returning pix_id, consulta_id, valor, status, codigo_pix, pix_qr_base64, pix_mode""",
                (str(reference), appointment_id, amount, qr, qr_base64, mode, provider_id),
            )
            return payment_response(cur.fetchone(), 201)
    except ProviderError:
        return problem("Mercado Pago indisponível. Tente novamente.", 502)
    finally:
        conn.close()


@bp.post("/pix/<uuid:payment_id>/confirm")
@require_auth
def confirm_pix(payment_id):
    if access_token():
        return problem("A confirmação manual só está disponível em demonstração.", 403)
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            if not has_role(cur, g.auth_user, "paciente"):
                return problem("Acesso restrito a pacientes.", 403)
            cur.execute(
                """select p.status, c.status from public.pagamentos p
                   join public.consultas c on c.id = p.consulta_id
                   where p.pix_id = %s and c.paciente_id = %s and p.pix_mode = 'sandbox_mock'
                   for update of c""",
                (str(payment_id), int(g.auth_user_id)),
            )
            payment = cur.fetchone()
            if not payment:
                return problem("Pagamento não encontrado.", 404)
            if str(payment[1]).lower() == "cancelada" or payment[0] not in ("pendente", "pago"):
                return problem("Pagamento indisponível.", 409)
            cur.execute(
                """update public.pagamentos set status = 'pago', pix_pago_em = coalesce(pix_pago_em, now())
                   where pix_id = %s and pix_mode = 'sandbox_mock'""",
                (str(payment_id),),
            )
            return jsonify(payment_id=str(payment_id), status="pago", mode="sandbox_mock")
    finally:
        conn.close()


@bp.post("/webhook/mercadopago")
def mercadopago_webhook():
    token = access_token()
    if not token:
        return "", 204
    data = payload().get("data")
    payment_id = str(data.get("id", "")) if isinstance(data, dict) else ""
    if not payment_id.isascii() or not payment_id.isdecimal() or len(payment_id) > 30:
        return problem("Notificação inválida.", 400)
    conn = get_db()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """select pix_id, valor, status from public.pagamentos
                   where pix_provider_id = %s and pix_mode = 'mercadopago'""", (payment_id,),
            )
            payment = cur.fetchone()
            if not payment:
                # A notification can arrive before the creation transaction commits.
                return problem("Cobrança ainda não localizada.", 503)
            if payment[2] == "pago":
                return "", 204
            result = provider(token, f"/{payment_id}")
            if result.get("status") != "approved":
                return "", 204
            try:
                amount = Decimal(str(result.get("transaction_amount")))
            except InvalidOperation:
                raise ProviderError from None
            if (str(result.get("id")) != payment_id or result.get("external_reference") != str(payment[0])
                    or result.get("currency_id") != "BRL" or result.get("payment_method_id") != "pix"
                    or not amount.is_finite() or amount != payment[1]):
                return problem("A cobrança não corresponde ao pagamento registrado.", 409)
            cur.execute(
                """update public.pagamentos set status = 'pago', pix_pago_em = now()
                   where pix_id = %s and pix_mode = 'mercadopago' and status = 'pendente'""",
                (str(payment[0]),),
            )
            return "", 204
    except ProviderError:
        return problem("Não foi possível verificar a cobrança.", 502)
    finally:
        conn.close()
