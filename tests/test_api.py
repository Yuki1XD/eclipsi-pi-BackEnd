import os
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('SUPABASE_JWT_SECRET', 'test-secret')
    monkeypatch.setenv('JWT_SECRET', 'test-secret-with-at-least-32-characters')

    class FakeCursor:
        def execute(self, query, params=None):
            self.query = query

        def fetchone(self):
            if 'from public.users' in self.query:
                return (1, 'Teste', 'test@example.com', None, 'paciente', None, None)
            return None

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    from api.core import database
    monkeypatch.setattr(database, 'get_db', lambda: FakeConnection())

    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client


def token():
    now = datetime.now(timezone.utc)
    secret = 'test-secret-with-at-least-32-characters'
    return jwt.encode({'sub': '1', 'email': 'test@example.com', 'iat': now, 'exp': now + timedelta(minutes=10)}, secret, algorithm='HS256')


def test_health(client):
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json['success'] is True


def test_protected_endpoint_without_token(client):
    response = client.get('/api/me')
    assert response.status_code == 401


def test_protected_endpoint_with_valid_token(client):
    response = client.get('/api/me', headers={'Authorization': f'Bearer {token()}'})
    assert response.status_code == 200
    assert response.json['user']['email'] == 'test@example.com'

def test_openapi_contract(client):
    response = client.get('/api/v1/openapi.json')
    assert response.status_code == 200
    assert '/api/v1/auth/login' in response.json['paths']
    assert '/api/v1/appointments/{appointment_id}' in response.json['paths']


@pytest.mark.parametrize('path', ['/api/v1/doctors', '/api/v1/appointments', '/api/v1/chat/conversations', '/api/v1/quiz/questions', '/api/v1/payments'])
def test_domain_routes_require_authentication(client, path):
    assert client.get(path).status_code == 401
