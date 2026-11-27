from binascii import crc_hqx
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from api.v1.endpoints import patient, pix, scheduling
from api.v1.endpoints.auth import _issue_token
from app import create_app


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20261124_full_mvp.sql"


def test_mock_pix_has_valid_crc_and_no_real_destination():
    code = pix.mock_pix(Decimal("145.50"), uuid4())
    assert "5406145.50" in code
    assert "pix-demo.invalid/" in code
    assert code[-4:] == f"{crc_hqx(code[:-4].encode(), 0xFFFF):04X}"


def test_score_covers_all_criteria():
    care = ("Ansiedade", "tcc", "online", Decimal("150"), "alta")
    doctor = (1, "Demo", "Psicologia — Ansiedade", "002", "PE", "Bio", Decimal("145"),
              None, "TCC", "híbrida", True)
    score, reasons = scheduling.compatibility(care, doctor)
    assert score == 100
    assert "Dentro do orçamento" in reasons


def test_cors_allowlist():
    client = create_app().test_client()
    for origin in ("https://eclipsi.dev", "https://www.eclipsi.dev", "http://localhost:8081"):
        response = client.get("/api/me", headers={"Origin": origin})
        assert response.headers["Access-Control-Allow-Origin"] == origin
        assert response.headers["X-Frame-Options"] == "DENY"
    assert "Access-Control-Allow-Origin" not in client.get(
        "/api/me", headers={"Origin": "https://other.example"}
    ).headers


@pytest.fixture
def postgres(monkeypatch):
    dsn = os.getenv("ECLIPSI_DISPOSABLE_TEST_DSN")
    if not dsn:
        pytest.skip("ECLIPSI_DISPOSABLE_TEST_DSN deve apontar para um banco PostgreSQL vazio.")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.users')")
        if cur.fetchone()[0]:
            pytest.fail("O banco de testes deve estar vazio.")
        cur.execute("""
            do $$ begin
                if not exists (select from pg_roles where rolname = 'anon') then create role anon; end if;
                if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated; end if;
            end $$;

            create table users (
                id bigint generated always as identity primary key, nome text not null,
                email text not null unique, senha text not null, telefone text, role text not null,
                registro text, uf text, valor_hora numeric(10,2), descricao text, foto text
            );
            create table consultas (
                id bigint generated always as identity primary key, paciente_id bigint references users,
                medico_id bigint references users, data_hora timestamptz not null,
                valor numeric(10,2) not null, status text default 'agendada'
            );

            insert into users(nome, email, senha, role, valor_hora) values
                ('Paciente', 'patient@example.com', '!', 'paciente', null),
                ('Outro Paciente', 'other@example.com', '!', 'paciente', null),
                ('Profissional', 'doctor@example.com', '!', 'medico', 145.50);
            set eclipsi.seed_demo = 'on';
        """)
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute("select count(*) from users where email like '%@eclipsi.example'")
        assert cur.fetchone()[0] == 6
        cur.execute("select count(*) from disponibilidades")
        assert cur.fetchone()[0] == 84
    monkeypatch.setenv("JWT_SECRET", "test-not-a-credential-secret-of-32-characters")
    monkeypatch.delenv("MERCADO_PAGO_ACCESS_TOKEN", raising=False)
    for module in (patient, scheduling, pix):
        monkeypatch.setattr(module, "get_db", lambda: psycopg2.connect(dsn))
    try:
        yield create_app().test_client()
    finally:
        with conn.cursor() as cur:
            cur.execute("drop table pagamentos, triagens, disponibilidades, consultas, users")
        conn.close()


def auth(user_id, role):
    return {"Authorization": f"Bearer {_issue_token(user_id, 'test@example.com', role)}"}


