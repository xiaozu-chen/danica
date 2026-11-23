#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    net::TcpStream,
    path::Path,
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use tauri::Manager;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

struct ManagedProcesses {
    native: Mutex<Vec<Child>>,
    sidecar: Mutex<Option<CommandChild>>,
}

fn wait_for_api() -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", 8765)).is_ok() {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(100));
    }
    Err("Danica API sidecar did not become ready on port 8765".into())
}

#[tauri::command]
fn api_url() -> String {
    env::var("DANICA_API_URL").unwrap_or_else(|_| "http://127.0.0.1:8765".into())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(ManagedProcesses {
            native: Mutex::new(Vec::new()),
            sidecar: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![api_url])
        .setup(|app| {
            let dev = cfg!(debug_assertions);
            if dev {
                let python = env::var("DANICA_PYTHON")
                    .map(Into::into)
                    .unwrap_or_else(|_| {
                        if cfg!(windows) {
                            Path::new(env!("CARGO_MANIFEST_DIR"))
                                .join("..")
                                .join("..")
                                .join(".venv")
                                .join("Scripts")
                                .join("python.exe")
                        } else {
                            "python3".into()
                        }
                    });
                let api = Command::new(python)
                    .args(["-m", "web.sidecar", "--port", "8765"])
                    .current_dir(Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join(".."))
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .spawn()?;
                app.state::<ManagedProcesses>()
                    .native
                    .lock()
                    .unwrap()
                    .push(api);
            } else {
                let (mut events, child): (_, CommandChild) = app
                    .shell()
                    .sidecar("binaries/danica-api")?
                    .args(["--port", "8765"])
                    .spawn()?;
                *app.state::<ManagedProcesses>().sidecar.lock().unwrap() = Some(child);
                thread::spawn(move || {
                    while let Some(event) = tauri::async_runtime::block_on(events.recv()) {
                        if let CommandEvent::Terminated(payload) = event {
                            eprintln!("Danica API sidecar exited: {payload:?}");
                            break;
                        }
                    }
                });
            }
            wait_for_api().map_err(std::io::Error::other)?;
            let window = app.get_webview_window("main").expect("main Tauri window");
            if !dev {
                window.navigate(api_url().parse().expect("valid local API URL"))?;
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let state = window.state::<ManagedProcesses>();
                if let Ok(mut children) = state.native.lock() {
                    for child in children.iter_mut() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
                if let Ok(mut sidecar) = state.sidecar.lock() {
                    if let Some(child) = sidecar.take() {
                        let _ = child.kill();
                    }
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running Danica desktop shell");
}
