#!/usr/bin/env python3
"""Suite d'ACCEPTATION du réconciliateur — elle décrit un comportement, pas une implémentation.

Ce que `helm lint`/`helm template` ne peuvent pas voir : le CONTRAT avec l'API Metabase. Le faux
Metabase (tests/stub_metabase.py) reproduit les comportements du vrai qui peuvent piéger un
client naïf (fusion de `details`, remise à zéro des champs absents d'un PUT, throttling des
connexions), sans quoi une suite verte ne prouverait que les hypothèses du programme.

Elle a d'abord validé l'implémentation Python 0.1.0, puis a été rejouée telle quelle contre ce
binaire Rust : c'est ce qui prouve la parité de comportement de la réécriture.

Aucune dépendance hors bibliothèque standard :
    cargo build && python3 tests/acceptance.py
"""
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

# Le stub est chargé par importlib : sans ça, Python déposerait un __pycache__ dans tests/.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BIN = os.path.join(os.path.dirname(HERE), "target", "debug", "metabase-datasource-sync")

# Cette suite décrit un COMPORTEMENT, pas une implémentation : `SYNC_CMD` permet de la rejouer
# telle quelle contre une autre implémentation (par exemple le binaire Rust de la 0.2.0), ce qui
# est la seule façon honnête de prouver une parité de comportement lors d'une réécriture.
#   SYNC_CMD=/chemin/vers/metabase-datasource-sync python3 tests/test_sync.py
IMPLEMENTATION = shlex.split(os.environ["SYNC_CMD"]) if os.environ.get("SYNC_CMD") \
    else [DEFAULT_BIN]

PG_PASSWORD_1 = "pg-password-un-jamais-imprime"
PG_PASSWORD_2 = "pg-password-deux-jamais-imprime"
MYSQL_PASSWORD = "mysql-password-jamais-imprime"

failures = []
port_counter = [18780]


def check(label, condition, detail=""):
    print("{} {}{}".format("ok  " if condition else "FAIL", label, "" if condition else " -> " + detail))
    if not condition:
        failures.append(label)


def fresh_stub():
    """Un module de stub par scénario : l'état global reste cloisonné."""
    port_counter[0] += 1
    spec = importlib.util.spec_from_file_location(
        "stub{}".format(port_counter[0]), os.path.join(HERE, "stub_metabase.py"))
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)
    stub.serve(port_counter[0])
    return stub, port_counter[0]


def write_secret(directory, values):
    """Écrit comme le kubelet : le contenu exact, sans newline ajoutée."""
    os.makedirs(directory, exist_ok=True)
    for key, value in values.items():
        with open(os.path.join(directory, key), "w") as handle:
            handle.write(value)


def pg_secret(user, password, host="cluster-rw.svc.cluster.local", port="5432", dbname="appdb"):
    return {"PGHOST": host, "PGPORT": port, "PGDATABASE": dbname,
            "PGUSER": user, "PGPASSWORD": password, "PGSSLMODE": "require"}


PG_KEYS = ('{"host":"PGHOST","port":"PGPORT","dbname":"PGDATABASE","user":"PGUSER",'
           '"password":"PGPASSWORD","ssl-mode":"PGSSLMODE"}')
MYSQL_KEYS = ('{"host":"MYSQL_HOST","port":"MYSQL_PORT","dbname":"MYSQL_DATABASE",'
              '"user":"MYSQL_USER","password":"MYSQL_PASSWORD"}')


