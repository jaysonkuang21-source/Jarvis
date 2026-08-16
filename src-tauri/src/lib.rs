//! Desktop shell for Jarvis.
//!
//! Owns the tray, autostart, and native notifications. Spawns the FastAPI
//! sidecar on launch so timers keep firing after the window is closed.
//! Mints a local API token, injects it into the sidecar env, and exposes it
//! to the webview via ``get_api_token`` (stdio is discarded).

#[cfg(not(windows))]
use std::io::Read;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, State, WindowEvent};
use tauri_plugin_autostart::MacosLauncher;

struct SidecarState(Mutex<Option<Child>>);

/// Shared secret handed to the FastAPI child and the frontend.
struct ApiTokenState(Mutex<String>);

fn project_root(app: &AppHandle) -> Option<std::path::PathBuf> {
    // Dev: resource dir is src-tauri; repo root is one level up.
    // Release: look next to the executable for a colocated backend.
    if cfg!(debug_assertions) {
        let resource = app.path().resource_dir().ok()?;
        return Some(resource.parent()?.to_path_buf());
    }
    app.path().executable_dir().ok()
}

/// Fill ``buf`` from the OS CSPRNG (no extra crate dependency).
fn fill_os_random(buf: &mut [u8]) -> bool {
    #[cfg(windows)]
    {
        #[link(name = "bcrypt")]
        extern "system" {
            fn BCryptGenRandom(
                h_algorithm: *mut core::ffi::c_void,
                pb_buffer: *mut u8,
                cb_buffer: u32,
                dw_flags: u32,
            ) -> i32;
        }
        const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 0x0000_0002;
        unsafe {
            BCryptGenRandom(
                std::ptr::null_mut(),
                buf.as_mut_ptr(),
                buf.len() as u32,
                BCRYPT_USE_SYSTEM_PREFERRED_RNG,
            ) >= 0
        }
    }
    #[cfg(not(windows))]
    {
        match std::fs::File::open("/dev/urandom") {
            Ok(mut file) => file.read_exact(buf).is_ok(),
            Err(_) => false,
        }
    }
}

/// Mint a cryptographically strong process-local token (256-bit hex).
fn mint_api_token() -> String {
    let mut bytes = [0u8; 32];
    if !fill_os_random(&mut bytes) {
        // Extremely unlikely; refuse a weak fallback rather than DefaultHasher.
        panic!("OS CSPRNG failed while minting JARVIS_API_TOKEN");
    }
    let mut out = String::with_capacity(64);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

/// Prefer an existing JARVIS_API_TOKEN; otherwise mint one for this session.
fn resolve_api_token() -> String {
    std::env::var("JARVIS_API_TOKEN")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(mint_api_token)
}

fn spawn_sidecar(app: &AppHandle, api_token: &str) -> Option<Child> {
    let root = project_root(app)?;

    // Prefer a packaged sidecar binary when present; otherwise drive the
    // in-repo Python entrypoint via uv so a developer checkout just works.
    let packaged = root
        .join("src-tauri")
        .join("binaries")
        .join(sidecar_name());
    let packaged_alt = root.join("binaries").join(sidecar_name());

    let mut command = if packaged.exists() {
        Command::new(packaged)
    } else if packaged_alt.exists() {
        Command::new(packaged_alt)
    } else {
        let mut cmd = Command::new("uv");
        cmd.arg("run")
            .arg("python")
            .arg("-m")
            .arg("app.main")
            .current_dir(&root);
        cmd
    };

    // Always inject the token for desktop — do not soft-default the child to empty.
    command.env("JARVIS_API_TOKEN", api_token);
    command
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .stdin(Stdio::null());

    match command.spawn() {
        Ok(child) => {
            eprintln!("Jarvis sidecar started (pid {})", child.id());
            Some(child)
        }
        Err(error) => {
            eprintln!("Could not start Jarvis sidecar: {error}");
            None
        }
    }
}

fn sidecar_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "jarvis-sidecar-x86_64-pc-windows-msvc.exe"
    } else if cfg!(target_os = "macos") {
        if cfg!(target_arch = "aarch64") {
            "jarvis-sidecar-aarch64-apple-darwin"
        } else {
            "jarvis-sidecar-x86_64-apple-darwin"
        }
    } else {
        "jarvis-sidecar-x86_64-unknown-linux-gnu"
    }
}

fn stop_sidecar(state: &SidecarState) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Return the session API token minted (or inherited) for the sidecar.
#[tauri::command]
fn get_api_token(state: State<'_, ApiTokenState>) -> String {
    state
        .0
        .lock()
        .map(|guard| guard.clone())
        .unwrap_or_default()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--autostart"]),
        ))
        .manage(SidecarState(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![get_api_token])
        .setup(|app| {
            let handle = app.handle().clone();
            let api_token = resolve_api_token();
            app.manage(ApiTokenState(Mutex::new(api_token.clone())));

            if let Some(child) = spawn_sidecar(&handle, &api_token) {
                if let Ok(mut guard) = app.state::<SidecarState>().0.lock() {
                    *guard = Some(child);
                }
            }

            let show = MenuItem::with_id(app, "show", "Open Jarvis", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .tooltip("Jarvis")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main_window(app),
                    "quit" => {
                        // State<'_, T> must be borrowed — passing it by value fails E0308.
                        stop_sidecar(&app.state::<SidecarState>());
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                })
                .build(app)?;

            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the window hides to the tray so timers keep running.
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Jarvis")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                stop_sidecar(&app_handle.state::<SidecarState>());
            }
        });
}
