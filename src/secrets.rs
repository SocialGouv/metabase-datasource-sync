//! Lecture des credentials dans un Secret Kubernetes monté en volume.
//!
//! C'est la seule forme qui voie la rotation : une variable d'environnement issue d'un Secret est
//! figée au démarrage du pod, alors qu'un volume est réécrit sur place par le kubelet.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::Path;

/// Lit toutes les clés d'un Secret monté dans UNE SEULE génération du volume.
///
/// Le kubelet publie les mises à jour en basculant le lien `..data` d'un coup, mais des lectures
/// successives peuvent enjamber cette bascule et assembler un utilisateur et un mot de passe de
/// générations différentes. On résout donc le lien une fois, on lit dedans, et on vérifie qu'il
/// n'a pas bougé pendant la lecture.
pub fn read_generation(directory: &Path) -> Result<BTreeMap<String, String>, String> {
    for _ in 0..3 {
        let before = fs::canonicalize(directory)
            .map_err(|e| format!("volume {} illisible : {}", directory.display(), e))?;

        match read_all(&before) {
            Ok(files) => {
                let after = fs::canonicalize(directory)
                    .map_err(|e| format!("volume {} illisible : {}", directory.display(), e))?;
                if after == before {
                    return Ok(files);
                }
            }
            // Un fichier disparu en cours de lecture = bascule de génération : on retente.
            Err(err) if err.kind() == io::ErrorKind::NotFound => continue,
            Err(err) => {
                return Err(format!(
                    "lecture de {} impossible : {}",
                    directory.display(),
                    err
                ))
            }
        }
    }
    Err(format!(
        "le volume {} change à chaque lecture",
        directory.display()
    ))
}

fn read_all(directory: &Path) -> io::Result<BTreeMap<String, String>> {
    let mut files = BTreeMap::new();
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        if !entry.file_type()?.is_file() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        let content = fs::read_to_string(entry.path())?;
        files.insert(
            name,
            content
                .trim_end_matches('\n')
                .trim_end_matches('\r')
                .to_string(),
        );
    }
    Ok(files)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::io::Write;

    #[test]
    fn lit_les_cles_sans_la_newline_finale() {
        let dir = std::env::temp_dir().join(format!("mds-secrets-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        // `writeln!` pour PGUSER : on veut justement vérifier que la newline finale est retirée.
        writeln!(File::create(dir.join("PGUSER")).unwrap(), "v-user").unwrap();
        write!(File::create(dir.join("PGPASSWORD")).unwrap(), "s3cret").unwrap();

        let files = read_generation(&dir).unwrap();
        assert_eq!(files.get("PGUSER").map(String::as_str), Some("v-user"));
        assert_eq!(files.get("PGPASSWORD").map(String::as_str), Some("s3cret"));
        fs::remove_dir_all(&dir).ok();
    }
}
