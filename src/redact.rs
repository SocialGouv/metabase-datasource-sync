//! Rédaction des secrets dans tout ce qui sort du processus.
//!
//! Le corps d'erreur d'une réponse HTTP vient d'un tiers : il peut contenir n'importe quoi, y
//! compris ce qu'on vient de lui envoyer. Chaque valeur sensible connue est donc enregistrée ici
//! dès qu'elle est lue, et retirée de tout message avant impression.

use std::sync::{Mutex, OnceLock};

fn registry() -> &'static Mutex<Vec<String>> {
    static REGISTRY: OnceLock<Mutex<Vec<String>>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(Vec::new()))
}

/// Enregistre une valeur à ne jamais imprimer. Les valeurs vides sont ignorées : elles
/// transformeraient la rédaction en remplacement de chaîne vide, donc en bouillie.
pub fn remember(value: &str) {
    if value.is_empty() {
        return;
    }
    let mut secrets = registry().lock().expect("registre des secrets empoisonné");
    if !secrets.iter().any(|known| known == value) {
        secrets.push(value.to_string());
    }
}

pub fn redact(text: &str) -> String {
    let secrets = registry().lock().expect("registre des secrets empoisonné");
    let mut out = text.to_string();
    for secret in secrets.iter() {
        if out.contains(secret.as_str()) {
            out = out.replace(secret.as_str(), "<redacted>");
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn une_valeur_enregistree_disparait_des_messages() {
        remember("hunter2-tres-secret");
        let msg = redact("erreur pour hunter2-tres-secret sur la base");
        assert_eq!(msg, "erreur pour <redacted> sur la base");
    }

    #[test]
    fn une_valeur_vide_ne_casse_pas_la_redaction() {
        remember("");
        assert_eq!(redact("texte intact"), "texte intact");
    }
}