class Runner:
    """Lance le réconciliateur avec un environnement donné et le coupe à la sortie du bloc."""

    def __init__(self, stub, port, work, env_extra, admin_password=None):
        self.work = work
        self.heartbeat = os.path.join(work, "alive")
        admin_dir = os.path.join(work, "admin")
        write_secret(admin_dir, {"password": admin_password or stub.ADMIN["password"]})
        env = dict(os.environ)
        env.update({
            "MB_URL": "http://127.0.0.1:{}".format(port),
            "MB_ADMIN_EMAIL": stub.ADMIN["email"],
            "MB_ADMIN_PASSWORD_FILE": os.path.join(admin_dir, "password"),
            "POLL_SECONDS": "1",
            "RECONCILE_SECONDS": "3600",
            "HEARTBEAT_FILE": self.heartbeat,
        })
        env.update(env_extra)
        self.proc = subprocess.Popen(IMPLEMENTATION, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.proc.terminate()
        self.output = self.proc.stdout.read()
        return False


def scenario_nominal():
    print("--- nominal : création, rotation, silence, rattrapage ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-nominal-")
    pg_dir, mysql_dir = os.path.join(work, "ds0"), os.path.join(work, "ds1")
    write_secret(pg_dir, pg_secret("v-reader-aaaa", PG_PASSWORD_1))
    write_secret(mysql_dir, {"MYSQL_HOST": "mysql", "MYSQL_PORT": "3306",
                             "MYSQL_DATABASE": "entrepot", "MYSQL_USER": "bi",
                             "MYSQL_PASSWORD": MYSQL_PASSWORD})

    with Runner(stub, port, work, {
        "DS_COUNT": "2",
        "DS0_NAME": "Base applicative", "DS0_ENGINE": "postgres", "DS0_DIR": pg_dir,
        "DS0_KEYS": PG_KEYS, "DS0_EXTRA": '{"ssl":true,"tunnel-enabled":false}',
        "DS1_NAME": "Entrepôt MySQL", "DS1_ENGINE": "mysql", "DS1_DIR": mysql_dir,
        "DS1_KEYS": MYSQL_KEYS, "DS1_EXTRA": '{"ssl":false}',
    }) as run:
        time.sleep(2.5)
        names = sorted(db["name"] for db in stub.STATE["databases"])
        check("les deux sources sont créées", names == ["Base applicative", "Entrepôt MySQL"], str(names))
        pg = next(db for db in stub.STATE["databases"] if db["name"] == "Base applicative")
        mysql = next(db for db in stub.STATE["databases"] if db["name"] == "Entrepôt MySQL")
        check("moteur par source", (pg["engine"], mysql["engine"]) == ("postgres", "mysql"))
        check("port converti en entier", (pg["details"]["port"], mysql["details"]["port"]) == (5432, 3306))
        check("clés propres à chaque Secret", mysql["details"]["dbname"] == "entrepot")
        check("ssl-mode lu dans le Secret pg", pg["details"].get("ssl-mode") == "require")
        check("aucun ssl-mode imposé à mysql", "ssl-mode" not in mysql["details"])
        check("extraDetails appliqués", pg["details"]["ssl"] is True and mysql["details"]["ssl"] is False)

        time.sleep(2.5)
        check("silence tant que rien ne bouge", len(stub.STATE["writes"]) == 2, str(stub.STATE["writes"]))
        check("une seule connexion réutilisée", stub.STATE["logins"] == 1, str(stub.STATE["logins"]))

        write_secret(pg_dir, pg_secret("v-reader-bbbb", PG_PASSWORD_2))
        time.sleep(3)
        check("rotation poussée par PUT",
              stub.STATE["writes"][-1] == ("PUT", "Base applicative", "v-reader-bbbb"),
              str(stub.STATE["writes"]))
        check("nouveau password transmis", pg["details"]["password"] == PG_PASSWORD_2)
        check("la source non tournée n'est pas réécrite",
              len([w for w in stub.STATE["writes"] if w[1] == "Entrepôt MySQL"]) == 1)

        stub.STATE["databases"][0]["name"] = "Base applicative (anonymisée)"
        write_secret(pg_dir, pg_secret("v-reader-cccc", PG_PASSWORD_1))
        time.sleep(3)
        check("rattrapage sans doublon", len(stub.STATE["databases"]) == 2)
        check("nom affiché préservé", stub.STATE["databases"][0]["name"] == "Base applicative (anonymisée)")

    for label, secret in (("password pg", PG_PASSWORD_1), ("password pg tourné", PG_PASSWORD_2),
                          ("password mysql", MYSQL_PASSWORD), ("password admin", stub.ADMIN["password"])):
        check("aucun {} dans les journaux".format(label), secret not in run.output)
    check("l'utilisateur est journalisé (diagnostic)", "v-reader-cccc" in run.output)
    shutil.rmtree(work, ignore_errors=True)
    return run.output


def scenario_preserved_fields():
    print("--- réglages de l'utilisateur préservés par le PUT ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-preserve-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-reader-aaaa", PG_PASSWORD_1))
    # Une source déjà configurée à la main : synchro à la demande + cache personnalisé.
    stub.add_database("Base applicative", "postgres",
                      {"host": "cluster-rw.svc.cluster.local", "port": 5432, "dbname": "appdb",
                       "user": "v-reader-AVANT", "password": "ancien"},
                      is_on_demand=True, cache_ttl=42)

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "Base applicative", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run:
        time.sleep(3)
        database = stub.STATE["databases"][0]
        check("la connexion est bien mise à jour", database["details"]["user"] == "v-reader-aaaa")
        check("is_on_demand préservé", database["is_on_demand"] is True, str(database.get("is_on_demand")))
        check("cache_ttl préservé", database["cache_ttl"] == 42, str(database.get("cache_ttl")))
        check("aucune source créée en doublon", len(stub.STATE["databases"]) == 1)
    shutil.rmtree(work, ignore_errors=True)


