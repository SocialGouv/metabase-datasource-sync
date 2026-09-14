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
        let before = generation(directory);

        match read_all(directory) {
            Ok(files) => {
                if generation(directory) == before {
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

/// Identité de la génération publiée. Le kubelet la fait basculer en repointant le lien `..data`
/// d'un coup ; c'est CE lien qu'il faut observer, pas le point de montage, qui ne bouge jamais.
/// Hors Kubernetes (tests, montage à plat), l'absence de `..data` est un cas normal : on retombe
/// sur un marqueur constant, et la vérification devient un non-événement.
fn generation(directory: &Path) -> Option<std::path::PathBuf> {
    fs::canonicalize(directory.join("..data")).ok()
}

fn read_all(directory: &Path) -> io::Result<BTreeMap<String, String>> {
    let mut files = BTreeMap::new();
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        // 🪤 `entry.file_type()` décrit le LIEN, pas sa cible, et un Secret monté n'est qu'une
        // forêt de liens symboliques (`PGUSER -> ..data/PGUSER`). Filtrer là-dessus écarte
        // silencieusement toutes les clés. `Path::is_file()` suit le lien, lui — et écarte au
        // passage `..data` et les répertoires de génération, qui pointent sur des répertoires.
        if !entry.path().is_file() {
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

    /// Reproduit la disposition RÉELLE d'un Secret monté par le kubelet : les clés sont des liens
    /// symboliques vers `..data`, lui-même un lien vers le répertoire de la génération courante.
    /// Un test qui écrit des fichiers ordinaires ne prouve rien de ce montage — c'est précisément
    /// ce qui a laissé passer un filtre sur `DirEntry::file_type()`, lequel décrit le lien et non
    /// sa cible, et écartait donc toutes les clés.
    #[test]
    fn lit_un_secret_monte_comme_le_kubelet() {
        use std::os::unix::fs::symlink;

        let dir = std::env::temp_dir().join(format!("mds-kubelet-{}", std::process::id()));
        fs::remove_dir_all(&dir).ok();
        let generation = dir.join("..2026_09_14_15_00_00.123456789");
        fs::create_dir_all(&generation).unwrap();
        write!(
            File::create(generation.join("PGUSER")).unwrap(),
            "v-reader-aaaa"
        )
        .unwrap();
        write!(
            File::create(generation.join("PGPASSWORD")).unwrap(),
            "s3cret"
        )
        .unwrap();
        symlink(&generation, dir.join("..data")).unwrap();
        symlink("..data/PGUSER", dir.join("PGUSER")).unwrap();
        symlink("..data/PGPASSWORD", dir.join("PGPASSWORD")).unwrap();

        let files = read_generation(&dir).unwrap();
        assert_eq!(
            files.get("PGUSER").map(String::as_str),
            Some("v-reader-aaaa")
        );
        assert_eq!(files.get("PGPASSWORD").map(String::as_str), Some("s3cret"));
        // Les entrées internes du kubelet ne doivent pas être prises pour des clés.
        assert!(
            !files.contains_key("..data"),
            "..data pris pour une clé : {files:?}"
        );
        assert_eq!(files.len(), 2, "clés inattendues : {files:?}");
        fs::remove_dir_all(&dir).ok();
    }

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
