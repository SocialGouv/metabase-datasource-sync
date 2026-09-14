#!/usr/bin/env python3
"""Faux Metabase : implémente ce que le réconciliateur appelle, en reproduisant les comportements
du vrai qui peuvent le piéger.

Un stub plus gentil que la réalité rend la suite verte pour rien. Sont donc reproduits :

- `GET /api/database` ne renvoie JAMAIS le password ;
- `PUT /api/database/:id` **fusionne** `details` (v0.63.15, warehouses_rest/api.clj) ;
- un champ ABSENT du corps d'un PUT est remis à son défaut : `is_on_demand` -> false,
  `cache_ttl` -> nil. C'est ainsi qu'un client naïf écrase des réglages qu'il ne possède pas ;
- toute écriture exige une session valide, et `/api/session` applique un throttling par compte.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"databases": [], "next_id": 1, "writes": [], "logins": 0, "throttled": False, "lists": 0}
ADMIN = {"email": "ops@exemple.fr", "password": "mot-de-passe-admin-jamais-imprime"}
TOKEN = "session-de-test"

# Défauts appliqués par Metabase quand le champ est absent du corps d'un PUT.
RESET_ON_PUT = {"is_on_demand": False, "cache_ttl": None}


def add_database(name, engine, details, **extra):
    """Pose une source préexistante (utilisé par les tests pour préparer un état)."""
    database = {"id": STATE["next_id"], "name": name, "engine": engine, "details": details}
    database.update(extra)
    STATE["next_id"] += 1
    STATE["databases"].append(database)
    return database


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def _authed(self):
        return self.headers.get("X-Metabase-Session") == TOKEN

    def do_GET(self):
        if self.path == "/api/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/api/database":
            if not self._authed():
                return self._send(401, {"message": "unauthenticated"})
            STATE["lists"] += 1
            masked = []
            for database in STATE["databases"]:
                copy = dict(database)
                copy["details"] = {k: v for k, v in database["details"].items() if k != "password"}
                masked.append(copy)
            return self._send(200, {"data": masked, "total": len(masked)})
        return self._send(404, {"message": "not found"})

    def do_POST(self):
        if self.path == "/api/session":
            STATE["logins"] += 1
            body = self._body()
            if STATE["throttled"]:
                return self._send(429, {"message": "Too many attempts"})
            if (body.get("username"), body.get("password")) != (ADMIN["email"], ADMIN["password"]):
                # Comme le vrai : les tentatives ratées finissent par bloquer le compte.
                if STATE["logins"] >= 3:
                    STATE["throttled"] = True
                return self._send(401, {"message": "mauvais identifiants"})
            return self._send(200, {"id": TOKEN})
        if self.path == "/api/database":
            if not self._authed():
                return self._send(401, {"message": "unauthenticated"})
            body = self._body()
            database = add_database(body["name"], body["engine"], body["details"],
                                    is_on_demand=False, cache_ttl=None)
            STATE["writes"].append(("POST", database["name"], body["details"].get("user")))
            return self._send(200, database)
        return self._send(404, {"message": "not found"})

    def do_PUT(self):
        if self.path.startswith("/api/database/"):
            if not self._authed():
                return self._send(401, {"message": "unauthenticated"})
            body = self._body()
            database_id = int(self.path.rsplit("/", 1)[1])
            for database in STATE["databases"]:
                if database["id"] != database_id:
                    continue
                if any(database.get(flag) for flag in ("is_sample", "is_audit")):
                    return self._send(400, {"message": "Cannot modify a reserved database"})
                database["name"] = body.get("name", database["name"])
                database["details"].update(body["details"])
                for field, reset in RESET_ON_PUT.items():
                    database[field] = body.get(field, reset)
                STATE["writes"].append(("PUT", database["name"], body["details"].get("user")))
                return self._send(200, database)
            return self._send(404, {"message": "no such database"})
        return self._send(404, {"message": "not found"})


def serve(port):
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