def scenario_reserved_and_ambiguous():
    print("--- sources réservées ignorées, ambiguïté refusée ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-ambig-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-reader-aaaa", PG_PASSWORD_1))
    # Une base d'exemple qui porte par malchance la même adresse : la toucher est refusé par
    # Metabase, et la confondre avec la cible serait un détournement.
    stub.add_database("Sample Database", "postgres",
                      {"host": "cluster-rw.svc.cluster.local", "port": 5432, "dbname": "appdb",
                       "user": "sample"}, is_sample=True)

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "Base applicative", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run:
        time.sleep(3)
        check("la base réservée n'est pas réécrite",
              stub.STATE["databases"][0]["details"]["user"] == "sample")
        check("une source propre est créée à côté", len(stub.STATE["databases"]) == 2)

    stub2, port2 = fresh_stub()
    work2 = tempfile.mkdtemp(prefix="mds-ambig2-")
    directory2 = os.path.join(work2, "ds0")
    write_secret(directory2, pg_secret("v-reader-aaaa", PG_PASSWORD_1))
    for name in ("Copie A", "Copie B"):
        stub2.add_database(name, "postgres",
                           {"host": "cluster-rw.svc.cluster.local", "port": 5432,
                            "dbname": "appdb", "user": "x"})
    with Runner(stub2, port2, work2, {
        "DS_COUNT": "1", "DS0_NAME": "Base applicative", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory2, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run2:
        time.sleep(3)
    check("deux candidates -> refus d'écrire", stub2.STATE["writes"] == [], str(stub2.STATE["writes"]))
    check("refus journalisé explicitement", "refus de choisir" in run2.output, run2.output[-200:])
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(work2, ignore_errors=True)


def scenario_isolation():
    print("--- une source en échec n'empêche pas les autres ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-isolation-")
    broken, healthy = os.path.join(work, "ds0"), os.path.join(work, "ds1")
    write_secret(broken, {"PGHOST": "h"})  # clés manquantes
    write_secret(healthy, pg_secret("v-reader-ok", PG_PASSWORD_1, dbname="saine"))

    with Runner(stub, port, work, {
        "DS_COUNT": "2",
        "DS0_NAME": "Cassée", "DS0_ENGINE": "postgres", "DS0_DIR": broken,
        "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
        "DS1_NAME": "Saine", "DS1_ENGINE": "postgres", "DS1_DIR": healthy,
        "DS1_KEYS": PG_KEYS, "DS1_EXTRA": "{}",
    }) as run:
        time.sleep(3)
        names = [db["name"] for db in stub.STATE["databases"]]
        check("la source saine est bien créée", names == ["Saine"], str(names))
    check("l'échec de l'autre est journalisé", "[Cassée] ECHEC" in run.output, run.output[-200:])
    shutil.rmtree(work, ignore_errors=True)


def scenario_local_drift():
    print("--- dérive locale sans changement d'utilisateur ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-drift-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("utilisateur-stable", PG_PASSWORD_1))

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "Base", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run:
        time.sleep(2.5)
        check("source créée", len(stub.STATE["writes"]) == 1)
        # Password renouvelé EN PLACE : l'utilisateur ne change pas, donc le témoin visible non
        # plus. C'est l'empreinte de l'état voulu qui doit déclencher la réécriture.
        write_secret(directory, pg_secret("utilisateur-stable", PG_PASSWORD_2))
        time.sleep(3)
        check("le password seul déclenche un PUT", len(stub.STATE["writes"]) == 2,
              str(stub.STATE["writes"]))
        check("le nouveau password est bien en place",
              stub.STATE["databases"][0]["details"]["password"] == PG_PASSWORD_2)
    shutil.rmtree(work, ignore_errors=True)


def scenario_periodic():
    print("--- vérification périodique sans aucun changement local ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-periodic-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-reader-aaaa", PG_PASSWORD_1))

    with Runner(stub, port, work, {
        "RECONCILE_SECONDS": "2",
        "DS_COUNT": "1", "DS0_NAME": "Base", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run:
        time.sleep(2)
        first = stub.STATE["lists"]
        time.sleep(4)
        check("l'état distant est re-interrogé même sans dérive", stub.STATE["lists"] > first,
              "{} -> {}".format(first, stub.STATE["lists"]))
        check("mais rien n'est réécrit", len(stub.STATE["writes"]) == 1, str(stub.STATE["writes"]))
    shutil.rmtree(work, ignore_errors=True)


def scenario_failure_path():
    print("--- chemin d'échec : mot de passe admin invalide ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-fail-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-user", PG_PASSWORD_1))

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "X", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }, admin_password="mauvais") as run:
        time.sleep(2)
        first_beat = os.path.getmtime(run.heartbeat)
        time.sleep(4)
        check("process vivant, il retente", run.proc.poll() is None)
        check("heartbeat figé pendant l'échec", os.path.getmtime(run.heartbeat) == first_beat)
        check("rien écrit dans Metabase", stub.STATE["writes"] == [])
        # Sans temporisation croissante, 6 secondes à 1 s d'intervalle feraient ~6 tentatives et
        # entretiendraient le throttling du compte.
        check("les tentatives sont espacées (anti-throttling)", stub.STATE["logins"] <= 4,
              str(stub.STATE["logins"]))
    check("échec journalisé avec le code HTTP", "ECHEC" in run.output and "HTTP 401" in run.output)
    check("aucun password dans les journaux du chemin d'échec", PG_PASSWORD_1 not in run.output)
    check("le mot de passe admin non plus", "mauvais" not in run.output.replace("mauvais identifiants", ""))
    shutil.rmtree(work, ignore_errors=True)


def scenario_liveness():
    """L'image finale est un `scratch` : ce binaire est le seul exécutable disponible, donc c'est
    lui qui porte la sonde de liveness du pod. Le chart en dépend."""
    print("--- sonde de liveness ---")
    work = tempfile.mkdtemp(prefix="mds-liveness-")
    beat = os.path.join(work, "alive")
    open(beat, "w").close()

    def probe():
        env = dict(os.environ)
        env.update({"HEARTBEAT_FILE": beat, "LIVENESS_MAX_AGE_SECONDS": "60"})
        return subprocess.run(IMPLEMENTATION + ["--liveness"], env=env,
                              capture_output=True, text=True)

    fresh = probe()
    check("heartbeat frais -> sonde verte", fresh.returncode == 0, fresh.stderr[:200])

    stale = time.time() - 120
    os.utime(beat, (stale, stale))
    old = probe()
    check("heartbeat rassis -> sonde rouge", old.returncode != 0)
    check("le motif de l'échec est explicite", "heartbeat vieux de" in old.stderr,
          old.stderr[:200])

    os.remove(beat)
    missing = probe()
    check("heartbeat absent -> sonde rouge", missing.returncode != 0)
    check("l'absence est journalisée", "heartbeat illisible" in missing.stderr,
          missing.stderr[:200])
    shutil.rmtree(work, ignore_errors=True)


def scenario_pinned_id():
    """Instance MIGRÉE : la source à adopter porte le nom de l'ancienne instance et vise l'ancien
    hôte, donc ni le nom ni l'adressage ne la retrouvent. Sans épinglage, le programme créerait un
    doublon vide et laisserait les questions sur une source cassée (vu en production)."""
    print("--- id épinglé : adoption d'une source migrée ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-pinned-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-reader-aaaa", PG_PASSWORD_1))
    migrated = stub.add_database("SIRENA (anonymisé)", "postgres",
                                 {"host": "ancien-hote-v1", "port": 5432, "dbname": "sirena",
                                  "user": "avnadmin", "password": "ancien"},
                                 is_on_demand=True, cache_ttl=24)
    decoy = stub.add_database("cible", "postgres",
                              {"host": "autre", "port": 5432, "dbname": "autre", "user": "u"})

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "cible", "DS0_ID": str(migrated["id"]),
        "DS0_ENGINE": "postgres", "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }):
        time.sleep(3)
    check("aucune création malgré nom et adresse inconnus",
          not any(w[0] == "POST" for w in stub.STATE["writes"]), str(stub.STATE["writes"]))
    check("la source épinglée est réécrite",
          migrated["details"].get("host") == "cluster-rw.svc.cluster.local"
          and migrated["details"].get("user") == "v-reader-aaaa", str(migrated["details"]))
    check("son nom n'est pas touché", migrated["name"] == "SIRENA (anonymisé)", migrated["name"])
    check("ses réglages sont conservés",
          migrated.get("is_on_demand") is True and migrated.get("cache_ttl") == 24,
          str({k: migrated.get(k) for k in ("is_on_demand", "cache_ttl")}))
    check("l'homonyme n'est pas adoptée", decoy["details"].get("host") == "autre",
          str(decoy["details"]))
    shutil.rmtree(work, ignore_errors=True)