def test_scheduling_triage_and_payments(postgres, monkeypatch):
    client = postgres
    professional = auth(3, "medico")
    patient_headers = auth(1, "paciente")
    stranger = auth(2, "paciente")
    when = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    data = {"data_hora": when}
    assert client.post("/api/v1/professional/availability", headers=auth(1, "medico"), json=data).status_code == 403
    slot = client.post("/api/v1/professional/availability", headers=professional, json=data)
    assert slot.status_code == 201
    slot_id = slot.json["slot"]["id"]
    path = "/api/v1/professionals/3/availability"
    assert len(client.get(path, headers=patient_headers).json["slots"]) == 1
    book = "/api/v1/patient/appointments"
    assert client.post(book, headers=patient_headers, json={"slot_id": str(uuid4())}).status_code == 409
    def attempt_booking(_):
        with create_app().test_client() as contender:
            return contender.post(book, headers=patient_headers, json={"slot_id": slot_id})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt_booking, range(2)))
    assert sorted(response.status_code for response in results) == [201, 409]
    visit = next(response for response in results if response.status_code == 201)
    assert client.post(book, headers=stranger, json={"slot_id": slot_id}).status_code == 409
    assert not client.get(path, headers=patient_headers).json["slots"]

    triage = {"motivo_principal": "Ansiedade", "abordagem_preferida": "TCC", "modalidade": "online",
              "urgencia": "alta", "faixa_valor_max": 160}
    assert client.post("/api/v1/patient/triage", headers=professional, json=triage).status_code == 403
    assert client.post("/api/v1/patient/triage", headers=patient_headers, json=triage).status_code == 200
    doctors = client.get("/api/v1/patient/doctors/recommended", headers=patient_headers).json["doctors"]
    assert doctors[0]["compatibility_score"] > doctors[-1]["compatibility_score"]
    assert client.get("/api/v1/doctors", headers=patient_headers).status_code == 200

    payment_path = "/api/v1/payments/pix/create"
    body = {"consulta_id": visit.json["appointment"]["id"]}
    assert client.post(payment_path, headers=stranger, json=body).status_code == 404
    demo_payment = client.post(payment_path, headers=patient_headers, json=body).json
    assert demo_payment["mode"] == "sandbox_mock"
    assert demo_payment["valor"] == 145.50
    assert client.post(payment_path, headers=patient_headers, json=body).json == demo_payment
    confirm = f"/api/v1/payments/pix/{demo_payment['payment_id']}/confirm"
    assert client.post(confirm, headers=stranger).status_code == 404
    assert client.post(confirm, headers=patient_headers).json["status"] == "pago"

    monkeypatch.setenv("MERCADO_PAGO_ACCESS_TOKEN", "test-provider")
    assert client.post(confirm, headers=patient_headers).status_code == 403
    reference = None
    verified_amount = 100

    def fake_provider(token, path="", *, data=None, key=None):
        nonlocal reference
        assert token == "test-provider"
        if data:
            reference = data["external_reference"]
            assert str(key) == reference
            assert data["transaction_amount"] == 145.50
            return {"id": 123, "point_of_interaction": {"transaction_data": {"qr_code": "real", "qr_code_base64": "encoded"}}}
        assert path == "/123"
        return {"id": 123, "external_reference": reference, "status": "approved",
                "transaction_amount": verified_amount, "currency_id": "BRL", "payment_method_id": "pix"}

    monkeypatch.setattr(pix, "provider", fake_provider)
    real = client.post(payment_path, headers=patient_headers, json=body).json
    assert real["mode"] == "mercadopago" and real["status"] == "pendente"
    webhook = "/api/v1/payments/webhook/mercadopago"
    notice = {"data": {"id": 123}}
    assert client.post(webhook, json=notice).status_code == 409
    assert client.post(payment_path, headers=patient_headers, json=body).json["status"] == "pendente"
    verified_amount = 145.50
    assert client.post(webhook, json=notice).status_code == 204
    assert client.post(webhook, json=notice).status_code == 204
    assert client.post(payment_path, headers=patient_headers, json=body).json["status"] == "pago"
