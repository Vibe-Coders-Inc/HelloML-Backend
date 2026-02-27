"""
General API smoke tests — health check, agent CRUD, business CRUD, auth.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestHealthCheck:

    def test_root_returns_running(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    def test_version_endpoint(self, client):
        resp = client.get("/version")
        assert resp.status_code == 200
        assert "version" in resp.json()


class TestAuthFlow:

    def test_missing_auth_returns_403(self, client):
        """Endpoints requiring auth should return 401/403 without token."""
        resp = client.get("/agent/1")
        assert resp.status_code in (401, 403)

    def test_invalid_token_returns_401(self, client):
        with patch("api.auth.get_service_client") as mock_svc:
            mock_db = MagicMock()
            mock_db.auth.get_user.side_effect = Exception("invalid token")
            mock_svc.return_value = mock_db

            resp = client.get("/agent/1", headers={"Authorization": "Bearer bad-token"})
            assert resp.status_code == 401


class TestBusinessCRUD:

    def test_create_business(self, client, mock_auth):
        user, db = mock_auth
        db.set_table_data("business", [])

        # Mock the insert to return data
        qb = MagicMock()
        qb.insert.return_value = qb
        resp_mock = MagicMock()
        resp_mock.data = [{"id": 1, "name": "Test Biz", "address": "123 Main", "owner_user_id": user.id}]
        qb.execute.return_value = resp_mock
        db.table = MagicMock(return_value=qb)

        resp = client.post("/business", json={
            "name": "Test Biz",
            "address": "123 Main St",
        }, headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Biz"

    def test_get_business(self, client, mock_auth):
        user, db = mock_auth
        qb = MagicMock()
        qb.select.return_value = qb; qb.eq.return_value = qb; qb.single.return_value = qb
        r = MagicMock(); r.data = {"id": 1, "name": "Biz", "address": "A", "owner_user_id": user.id}
        qb.execute.return_value = r
        db.table = MagicMock(return_value=qb)

        resp = client.get("/business/1", headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Biz"

    def test_list_businesses(self, client, mock_auth):
        user, db = mock_auth
        qb = MagicMock()
        qb.select.return_value = qb
        r = MagicMock(); r.data = [{"id": 1, "name": "Biz"}]
        qb.execute.return_value = r
        db.table = MagicMock(return_value=qb)

        resp = client.get("/business", headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_update_business(self, client, mock_auth):
        user, db = mock_auth
        qb = MagicMock()
        qb.update.return_value = qb; qb.eq.return_value = qb
        r = MagicMock(); r.data = [{"id": 1, "name": "Updated"}]
        qb.execute.return_value = r
        db.table = MagicMock(return_value=qb)

        resp = client.put("/business/1", json={"name": "Updated"},
                          headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 200

    def test_update_business_no_fields(self, client, mock_auth):
        resp = client.put("/business/1", json={},
                          headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 400


class TestAgentCRUD:

    def test_create_agent(self, client, mock_auth):
        user, db = mock_auth

        call_n = {"n": 0}
        def _table(name):
            call_n["n"] += 1
            qb = MagicMock()
            qb.select.return_value = qb; qb.eq.return_value = qb
            qb.single.return_value = qb; qb.insert.return_value = qb

            if name == "business":
                r = MagicMock(); r.data = {"id": 10, "owner_user_id": user.id}
                qb.execute.return_value = r
            elif name == "agent" and call_n["n"] <= 2:
                # existing agent check
                r = MagicMock(); r.data = []
                qb.execute.return_value = r
            elif name == "agent":
                r = MagicMock(); r.data = [{"id": 42, "name": "Agent", "business_id": 10}]
                qb.execute.return_value = r
            else:
                r = MagicMock(); r.data = []
                qb.execute.return_value = r
            return qb

        db.table = _table

        with patch("api.crud.agent.provision_phone_for_agent", return_value={"phone_number": "+18005551212"}):
            resp = client.post("/agent", json={
                "business_id": 10,
                "area_code": "800",
                "name": "Agent",
            }, headers={"Authorization": "Bearer fake-jwt-token"})
            assert resp.status_code == 200

    def test_get_agent(self, client, mock_auth):
        user, db = mock_auth

        call_n = {"n": 0}
        def _table(name):
            call_n["n"] += 1
            qb = MagicMock()
            qb.select.return_value = qb; qb.eq.return_value = qb
            qb.single.return_value = qb
            if name == "agent":
                r = MagicMock(); r.data = {"id": 42, "name": "Agent"}
                qb.execute.return_value = r
            elif name == "phone_number":
                r = MagicMock(); r.data = [{"phone_number": "+18005551212"}]
                qb.execute.return_value = r
            else:
                r = MagicMock(); r.data = []
                qb.execute.return_value = r
            return qb

        db.table = _table

        resp = client.get("/agent/42", headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Agent"

    def test_update_agent(self, client, mock_auth):
        user, db = mock_auth
        qb = MagicMock()
        qb.update.return_value = qb; qb.eq.return_value = qb
        r = MagicMock(); r.data = [{"id": 42, "name": "Updated", "updated_at": "now()"}]
        qb.execute.return_value = r
        db.table = MagicMock(return_value=qb)

        resp = client.put("/agent/42", json={"name": "Updated"},
                          headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 200

    def test_update_agent_no_fields(self, client, mock_auth):
        resp = client.put("/agent/42", json={},
                          headers={"Authorization": "Bearer fake-jwt-token"})
        assert resp.status_code == 400