def scenario_pinned_id_missing():
    print("--- id épinglé absent : erreur, jamais de création ni de repli sur le nom ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-pinned-missing-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-reader-aaaa", PG_PASSWORD_1))
    homonym = stub.add_database("cible", "postgres",
                                {"host": "cluster-rw.svc.cluster.local", "port": 5432,
                                 "dbname": "appdb", "user": "ancien"})

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "cible", "DS0_ID": "99",
        "DS0_ENGINE": "postgres", "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run:
        time.sleep(3)
    check("rien écrit dans Metabase", stub.STATE["writes"] == [], str(stub.STATE["writes"]))
    check("l'homonyme n'est pas adoptée", homonym["details"].get("user") == "ancien")
    check("l'absence est journalisée", "introuvable" in run.output, run.output[-300:])
    shutil.rmtree(work, ignore_errors=True)


def scenario_invalid_id():
    print("--- id épinglé invalide : refus de démarrer ---")
    env = dict(os.environ)
    env.update({"MB_URL": "http://127.0.0.1:1", "MB_ADMIN_EMAIL": "a@b",
                "MB_ADMIN_PASSWORD_FILE": "/nowhere", "DS_COUNT": "1", "DS0_NAME": "x",
                "DS0_DIR": "/nowhere", "DS0_ID": "deux"})
    result = subprocess.run(IMPLEMENTATION, env=env, capture_output=True, text=True, timeout=10)
    check("code de sortie non nul", result.returncode != 0)
    check("le motif est explicite", "DS0_ID" in result.stderr, result.stderr[:200])
    # Une valeur illisible n'est pas une absence : la confondre ferait créer la source épinglée.
    envb = {k.encode(): v.encode() for k, v in env.items()}
    envb[b"DS0_ID"] = b"\xff"
    result = subprocess.run(IMPLEMENTATION, env=envb, capture_output=True, text=True, timeout=10)
    check("DS0_ID non UTF-8 refusé au démarrage", result.returncode != 0 and "DS0_ID" in result.stderr,
          result.stderr[:200])


