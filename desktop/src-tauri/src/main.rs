//! PHANTOM desktop shell (Tauri 2).
//!
//! The Rust side is intentionally thin: it owns the window, system dialogs and
//! the path to the bundled `phantom-bridge` sidecar.  All cryptography and all
//! file-format logic lives in the Python core; this file never logs request
//! bodies (which carry passwords) and never passes them on the command line.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::Value;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// The core runs Argon2id (m = 96 MiB, t = 3) and can walk a 24 MP carrier, so
/// minutes are legitimate; forever is not.
const BRIDGE_TIMEOUT: Duration = Duration::from_secs(900);

fn find_bridge() -> Result<PathBuf, String> {
    let exe_name = if cfg!(windows) {
        "phantom-bridge.exe"
    } else {
        "phantom-bridge"
    };
    let mut candidates: Vec<PathBuf> = Vec::new();

    // Release layout: the sidecar is bundled next to the main executable.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join(exe_name));
        }
    }
    // Bundle layouts seen in the wild: Contents/MacOS on macOS (covered by the
    // exe dir above) and an optional Resources/ copy.
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent().and_then(|d| d.parent()) {
            candidates.push(dir.join("Resources").join(exe_name));
        }
    }
    // Dev layout: src-tauri/binaries/phantom-bridge or -<target-triple>.
    if let Some(dir) = option_env!("CARGO_MANIFEST_DIR") {
        let binaries = PathBuf::from(dir).join("binaries");
        candidates.push(binaries.join(exe_name));
        if let Ok(entries) = std::fs::read_dir(&binaries) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                let is_bridge = if cfg!(windows) {
                    name.starts_with("phantom-bridge") && name.ends_with(".exe")
                } else {
                    name.starts_with("phantom-bridge")
                };
                if is_bridge {
                    candidates.push(entry.path());
                }
            }
        }
    }

    candidates
        .into_iter()
        .find(|path| path.is_file())
        .ok_or_else(|| "PHANTOM bridge sidecar was not found next to the app".to_string())
}

fn run_bridge(request: &Value) -> Result<Value, String> {
    let binary = find_bridge()?;
    let mut command = Command::new(&binary);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to start the bridge: {error}"))?;

    {
        let stdin = child.stdin.take().ok_or("bridge stdin is unavailable")?;
        let mut stdin = stdin;
        let line = serde_json::to_vec(request).map_err(|error| error.to_string())?;
        stdin
            .write_all(&line)
            .and_then(|()| stdin.write_all(b"\n"))
            .map_err(|error| format!("failed to write the request: {error}"))?;
        // Dropping stdin closes the pipe: the bridge answers one request and exits.
    }

    let mut stdout = child.stdout.take().ok_or("bridge stdout is unavailable")?;
    let mut stderr = child.stderr.take().ok_or("bridge stderr is unavailable")?;
    let stdout_thread = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stdout.read_to_end(&mut buffer);
        buffer
    });
    let stderr_thread = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stderr.read_to_end(&mut buffer);
        buffer
    });

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => break,
            Ok(None) => {
                if started.elapsed() > BRIDGE_TIMEOUT {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err("the bridge did not answer in time".to_string());
                }
                std::thread::sleep(Duration::from_millis(25));
            }
            Err(error) => return Err(format!("failed to wait for the bridge: {error}")),
        }
    }

    let stdout_bytes = stdout_thread.join().unwrap_or_default();
    let stderr_bytes = stderr_thread.join().unwrap_or_default();

    // The protocol is one JSON object per line; we want the last non-empty one.
    let stdout_text = String::from_utf8_lossy(&stdout_bytes);
    let line = stdout_text.lines().rev().find(|line| !line.trim().is_empty());
    match line {
        Some(line) => serde_json::from_str::<Value>(line)
            .map_err(|error| format!("the bridge answered with invalid JSON: {error}")),
        None => {
            // stderr carries one short line per command by design (never
            // passwords or payload bytes); cap it before it reaches the UI.
            let tail: String = String::from_utf8_lossy(&stderr_bytes)
                .chars()
                .take(300)
                .collect();
            Err(format!("the bridge produced no response (stderr: {tail})"))
        }
    }
}

