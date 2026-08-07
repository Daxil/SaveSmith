#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! The shell around the window.
//!
//! It does one thing: run the `savesmith` binary as a child process and carry
//! JSON-RPC lines between it and the webview. Every rule about what may be
//! edited — the backup, the acknowledgements, the risk tier, the Steam Cloud
//! steps — lives in the Python core. Nothing here inspects a message, and
//! nothing here can decide that a write is allowed.
//!
//! The pipe is the point. A local HTTP port with no authentication is reachable
//! by any page the user happens to have open in a browser, and this process can
//! rewrite save files; a pipe is reachable only by the process that created it.
//!
//! Answers are matched by id rather than by arrival order, because long calls
//! (`scan`, `discover`) emit progress notifications before their result. Taking
//! the first line back as the answer would hand the window a progress report
//! and call it a save file.

use std::collections::HashMap;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Mutex;

use serde_json::{json, Value};
use tauri::async_runtime::Receiver;
use tauri::{AppHandle, Emitter, Manager, RunEvent, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tokio::sync::oneshot;

/// The backend process, and the answers it still owes the window.
struct Backend {
    /// `None` once the process has been killed on the way out.
    child: Mutex<Option<CommandChild>>,
    next_id: AtomicI64,
    waiting: Mutex<HashMap<i64, oneshot::Sender<Value>>>,
}

impl Backend {
    fn new(child: CommandChild) -> Self {
        Self {
            child: Mutex::new(Some(child)),
            next_id: AtomicI64::new(1),
            waiting: Mutex::new(HashMap::new()),
        }
    }

    /// Stop waiting for an answer that will never come.
    fn forget(&self, id: i64) {
        self.waiting.lock().unwrap().remove(&id);
    }
}

/// One request, one answer.
///
/// The whole JSON-RPC envelope goes back untouched, `error` and all, so that
/// this transport and the development one hand the window exactly the same
/// thing. A refusal from the core is an answer, not a failure of the shell —
/// only a backend that has stopped talking is that.
#[tauri::command]
async fn rpc(backend: State<'_, Backend>, method: String, params: Value) -> Result<Value, String> {
    let id = backend.next_id.fetch_add(1, Ordering::Relaxed);
    let (sender, receiver) = oneshot::channel();
    backend.waiting.lock().unwrap().insert(id, sender);

    let request = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
    let mut line = match serde_json::to_string(&request) {
        Ok(text) => text,
        Err(problem) => {
            backend.forget(id);
            return Err(format!("Запрос не удалось собрать: {problem}"));
        }
    };
    line.push('\n');

    let written = {
        let mut guard = backend.child.lock().unwrap();
        match guard.as_mut() {
            Some(child) => child
                .write(line.as_bytes())
                .map_err(|problem| problem.to_string()),
            None => Err("SaveSmith уже закрыт.".to_string()),
        }
    };
    if let Err(problem) = written {
        backend.forget(id);
        return Err(format!("SaveSmith не отвечает: {problem}"));
    }

    receiver
        .await
        .map_err(|_| "SaveSmith закрылся, не ответив. Ничего не записано.".to_string())
}

/// Anything the window wants written down.
///
/// A webview's console goes nowhere a person can find, so when the interface
/// threw and unmounted itself the user got a black window and nobody — not
/// them, not us — had a single line to go on. These land in the app's own
/// output, beside the backend's.
#[tauri::command]
fn log(level: String, message: String) {
    eprintln!("[window/{level}] {message}");
}

/// Read the backend's output for as long as it keeps talking.
fn pump(app: AppHandle, mut events: Receiver<CommandEvent>) {
    tauri::async_runtime::spawn(async move {
        while let Some(event) = events.recv().await {
            match event {
                // The shell plugin delivers stdout one line at a time; the
                // split is there so a change in that behaviour degrades into
                // slower parsing rather than into lost answers.
                CommandEvent::Stdout(bytes) => {
                    for line in String::from_utf8_lossy(&bytes).split('\n') {
                        deliver(&app, line);
                    }
                }
                // The core puts tracebacks here and never on stdout. They are a
                // bug report, so they go to the log, not on screen.
                CommandEvent::Stderr(bytes) => {
                    eprint!("[savesmith] {}", String::from_utf8_lossy(&bytes));
                }
                CommandEvent::Error(problem) => eprintln!("[savesmith] {problem}"),
                CommandEvent::Terminated(status) => {
                    eprintln!("[savesmith] закончил работу: {status:?}");
                    // Everyone still waiting is woken by the senders dropping.
                    app.state::<Backend>().waiting.lock().unwrap().clear();
                }
                _ => {}
            }
        }
    });
}

fn deliver(app: &AppHandle, line: &str) {
    let line = line.trim();
    if line.is_empty() {
        return;
    }
    let Ok(message) = serde_json::from_str::<Value>(line) else {
        eprintln!("[savesmith] ответ не разобран как JSON: {line}");
        return;
    };

    match message.get("id").and_then(Value::as_i64) {
        Some(id) => {
            if let Some(sender) = app.state::<Backend>().waiting.lock().unwrap().remove(&id) {
                // The receiver is gone if the window navigated away mid-call.
                let _ = sender.send(message);
            }
        }
        // A progress notification. Nothing waits on it, so a window that does
        // not listen simply misses it.
        None => {
            let payload = message.get("params").cloned().unwrap_or(Value::Null);
            let _ = app.emit("progress", payload);
        }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let (events, child) = app.shell().sidecar("savesmith")?.args(["rpc"]).spawn()?;
            app.manage(Backend::new(child));
            pump(app.handle().clone(), events);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![rpc, log])
        .build(tauri::generate_context!())
        // Almost always the sidecar: a shell built without `npm run sidecar`
        // has nothing to talk to, and a window with no backend would be a
        // frame around an error message.
        .expect("окно не открылось — скорее всего рядом нет бэкенда savesmith");

    app.run(|handle, event| {
        // A backend left running holds an open handle on a save file, which is
        // the one thing this program must never do behind the user's back.
        if let RunEvent::Exit = event {
            if let Some(child) = handle.state::<Backend>().child.lock().unwrap().take() {
                let _ = child.kill();
            }
        }
    });
}