def scenario_real_throttling():
    """Le throttling RÉEL de Metabase : chaque tentative faite pendant le verrou compte et le
    prolonge, et le délai affiché n'en est pas l'échéance. Un client qui retente sur sa seule
    temporisation garde le compte admin verrouillé indéfiniment (vu en production)."""
    print("--- throttling réel : pas de nouvelle tentative pendant le verrou ---")
    stub, port = fresh_stub()
    stub.STATE["throttle_real"] = True
    work = tempfile.mkdtemp(prefix="mds-throttle-")
    directory = os.path.join(work, "ds0")
    write_secret(directory, pg_secret("v-user", PG_PASSWORD_1))

    with Runner(stub, port, work, {
        "DS_COUNT": "1", "DS0_NAME": "X", "DS0_ENGINE": "postgres",
        "DS0_DIR": directory, "DS0_KEYS": PG_KEYS, "DS0_EXTRA": "{}",
    }) as run:
        time.sleep(6)
        check("process vivant", run.proc.poll() is None)
    check("une seule tentative de connexion pendant le verrou", stub.STATE["logins"] == 1,
          str(stub.STATE["logins"]))
    check("le throttling est journalisé", "Too many attempts" in run.output, run.output[-300:])
    shutil.rmtree(work, ignore_errors=True)