#[tauri::command]
async fn bridge_call(request: Value) -> Result<Value, String> {
    // Never log `request`: it may contain a password or a document's text.
    tauri::async_runtime::spawn_blocking(move || run_bridge(&request))
        .await
        .map_err(|error| format!("bridge task failed: {error}"))?
}

fn path_fields(path: &std::path::Path) -> Result<Value, String> {
    let size_bytes = std::fs::metadata(path).ok().map(|metadata| metadata.len());
    Ok(serde_json::json!({
        "path": path.to_string_lossy(),
        "name": path
            .file_name()
            .map(|n| n.to_string_lossy())
            .unwrap_or_default(),
        "size_bytes": size_bytes,
    }))
}

/// System file picker for a payload to hide (`secret`) or a carrier
/// (`carrier`).  `media` narrows the carrier picker: `image` (PNG) or
/// `audio` (WAV/FLAC); anything else selects payload files.
#[tauri::command]
async fn pick_file(kind: String, media: String) -> Result<Option<Value>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = rfd::FileDialog::new();
        match (kind.as_str(), media.as_str()) {
            ("carrier", "image") => {
                dialog = dialog.add_filter("PNG image", &["png"]);
            }
            ("carrier", "audio") => {
                dialog = dialog.add_filter("WAV / FLAC audio", &["wav", "flac"]);
            }
            ("carrier", "metadata") => {
                dialog = dialog.add_filter("PNG, WAV or FLAC", &["png", "wav", "flac"]);
            }
            ("secret", _) => {}
            _ => {}
        }
        match dialog.pick_file() {
            Some(path) => path_fields(&path).map(Some),
            None => Ok(None),
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

/// System save dialog.  `kind`: `image` (PNG), `audio` (WAV/FLAC), `text` or
/// `file` (any).  `suggested_dir` re-opens the dialog in the folder the core
/// resolved, so the confirmed name and the written name are the same path.
/// Returns the chosen path or `null` when the user cancels.
#[tauri::command]
async fn save_file(
    suggested_name: String,
    kind: String,
    suggested_dir: Option<String>,
) -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = rfd::FileDialog::new().set_file_name(&suggested_name);
        if let Some(dir) = suggested_dir.filter(|dir| !dir.is_empty()) {
            dialog = dialog.set_directory(dir);
        }
        dialog = match kind.as_str() {
            "image" => dialog.add_filter("PNG image", &["png"]),
            "audio" => dialog.add_filter("WAV / FLAC audio", &["wav", "flac"]),
            "text" => dialog.add_filter("Text file", &["txt"]),
            // The Metadata tab saves a cleaned copy in the source's own format, so
            // the dialog offers the three containers the inspector understands.
            "metadata" => dialog.add_filter("PNG, WAV or FLAC", &["png", "wav", "flac"]),
            _ => dialog,
        };
        Ok(dialog.save_file().map(|path| path.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|error| error.to_string())?
}

/// Reveal a file in the system file manager (or open its folder on Linux).
/// The path is a user-chosen location, not a secret.
#[tauri::command]
async fn reveal_path(path: String) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        let path = std::path::PathBuf::from(&path);
        if !path.exists() {
            return Err("the file does not exist".to_string());
        }
        #[cfg(target_os = "macos")]
        let result = Command::new("open").arg("-R").arg(&path).spawn();
        #[cfg(target_os = "windows")]
        let result = Command::new("explorer")
            .arg(format!("/select,{}", path.to_string_lossy()))
            .spawn();
        #[cfg(all(unix, not(target_os = "macos")))]
        let result = Command::new("xdg-open")
            .arg(path.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| path.clone()))
            .spawn();
        result.map(|_child| ()).map_err(|error| error.to_string())
    })
    .await
    .map_err(|error| error.to_string())?
}

#[tauri::command]
fn app_info() -> HashMap<&'static str, String> {
    let mut info = HashMap::new();
    info.insert("app", env!("CARGO_PKG_VERSION").to_string());
    info.insert("core", "2.1.0".to_string());
    info.insert("name", "PHANTOM desktop (Tauri)".to_string());
    info
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_clipboard_manager::init())
        .invoke_handler(tauri::generate_handler![bridge_call, pick_file, save_file, reveal_path, app_info])
        .run(tauri::generate_context!())
        .expect("error while running the PHANTOM desktop shell");
}
