#!/usr/bin/env python3
"""Acceptation contre un VRAI Metabase et un VRAI PostgreSQL.

La suite `acceptance.py` pilote un faux Metabase : elle vérifie le comportement du programme, pas
le contrat réel de l'API. Or ce contrat bouge — Metabase publie souvent, et les champs qu'un `PUT`
remet à leur défaut ont déjà changé d'une version à l'autre. Ce scénario-ci exerce donc le binaire
contre l'image Metabase réelle et une base réelle :

1. bootstrap de l'instance (compte admin) comme le fait un déploiement neuf ;
2. création de la source par le binaire, puis requête SQL RÉELLE à travers Metabase ;
3. **rotation** : un nouvel utilisateur PostgreSQL, les fichiers du « Secret » réécrits, et on
   vérifie que Metabase interroge toujours la base — avec le nouvel utilisateur.

`MB_ENCRYPTION_SECRET_KEY` est posée côté Metabase, comme en production : c'est ce qui rend le
champ `details` chiffré et interdit toute écriture SQL directe.

Prérequis (fournis par le job CI) : Metabase sur MB_URL, PostgreSQL joignable par `psql` en local
et par Metabase sous le nom `PG_HOST_FOR_METABASE`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

MB = os.environ.get("MB_URL", "http://localhost:3000").rstrip("/")
BIN = os.environ.get("SYNC_CMD", "./target/debug/metabase-datasource-sync")
PG_HOST_FOR_METABASE = os.environ.get("PG_HOST_FOR_METABASE", "postgres")
PG_LOCAL = os.environ.get("PG_LOCAL", "localhost")
PG_LOCAL_PORT = os.environ.get("PG_LOCAL_PORT", "5432")
PG_DB = os.environ.get("PG_DB", "app")
PG_SUPERUSER = os.environ.get("PG_SUPERUSER", "postgres")
PG_SUPERPASS = os.environ.get("PG_SUPERPASS", "postgres")

ADMIN_EMAIL = "ops@exemple.fr"
ADMIN_PASSWORD = "Acceptation-2026-jamais-imprimee"
SOURCE_NAME = "Base applicative"

failures = []


def check(label, condition, detail=""):
    print("{} {}{}".format("ok  " if condition else "FAIL", label, "" if condition else " -> " + detail))
    if not condition:
        failures.append(label)


def call(path, method="GET", body=None, token=None, timeout=60):
    req = urllib.request.Request(MB + path, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    if token:
        req.add_header("X-Metabase-Session", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raise RuntimeError("{} {} -> HTTP {} : {}".format(
            method, path, err.code, err.read().decode("utf-8", "replace")[:400]))
    return json.loads(raw) if raw else None


def wait_ready(deadline_seconds=420):
    """Metabase met une à deux minutes à démarrer, plus longtemps sur un runner partagé."""
    deadline = time.time() + deadline_seconds
    while True:
        try:
            call("/api/health", timeout=10)
            return
        except Exception:
            if time.time() >= deadline:
                raise SystemExit("Metabase injoignable apres {}s".format(deadline_seconds))
            time.sleep(5)


def bootstrap_admin():
    """Metabase v0.63 n'adopte pas un MB_SETUP_TOKEN imposé : il génère le sien, exposé tant que
    le setup n'est pas fait."""
    props = call("/api/session/properties")
    if props.get("has-user-setup"):
        return call("/api/session", "POST",
                    {"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD})["id"]
    token = props.get("setup-token")
    if not token:
        raise SystemExit("setup-token introuvable et instance deja initialisee")
    return call("/api/setup", "POST", {
        "token": token,
        "user": {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                 "first_name": "Equipe", "last_name": "Acceptation"},
        "prefs": {"site_name": "Acceptation", "allow_tracking": False},
    })["id"]


def psql(sql, database=PG_DB):
    env = dict(os.environ, PGPASSWORD=PG_SUPERPASS)
    result = subprocess.run(
        ["psql", "-h", PG_LOCAL, "-p", PG_LOCAL_PORT, "-U", PG_SUPERUSER, "-d", database,
         "-tAc", sql],
        env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("psql a echoue : {}".format(result.stderr.strip()[:400]))
    return result.stdout.strip()


def write_secret(directory, user, password):
    os.makedirs(directory, exist_ok=True)
    values = {"PGHOST": PG_HOST_FOR_METABASE, "PGPORT": "5432", "PGDATABASE": PG_DB,
              "PGUSER": user, "PGPASSWORD": password, "PGSSLMODE": "disable"}
    for key, value in values.items():
        with open(os.path.join(directory, key), "w") as handle:
            handle.write(value)


def stored_user(token):
    listing = call("/api/database", token=token)
    databases = listing["data"] if isinstance(listing, dict) else listing
    ours = [db for db in databases if db.get("name") == SOURCE_NAME]
    if not ours:
        return None
    return (ours[0].get("details") or {}).get("user"), ours[0]["id"], len(ours)


def query_rows(token, database_id):
    res = call("/api/dataset", "POST", {
        "database": database_id, "type": "native",
        "native": {"query": "select count(*) as n from acceptation"},
    }, token=token)
    if res.get("status") != "completed":
        raise RuntimeError("requete refusee : {}".format(str(res.get("error"))[:300]))
    return res["data"]["rows"]


def run_sync(env_extra, seconds, work):
    """Lance le binaire `seconds` secondes avec l'environnement commun, rend son journal."""
    env = dict(os.environ)
    env.update({
        "MB_URL": MB,
        "MB_ADMIN_EMAIL": ADMIN_EMAIL,
        "MB_ADMIN_PASSWORD_FILE": os.path.join(work, "admin", "password"),
        "POLL_SECONDS": "2",
        "RECONCILE_SECONDS": "3600",
        "HEARTBEAT_FILE": os.path.join(work, "alive"),
    })
    env.update(env_extra)
    proc = subprocess.Popen(BIN.split(), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(seconds)
    finally:
        proc.terminate()
    return proc.stdout.read()


def scenario_pinned(token, work):
    """Instance MIGRÉE : une source porte le nom de l'ancienne instance et une autre adresse (ici
    une autre base du même serveur). Ni le nom ni l'adressage ne la retrouvent ; seul `DS0_ID`
    peut la désigner. Elle doit être réécrite — pas doublée d'une source neuve et vide."""
    print("--- instance migree : adoption par identifiant epingle ---")
    migrated = call("/api/database", "POST", {
        "name": "SIRENA (anonymise, ancienne instance)", "engine": "postgres",
        "details": {"host": PG_HOST_FOR_METABASE, "port": 5432, "dbname": "postgres",
                    "user": PG_SUPERUSER, "password": PG_SUPERPASS, "ssl": False,
                    "ssl-mode": "disable", "tunnel-enabled": False},
    }, token=token)
    secret_dir = os.path.join(work, "pinned")
    write_secret(secret_dir, "lecteur_deux", "un-autre-mot-de-passe")
    output = run_sync({
        "DS_COUNT": "1", "DS0_NAME": "nom-de-la-configuration", "DS0_ID": str(migrated["id"]),
        "DS0_ENGINE": "postgres", "DS0_DIR": secret_dir,
        "DS0_KEYS": '{"host":"PGHOST","port":"PGPORT","dbname":"PGDATABASE","user":"PGUSER",'
                    '"password":"PGPASSWORD","ssl-mode":"PGSSLMODE"}',
        "DS0_EXTRA": '{"ssl":false,"tunnel-enabled":false}',
    }, 12, work)
    listing = call("/api/database", token=token)
    databases = listing["data"] if isinstance(listing, dict) else listing
    check("aucune source creee sous le nom de la configuration",
          not any(db.get("name") == "nom-de-la-configuration" for db in databases))
    after = next((db for db in databases if db["id"] == migrated["id"]), None)
    details = (after or {}).get("details") or {}
    check("la source migree est reecrite sur la nouvelle base",
          details.get("dbname") == PG_DB and details.get("user") == "lecteur_deux", str(details))
    check("son nom n'est pas touche",
          (after or {}).get("name") == "SIRENA (anonymise, ancienne instance)")
    rows = query_rows(token, migrated["id"])
    check("la source migree est interrogeable apres adoption", rows == [[7]], str(rows))
    check("aucun mot de passe dans les journaux (epinglage)",
          "un-autre-mot-de-passe" not in output)
    print(output.strip()[-600:])


def scenario_throttling(work):
    """Le throttling RÉEL de `/api/session` : 10 échecs dans l'heure verrouillent le compte, et
    chaque refus compte. On verrouille un compte JETABLE (le verrou est par nom d'utilisateur,
    qu'il existe ou non), pour ne pas priver la suite de son admin."""
    print("--- throttling reel : une seule tentative pendant le verrou ---")
    victim = "verrouille@exemple.fr"
    for _ in range(11):
        try:
            call("/api/session", "POST", {"username": victim, "password": "faux"})
        except RuntimeError:
            pass
    output = run_sync({"MB_ADMIN_EMAIL": victim, "DS_COUNT": "1", "DS0_NAME": "x",
                       "DS0_DIR": os.path.join(work, "ds0"), "DS0_KEYS": "{}"}, 15, work)
    attempts = output.count("POST /api/session")
    check("le verrou est bien celui de Metabase", "Too many attempts" in output, output[-300:])
    check("une seule tentative en 15 s malgre POLL_SECONDS=2", attempts == 1, str(attempts))


def main():
    print("--- attente de Metabase ---")
    wait_ready()
    token = bootstrap_admin()
    print("admin provisionne, session obtenue")

    # Une table réelle à interroger, et un premier rôle de lecture.
    psql("create table if not exists acceptation(id int); "
         "insert into acceptation select generate_series(1,7) "
         "on conflict do nothing;")
    psql("drop role if exists lecteur_un; create role lecteur_un login password 'un-mot-de-passe';")
    psql("grant connect on database {} to lecteur_un; grant usage on schema public to lecteur_un; "
         "grant select on all tables in schema public to lecteur_un;".format(PG_DB))

    work = tempfile.mkdtemp(prefix="mds-reel-")
    secret_dir = os.path.join(work, "ds0")
    admin_dir = os.path.join(work, "admin")
    os.makedirs(admin_dir)
    with open(os.path.join(admin_dir, "password"), "w") as handle:
        handle.write(ADMIN_PASSWORD)
    write_secret(secret_dir, "lecteur_un", "un-mot-de-passe")

    env = dict(os.environ)
    env.update({
        "MB_URL": MB,
        "MB_ADMIN_EMAIL": ADMIN_EMAIL,
        "MB_ADMIN_PASSWORD_FILE": os.path.join(admin_dir, "password"),
        "POLL_SECONDS": "2",
        "RECONCILE_SECONDS": "3600",
        "HEARTBEAT_FILE": os.path.join(work, "alive"),
        "DS_COUNT": "1",
        "DS0_NAME": SOURCE_NAME,
        "DS0_ENGINE": "postgres",
        "DS0_DIR": secret_dir,
        "DS0_KEYS": '{"host":"PGHOST","port":"PGPORT","dbname":"PGDATABASE","user":"PGUSER",'
                    '"password":"PGPASSWORD","ssl-mode":"PGSSLMODE"}',
        "DS0_EXTRA": '{"ssl":false,"tunnel-enabled":false}',
    })
    proc = subprocess.Popen(BIN.split(), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        print("--- creation de la source ---")
        created = None
        for _ in range(30):
            time.sleep(2)
            created = stored_user(token)
            if created:
                break
        check("la source est creee dans un vrai Metabase", created is not None)
        if created:
            user, database_id, count = created
            check("un seul exemplaire", count == 1, str(count))
            check("l'utilisateur pousse est le bon", user == "lecteur_un", str(user))
            rows = query_rows(token, database_id)
            check("requete SQL reelle a travers Metabase", rows == [[7]], str(rows))

        print("--- rotation ---")
        psql("drop role if exists lecteur_deux; "
             "create role lecteur_deux login password 'un-autre-mot-de-passe';")
        psql("grant connect on database {} to lecteur_deux; "
             "grant usage on schema public to lecteur_deux; "
             "grant select on all tables in schema public to lecteur_deux;".format(PG_DB))
        write_secret(secret_dir, "lecteur_deux", "un-autre-mot-de-passe")

        rotated = None
        for _ in range(30):
            time.sleep(2)
            rotated = stored_user(token)
            if rotated and rotated[0] == "lecteur_deux":
                break
        check("la rotation est poussee dans Metabase",
              rotated is not None and rotated[0] == "lecteur_deux",
              str(rotated[0]) if rotated else "source disparue")
        if rotated:
            check("toujours un seul exemplaire apres rotation", rotated[2] == 1, str(rotated[2]))
            rows = query_rows(token, rotated[1])
            check("la base reste interrogeable apres rotation", rows == [[7]], str(rows))
    finally:
        proc.terminate()
        output = proc.stdout.read()

    check("aucun mot de passe dans les journaux",
          "un-mot-de-passe" not in output and ADMIN_PASSWORD not in output)
    print("--- journal du reconciliateur ---")
    print(output.strip()[-1500:])

    scenario_pinned(token, work)
    scenario_throttling(work)
    shutil.rmtree(work, ignore_errors=True)
    print("--- {} echec(s) ---".format(len(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