def scenario_throttling_mid_pass():
    """La session expire pendant une ÉCRITURE et la reconnexion est refusée pour throttling : le
    refus concerne le compte, la passe doit s'arrêter. Passer à la source suivante relancerait une
    connexion par source, chacune comptant pour le verrou de Metabase."""
    print("--- throttling en pleine passe : la passe s'arrête ---")
    stub, port = fresh_stub()
    work = tempfile.mkdtemp(prefix="mds-throttle-pass-")
    env = {"DS_COUNT": "3"}
    for i in range(3):
        directory = os.path.join(work, "ds{}".format(i))
        write_secret(directory, pg_secret("v-user-{}".format(i), PG_PASSWORD_1,
                                          dbname="base{}".format(i)))
        stub.add_database("S{}".format(i), "postgres",
                          {"host": "cluster-rw.svc.cluster.local", "port": 5432,
                           "dbname": "base{}".format(i), "user": "ancien"})
        env.update({"DS{}_NAME".format(i): "S{}".format(i), "DS{}_ENGINE".format(i): "postgres",
                    "DS{}_DIR".format(i): directory, "DS{}_KEYS".format(i): PG_KEYS,
                    "DS{}_EXTRA".format(i): "{}"})
    stub.STATE["expire_on_put"] = True
    with Runner(stub, port, work, env) as run:
        time.sleep(4)
    check("une seule reconnexion refusée, pas une par source", stub.STATE["logins"] == 2,
          str(stub.STATE["logins"]))
    check("le refus est journalisé", "Too many attempts" in run.output, run.output[-300:])
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    nominal_output = scenario_nominal()
    scenario_preserved_fields()
    scenario_reserved_and_ambiguous()
    scenario_isolation()
    scenario_local_drift()
    scenario_periodic()
    scenario_failure_path()
    scenario_liveness()
    scenario_pinned_id()
    scenario_pinned_id_missing()
    scenario_invalid_id()
    scenario_real_throttling()
    scenario_throttling_mid_pass()
    print("--- journal du réconciliateur (nominal) ---")
    print(nominal_output.strip())
    print("--- {} échec(s) ---".format(len(failures)))
    sys.exit(1 if failures else 0)
