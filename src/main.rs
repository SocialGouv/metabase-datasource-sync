//! Aligne les sources de données d'une instance Metabase sur des creds montés dans ce pod.
//!
//! Pourquoi ce programme existe : Metabase stocke la connexion d'une source de données dans SA
//! PROPRE base, champ `metabase_database.details`, chiffré dès que `MB_ENCRYPTION_SECRET_KEY` est
//! posée. Écrire ce champ en SQL depuis l'extérieur imposerait de reproduire son format chiffré
//! (AES-CBC-HMAC-SHA512, clé dérivée PBKDF2) — un couplage à un format interne non contractuel
//! dont la rupture ne se voit pas : la source devient simplement illisible. On passe donc par
//! l'API HTTP, Metabase reste seul à écrire son propre schéma.
//!
//! Le déclencheur : des creds à rotation (Vault dynamique, utilisateur éphémère) livrés dans un
//! Secret Kubernetes MONTÉ EN VOLUME — la seule forme qui voie la rotation, un `secretKeyRef` en
//! `env` étant figé au démarrage du pod.
//!
//! La boucle est level-triggered : elle relit l'état et le corrige, plutôt que de réagir à un
//! front qui, s'il est manqué, laisserait une dérive silencieuse jusqu'au prochain redémarrage.

mod datasource;
mod metabase;
mod redact;
mod secrets;

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use datasource::DataSource;
use metabase::Metabase;

fn main() -> ExitCode {
    match std::env::args().nth(1).as_deref() {
        // L'image finale est un `scratch` : ce binaire est le SEUL exécutable disponible, donc
        // c'est lui qui doit fournir la sonde de liveness du pod.
        Some("--liveness") => return liveness(),
        Some("--version") => {
            println!("{}", env!("CARGO_PKG_VERSION"));
            return ExitCode::SUCCESS;
        }
        Some(other) => {
            log_err(&format!(
                "argument inconnu : {other} (attendu : aucun, --liveness ou --version)"
            ));
            return ExitCode::FAILURE;
        }
        None => {}
    }

    let config = match Config::from_env() {
        Ok(config) => config,
        Err(err) => {
            log_err(&format!("CONFIGURATION INVALIDE : {err}"));
            return ExitCode::FAILURE;
        }
    };
    run(config);
    ExitCode::SUCCESS
}

/// Sonde de liveness : le heartbeat a-t-il été rafraîchi récemment ?
///
/// Un échec transitoire est rattrapé par la boucle, qui continue de rafraîchir le heartbeat ; un
/// échec DURABLE le laisse vieillir, et c'est cette sonde qui le transforme en redémarrage visible.
fn liveness() -> ExitCode {
    let path = PathBuf::from(env_or("HEARTBEAT_FILE", "/tmp/alive"));
    let max_age = match secs("LIVENESS_MAX_AGE_SECONDS", 900.0) {
        Ok(duration) => duration,
        Err(err) => {
            log_err(&format!("CONFIGURATION INVALIDE : {err}"));
            return ExitCode::FAILURE;
        }
    };
    match heartbeat_age(&path) {
        Ok(age) if age <= max_age => ExitCode::SUCCESS,
        Ok(age) => {
            log_err(&format!(
                "heartbeat vieux de {}s (seuil {}s) — aucune réconciliation aboutie depuis",
                age.as_secs(),
                max_age.as_secs()
            ));
            ExitCode::FAILURE
        }
        Err(err) => {
            log_err(&format!("heartbeat illisible : {err}"));
            ExitCode::FAILURE
        }
    }
}

fn heartbeat_age(path: &PathBuf) -> Result<Duration, String> {
    let modified = fs::metadata(path)
        .and_then(|meta| meta.modified())
        .map_err(|e| format!("{} : {}", path.display(), e))?;
    // Une horloge qui recule ne doit pas faire passer la sonde pour fraîche par accident : on
    // traite l'écart négatif comme un âge nul, ce qui est le cas favorable et reste honnête.
    Ok(SystemTime::now()
        .duration_since(modified)
        .unwrap_or(Duration::ZERO))
}

struct Config {
    url: String,
    email: String,
    password_file: PathBuf,
    poll: Duration,
    reconcile: Duration,
    http_timeout: Duration,
    heartbeat: PathBuf,
    create_if_missing: bool,
    sources: Vec<DataSource>,
}

