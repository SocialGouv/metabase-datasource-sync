//! Une source de données à réconcilier : sa configuration, l'état voulu, et la recherche de son
//! équivalent côté Metabase.

use std::collections::BTreeMap;
use std::path::PathBuf;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::redact;
use crate::secrets;

/// Champs de `details` visibles dans la réponse de l'API — le password, lui, n'est jamais renvoyé.
/// Ils servent à détecter une dérive DISTANTE ; la dérive LOCALE se détecte par empreinte
/// complète, ce qui couvre une rotation de password sans changement d'utilisateur.
pub const VISIBLE: [&str; 4] = ["host", "port", "dbname", "user"];

/// Réglages appartenant à l'utilisateur, pas à ce programme : ils sont relus de l'existant et
/// renvoyés tels quels dans le PUT. Sans ça, Metabase remet à leur défaut les champs absents du
/// payload (`is_on_demand` → false, `cache_ttl` → nil).
pub const PRESERVED: [&str; 7] = [
    "is_on_demand",
    "is_full_sync",
    "auto_run_queries",
    "cache_ttl",
    "refingerprint",
    "schedules",
    "settings",
];

/// Bases internes de Metabase : les toucher est refusé par l'API, et les confondre avec une cible
/// serait un détournement.
pub const RESERVED: [&str; 3] = ["is_sample", "is_audit", "is_attached_dwh"];

pub struct DataSource {
    pub name: String,
    pub engine: String,
    pub dir: PathBuf,
    pub keys: BTreeMap<String, String>,
    pub extra: Map<String, Value>,
}

impl DataSource {
    /// Construit le bloc `details` depuis le Secret monté.
    pub fn wanted_details(&self) -> Result<Map<String, Value>, String> {
        let files = secrets::read_generation(&self.dir)?;
        let mut details = Map::new();
        for (field, filename) in &self.keys {
            let raw = files.get(filename).ok_or_else(|| {
                format!(
                    "clé {} absente du Secret monté sur {}",
                    filename,
                    self.dir.display()
                )
            })?;
            let value = if field == "port" {
                match raw.parse::<i64>() {
                    Ok(port) => Value::from(port),
                    Err(_) => {
                        return Err(format!("port {raw:?} non numérique (fichier {filename})"))
                    }
                }
            } else {
                Value::from(raw.clone())
            };
            details.insert(field.clone(), value);
        }
        for (field, value) in &self.extra {
            details.insert(field.clone(), value.clone());
        }
        // Tout ce qui ressemble à un secret dans `details` est enregistré pour rédaction, pas
        // seulement le champ `password` : un consommateur peut en poser d'autres.
        for (field, value) in &details {
            if field.contains("password") || field == "pass" || field == "tunnel-pass" {
                if let Some(text) = value.as_str() {
                    redact::remember(text);
                }
            }
        }
        Ok(details)
    }

    /// Trouve LA source à mettre à jour, ou `None`. Échoue si la situation est ambiguë : mieux vaut
    /// refuser bruyamment que réécrire la connexion d'une source qui ne nous appartient pas.
    pub fn find_existing<'a>(
        &self,
        databases: &'a [Value],
        want: &Map<String, Value>,
    ) -> Result<Option<&'a Value>, String> {
        let usable: Vec<&Value> = databases
            .iter()
            .filter(|db| {
                !RESERVED
                    .iter()
                    .any(|flag| db.get(*flag) == Some(&Value::Bool(true)))
            })
            .collect();

        let by_name: Vec<&&Value> = usable
            .iter()
            .filter(|db| db.get("name").and_then(Value::as_str) == Some(self.name.as_str()))
            .collect();
        if by_name.len() > 1 {
            return Err(format!(
                "{} sources portent le nom '{}' — refus de choisir",
                by_name.len(),
                self.name
            ));
        }
        if let Some(found) = by_name.first() {
            let engine = found.get("engine").and_then(Value::as_str).unwrap_or("");
            if engine != self.engine {
                return Err(format!(
                    "la source '{}' existe avec le moteur '{}', attendu '{}' — refus de la réécrire",
                    self.name, engine, self.engine
                ));
            }
            return Ok(Some(found));
        }

        // Repli sur l'adressage : rattrape une source créée à la main sous un autre nom. On exige
        // l'adresse COMPLÈTE et le moteur — un même host/dbname sur un autre port est une autre
        // base.
        let candidates: Vec<&&Value> = usable
            .iter()
            .filter(|db| {
                db.get("engine").and_then(Value::as_str) == Some(self.engine.as_str())
                    && ["host", "port", "dbname"].iter().all(|field| {
                        let have = db.get("details").and_then(|d| d.get(*field));
                        comparable(have) == comparable(want.get(*field))
                    })
            })
            .collect();
        if candidates.len() > 1 {
            return Err(format!(
                "{} sources pointent déjà {}:{}/{} — refus de choisir",
                candidates.len(),
                comparable(want.get("host")),
                comparable(want.get("port")),
                comparable(want.get("dbname"))
            ));
        }
        Ok(candidates.first().copied().copied())
    }
}

