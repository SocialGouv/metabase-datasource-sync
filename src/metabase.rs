//! Client de l'API Metabase : session, liste des sources, création, réécriture.
//!
//! La session est REUTILISÉE d'un tour à l'autre : se reconnecter à chaque passage alimenterait le
//! throttling de `/api/session`, qui compte par utilisateur ET par IP — une panne
//! d'authentification deviendrait auto-entretenue.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

use crate::redact;

/// Taille maximale d'un corps d'erreur journalisé. Il vient d'un tiers : on le rédige ET on le
/// tronque.
const ERROR_BODY_MAX: usize = 400;

#[derive(Debug)]
pub struct ApiError {
    pub status: Option<u16>,
    pub message: String,
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl ApiError {
    fn transport(message: String) -> Self {
        ApiError {
            status: None,
            message,
        }
    }
}

pub struct Metabase {
    base: String,
    email: String,
    password_file: PathBuf,
    agent: ureq::Agent,
    token: Option<String>,
    auth_failures: u32,
}

impl Metabase {
    pub fn new(base: &str, email: &str, password_file: &Path, timeout: Duration) -> Self {
        // `http_status_as_error(false)` : on veut le code ET le corps d'un 4xx/5xx pour
        // diagnostiquer, pas seulement le code.
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .http_status_as_error(false)
            .timeout_global(Some(timeout))
            .build()
            .into();
        Metabase {
            base: base.trim_end_matches('/').to_string(),
            email: email.to_string(),
            password_file: password_file.to_path_buf(),
            agent,
            token: None,
            auth_failures: 0,
        }
    }

    /// Le mot de passe admin est relu à CHAQUE connexion : sa rotation côté Vault/ESO doit être
    /// prise en compte sans recréer le pod.
    fn read_password(&self) -> Result<String, ApiError> {
        let raw = fs::read_to_string(&self.password_file).map_err(|e| {
            ApiError::transport(format!(
                "mot de passe admin illisible ({}) : {}",
                self.password_file.display(),
                e
            ))
        })?;
        let password = raw
            .trim_end_matches('\n')
            .trim_end_matches('\r')
            .to_string();
        redact::remember(&password);
        Ok(password)
    }

    fn login(&mut self) -> Result<(), ApiError> {
        let password = self.read_password()?;
        let body = json!({ "username": self.email, "password": password });
        let response = self.request("/api/session", "POST", Some(&body), None)?;
        let token = response
            .get("id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        if token.is_empty() {
            // Un 200 sans `id` existe (double authentification exigée, par exemple) : le dire,
            // plutôt que de repartir avec un jeton vide et d'échouer plus loin sans raison claire.
            return Err(ApiError::transport(
                "/api/session a répondu 200 sans identifiant de session — le compte exige \
                 probablement une étape supplémentaire"
                    .to_string(),
            ));
        }
        redact::remember(&token);
        self.token = Some(token);
        self.auth_failures = 0;
        Ok(())
    }

    /// Appelle l'API en rouvrant une session si elle a expiré, une seule fois.
    pub fn call(
        &mut self,
        path: &str,
        method: &str,
        body: Option<&Value>,
    ) -> Result<Value, ApiError> {
        if self.token.is_none() {
            self.login()?;
        }
        let token = self.token.clone();
        match self.request(path, method, body, token.as_deref()) {
            Err(err) if err.status == Some(401) => {
                self.token = None;
                self.login()?;
                let token = self.token.clone();
                self.request(path, method, body, token.as_deref())
            }
            other => other,
        }
    }

    fn request(
        &self,
        path: &str,
        method: &str,
        body: Option<&Value>,
        token: Option<&str>,
    ) -> Result<Value, ApiError> {
        let url = format!("{}{}", self.base, path);
        // ureq distingue au TYPE les requêtes avec et sans corps : les deux chemins ne peuvent pas
        // se rejoindre dans une même variable.
        let sent = match (method, body) {
            ("GET", None) => {
                let mut request = self.agent.get(&url).header("Accept", "application/json");
                if let Some(token) = token {
                    request = request.header("X-Metabase-Session", token);
                }
                request.call()
            }
            ("POST", Some(payload)) | ("PUT", Some(payload)) => {
                let mut request = if method == "POST" {
                    self.agent.post(&url)
                } else {
                    self.agent.put(&url)
                }
                .header("Accept", "application/json");
                if let Some(token) = token {
                    request = request.header("X-Metabase-Session", token);
                }
                request.send_json(payload)
            }
            (other, _) => {
                return Err(ApiError::transport(format!(
                    "méthode {other} non gérée (ou corps incohérent)"
                )));
            }
        };

        let mut response = sent.map_err(|e| {
            ApiError::transport(format!(
                "{} {} -> échec de transport : {}",
                method,
                path,
                redact::redact(&e.to_string())
            ))
        })?;

        let status = response.status().as_u16();
        let raw = response.body_mut().read_to_string().map_err(|e| {
            ApiError::transport(format!("{method} {path} -> corps illisible : {e}"))
        })?;

        if !(200..300).contains(&status) {
            let mut detail = redact::redact(&raw);
            detail.truncate(ERROR_BODY_MAX);
            return Err(ApiError {
                status: Some(status),
                message: format!("{method} {path} -> HTTP {status} : {detail}"),
            });
        }

        if raw.trim().is_empty() {
            return Ok(Value::Null);
        }
        serde_json::from_str(&raw).map_err(|e| {
            ApiError::transport(format!("{method} {path} -> réponse JSON invalide : {e}"))
        })
    }

    /// Temporisation croissante et dispersée après un échec d'authentification, pour ne pas
    /// maintenir le compte sous throttling.
    pub fn backoff(&mut self, poll: Duration) -> Duration {
        self.auth_failures = self.auth_failures.saturating_add(1);
        let exponent = self.auth_failures.min(5);
        let base = poll.as_secs_f64() * 2f64.powi(exponent as i32);
        let capped = base.min(300.0);
        Duration::from_secs_f64(capped * (0.5 + jitter() / 2.0))
    }
}

/// Dispersion tirée de l'horloge : suffisant pour désynchroniser des tentatives, et évite une
/// dépendance de plus pour ça.
fn jitter() -> f64 {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    f64::from(nanos % 1_000) / 1_000.0
}