impl Config {
    fn from_env() -> Result<Self, String> {
        let count: usize = env_or("DS_COUNT", "0")
            .parse()
            .map_err(|_| "DS_COUNT n'est pas un entier".to_string())?;
        let mut sources = Vec::with_capacity(count);
        for idx in 0..count {
            sources.push(DataSource {
                name: required(&format!("DS{idx}_NAME"))?,
                engine: env_or(&format!("DS{idx}_ENGINE"), "postgres"),
                dir: PathBuf::from(required(&format!("DS{idx}_DIR"))?),
                keys: parse_map(&env_or(&format!("DS{idx}_KEYS"), "{}"), idx, "KEYS")?,
                extra: parse_object(&env_or(&format!("DS{idx}_EXTRA"), "{}"), idx, "EXTRA")?,
            });
        }
        Ok(Config {
            url: required("MB_URL")?,
            email: required("MB_ADMIN_EMAIL")?,
            password_file: PathBuf::from(required("MB_ADMIN_PASSWORD_FILE")?),
            poll: secs("POLL_SECONDS", 30.0)?,
            reconcile: secs("RECONCILE_SECONDS", 600.0)?,
            http_timeout: secs("HTTP_TIMEOUT", 30.0)?,
            heartbeat: PathBuf::from(env_or("HEARTBEAT_FILE", "/tmp/alive")),
            create_if_missing: env_or("CREATE_IF_MISSING", "true").to_lowercase() == "true",
            sources,
        })
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn required(key: &str) -> Result<String, String> {
    std::env::var(key).map_err(|_| format!("variable {key} manquante"))
}

fn secs(key: &str, default: f64) -> Result<Duration, String> {
    let raw = env_or(key, &default.to_string());
    let value: f64 = raw
        .parse()
        .map_err(|_| format!("{key} = {raw:?} n'est pas un nombre de secondes"))?;
    if value <= 0.0 {
        return Err(format!(
            "{key} doit être strictement positif (reçu {value})"
        ));
    }
    Ok(Duration::from_secs_f64(value))
}

fn parse_map(raw: &str, idx: usize, what: &str) -> Result<BTreeMap<String, String>, String> {
    serde_json::from_str(raw).map_err(|e| format!("DS{idx}_{what} n'est pas un objet JSON : {e}"))
}

fn parse_object(raw: &str, idx: usize, what: &str) -> Result<Map<String, Value>, String> {
    serde_json::from_str(raw).map_err(|e| format!("DS{idx}_{what} n'est pas un objet JSON : {e}"))
}

/// Horodatage `HH:MM:SS` en UTC, calculé sans dépendance : un conteneur `scratch` n'embarque pas
/// de base de fuseaux, une heure « locale » y serait de toute façon UTC.
fn stamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!(
        "{:02}:{:02}:{:02}",
        (secs / 3600) % 24,
        (secs / 60) % 60,
        secs % 60
    )
}

fn log(message: &str) {
    println!("{} {}", stamp(), redact::redact(message));
}

fn log_err(message: &str) {
    eprintln!("{} {}", stamp(), redact::redact(message));
}

fn touch(path: &PathBuf) {
    if let Err(err) = fs::write(path, b"") {
        log_err(&format!("heartbeat {} non écrit : {}", path.display(), err));
    }
}

fn run(config: Config) {
    if config.sources.is_empty() {
        log("Aucune source déclarée — rien à réconcilier.");
    } else {
        log(&format!(
            "Réconciliation de {} source(s) : relecture {}s, vérification complète {}s",
            config.sources.len(),
            config.poll.as_secs(),
            config.reconcile.as_secs()
        ));
    }

    // Heartbeat posé AVANT la première tentative : Metabase met une à deux minutes à démarrer, et
    // le compte admin est créé par un hook post-déploiement. La liveness doit mesurer un échec
    // DURABLE (Metabase injoignable pendant tout son délai), pas une indisponibilité de démarrage.
    touch(&config.heartbeat);

    let mut session = Metabase::new(
        &config.url,
        &config.email,
        &config.password_file,
        config.http_timeout,
    );
    let mut pushed: HashMap<usize, String> = HashMap::new();
    let mut last_full: Option<Instant> = None;

    loop {
        let mut delay = config.poll;

        // Une source dont le Secret est illisible ne doit pas empêcher les autres d'être
        // réconciliées : on isole la lecture comme le reste.
        let mut wanted: Vec<(usize, Map<String, Value>)> = Vec::new();
        for (idx, source) in config.sources.iter().enumerate() {
            match source.wanted_details() {
                Ok(details) => wanted.push((idx, details)),
                Err(err) => log_err(&format!("[{}] ECHEC lecture : {}", source.name, err)),
            }
        }

        let due = last_full.map_or(true, |at| at.elapsed() >= config.reconcile);
        let drifted = wanted.iter().any(|(idx, details)| {
            let engine = &config.sources[*idx].engine;
            pushed.get(idx) != Some(&datasource::fingerprint(engine, details))
        });

        let mut global_failure = false;
        if !wanted.is_empty() && (due || drifted) {
            match session.call("/api/database", "GET", None) {
                Ok(listing) => {
                    let databases = as_databases(&listing);
                    for (idx, details) in &wanted {
                        let source = &config.sources[*idx];
                        if let Err(err) = reconcile_one(
                            &mut session,
                            &databases,
                            source,
                            details,
                            config.create_if_missing,
                            &mut pushed,
                            *idx,
                        ) {
                            // Une source en échec n'arrête pas les autres.
                            log_err(&format!("[{}] ECHEC : {}", source.name, err));
                        }
                    }
                    last_full = Some(Instant::now());
                }
                Err(err) => {
                    global_failure = true;
                    log_err(&format!("ECHEC : {err}"));
                    if matches!(err.status, Some(401) | Some(403) | Some(429)) {
                        delay = session.backoff(config.poll);
                        log_err(&format!(
                            "authentification refusée — prochaine tentative dans {}s",
                            delay.as_secs()
                        ));
                    }
                }
            }
        }

        // Le heartbeat atteste que le processus PARLE À METABASE. Un échec propre à une source ne
        // le gèle pas (redémarrer n'y changerait rien, et ça masquerait une panne globale derrière
        // un CrashLoop permanent) ; une indisponibilité durable de Metabase, si.
        if !global_failure {
            touch(&config.heartbeat);
        }
        std::thread::sleep(delay);
    }
}

/// L'API renvoie `{"data": [...]}` depuis la v0.47 ; les versions antérieures renvoyaient un
/// tableau nu.
fn as_databases(listing: &Value) -> Vec<Value> {
    match listing {
        Value::Object(map) => map
            .get("data")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
        Value::Array(items) => items.clone(),
        _ => Vec::new(),
    }
}

fn reconcile_one(
    session: &mut Metabase,
    databases: &[Value],
    source: &DataSource,
    want: &Map<String, Value>,
    create_if_missing: bool,
    pushed: &mut HashMap<usize, String>,
    idx: usize,
) -> Result<(), String> {
    let fingerprint = datasource::fingerprint(&source.engine, want);
    let existing = source.find_existing(databases, want)?;
    let user = datasource::comparable(want.get("user"));

    let Some(existing) = existing else {
        if !create_if_missing {
            return Err(format!(
                "la source '{}' n'existe pas et la création automatique est désactivée",
                source.name
            ));
        }
        let payload = json!({"name": source.name, "engine": source.engine, "details": want});
        session
            .call("/api/database", "POST", Some(&payload))
            .map_err(|e| e.to_string())?;
        pushed.insert(idx, fingerprint);
        log(&format!("[{}] source créée (user {})", source.name, user));
        return Ok(());
    };

    let display_name = existing
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or(&source.name)
        .to_string();
    let remote_drift = datasource::remote_drift(existing.get("details"), want);
    let local_drift = pushed.get(&idx) != Some(&fingerprint);
    if remote_drift.is_empty() && !local_drift {
        log(&format!("[{}] à jour (user {})", display_name, user));
        return Ok(());
    }

    // On réécrit `details` sans toucher au nom affiché (les questions référencent l'id) ni aux
    // réglages qui appartiennent à l'utilisateur : Metabase remet à leur défaut les champs absents
    // du payload.
    let mut payload = Map::new();
    payload.insert("name".into(), Value::from(display_name.clone()));
    payload.insert("engine".into(), Value::from(source.engine.clone()));
    payload.insert("details".into(), Value::Object(want.clone()));
    for field in datasource::PRESERVED {
        if let Some(value) = existing.get(field) {
            payload.insert(field.into(), value.clone());
        }
    }
    let id = existing
        .get("id")
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("la source '{display_name}' n'a pas d'identifiant exploitable"))?;
    session
        .call(
            &format!("/api/database/{id}"),
            "PUT",
            Some(&Value::Object(payload)),
        )
        .map_err(|e| e.to_string())?;
    pushed.insert(idx, fingerprint);

    let reason = if remote_drift.is_empty() {
        "état local".to_string()
    } else {
        remote_drift.join(",")
    };
    log(&format!(
        "[{}] réécrite ({}) -> user {}",
        display_name, reason, user
    ));
    Ok(())
}