/// Rend un champ JSON sous une forme comparable, quel que soit son type d'origine : un port peut
/// revenir en nombre ou en chaîne selon les versions.
pub fn comparable(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "null".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

/// Empreinte de l'état voulu, secrets compris — c'est elle qui détecte une rotation dont
/// l'utilisateur ne change pas (un password renouvelé en place, par exemple).
pub fn fingerprint(engine: &str, details: &Map<String, Value>) -> String {
    let payload = serde_json::json!({ "engine": engine, "details": details });
    let canonical = serde_json::to_string(&payload).unwrap_or_default();
    let digest = Sha256::digest(canonical.as_bytes());
    format!("{digest:x}")
}

/// Champs de `details` qui ont dérivé côté Metabase, parmi ceux que l'API renvoie.
pub fn remote_drift(have: Option<&Value>, want: &Map<String, Value>) -> Vec<&'static str> {
    VISIBLE
        .iter()
        .filter(|field| {
            let mine = have.and_then(|d| d.get(**field));
            comparable(mine) != comparable(want.get(**field))
        })
        .copied()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn want() -> Map<String, Value> {
        json!({"host": "h", "port": 5432, "dbname": "app", "user": "v-1", "password": "p"})
            .as_object()
            .unwrap()
            .clone()
    }

    fn source() -> DataSource {
        DataSource {
            name: "Cible".into(),
            engine: "postgres".into(),
            dir: PathBuf::from("/nowhere"),
            keys: BTreeMap::new(),
            extra: Map::new(),
        }
    }

    #[test]
    fn empreinte_sensible_au_password_seul() {
        let mut autre = want();
        autre.insert("password".into(), Value::from("p2"));
        assert_ne!(
            fingerprint("postgres", &want()),
            fingerprint("postgres", &autre)
        );
    }

    #[test]
    fn ignore_les_bases_reservees_meme_a_la_bonne_adresse() {
        let dbs = vec![json!({
            "id": 1, "name": "Sample Database", "engine": "postgres", "is_sample": true,
            "details": {"host": "h", "port": 5432, "dbname": "app"}
        })];
        assert!(source().find_existing(&dbs, &want()).unwrap().is_none());
    }

    #[test]
    fn refuse_deux_candidates_a_la_meme_adresse() {
        let db = json!({
            "id": 1, "name": "Copie", "engine": "postgres",
            "details": {"host": "h", "port": 5432, "dbname": "app"}
        });
        let dbs = vec![db.clone(), db];
        let err = source().find_existing(&dbs, &want()).unwrap_err();
        assert!(
            err.contains("refus de choisir"),
            "message inattendu : {err}"
        );
    }

    #[test]
    fn un_port_different_est_une_autre_base() {
        let dbs = vec![json!({
            "id": 1, "name": "Ailleurs", "engine": "postgres",
            "details": {"host": "h", "port": 6543, "dbname": "app"}
        })];
        assert!(source().find_existing(&dbs, &want()).unwrap().is_none());
    }

    #[test]
    fn refuse_de_reecrire_une_source_dun_autre_moteur() {
        let dbs = vec![json!({"id": 1, "name": "Cible", "engine": "mysql", "details": {}})];
        let err = source().find_existing(&dbs, &want()).unwrap_err();
        assert!(
            err.contains("refus de la réécrire"),
            "message inattendu : {err}"
        );
    }

    #[test]
    fn un_port_en_chaine_equivaut_a_un_port_numerique() {
        let dbs = vec![json!({
            "id": 1, "name": "Ailleurs", "engine": "postgres",
            "details": {"host": "h", "port": "5432", "dbname": "app"}
        })];
        assert!(source().find_existing(&dbs, &want()).unwrap().is_some());
    }
}
