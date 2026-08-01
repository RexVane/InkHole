#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod config;
mod desktop;

use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use anyhow::{Context, Result};
use desktop::DesktopState;
use serde_json::{Value, json};
use tauri::{
    AppHandle, Emitter, Manager, State, WebviewUrl, WebviewWindowBuilder, WindowEvent,
    image::Image,
    menu::MenuBuilder,
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::Color,
};
use tauri_plugin_autostart::MacosLauncher;

#[tauri::command]
async fn frontend_call(
    app: AppHandle,
    state: State<'_, Arc<DesktopState>>,
    method: String,
    args: Vec<Value>,
) -> Result<Value, String> {
    state
        .dispatch(&app, &method, args)
        .await
        .map_err(|error| format!("{error:#}"))
}

fn main() {
    if let Err(error) = run() {
        eprintln!("墨洞启动失败: {error:#}");
    }
}

fn run() -> Result<()> {
    let store = config::ConfigStore::discover()?;
    let config = store.load_or_create()?;
    let show_pet = config.show_pet;
    let start_hidden = std::env::args_os().any(|argument| argument == "--hidden");
    let state = Arc::new(DesktopState::new(store, config));

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        .manage(Arc::clone(&state))
        .invoke_handler(tauri::generate_handler![frontend_call])
        .on_menu_event(handle_menu_event)
        .setup(move |app| {
            create_main_window(app, !start_hidden)?;
            create_pet_window(app, show_pet)?;
            create_tray(app)?;

            state.spawn_event_pump(app.handle().clone());
            let app_handle = app.handle().clone();
            let state = Arc::clone(&state);
            tauri::async_runtime::spawn(async move {
                if let Err(error) = state.dispatch(&app_handle, "Start", Vec::new()).await {
                    let _ = app_handle.emit("status", format!("启动失败：{error:#}"));
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .context("无法构建 Tauri 应用")?;

    let stopping = Arc::new(AtomicBool::new(false));
    app.run(move |app_handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event
            && !stopping.swap(true, Ordering::SeqCst)
        {
            api.prevent_exit();
            let app_handle = app_handle.clone();
            let state = app_handle.state::<Arc<DesktopState>>().inner().clone();
            tauri::async_runtime::spawn(async move {
                let _ = state.dispatch(&app_handle, "Stop", Vec::new()).await;
                app_handle.exit(0);
            });
        }
    });
    Ok(())
}

fn create_main_window(app: &mut tauri::App, visible: bool) -> tauri::Result<()> {
    let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("墨洞 InkHole")
        .inner_size(960.0, 640.0)
        .min_inner_size(720.0, 480.0)
        // 全平台统一无边框 + 前端自绘标题栏,macOS 不再出现系统红绿灯与自绘控制条并存。
        .decorations(false)
        .background_color(Color(10, 15, 16, 255))
        .visible(visible)
        .build()?;
    let hide_window = window.clone();
    window.on_window_event(move |event| {
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            let _ = hide_window.hide();
        }
    });
    if visible {
        window.set_focus()?;
    }
    Ok(())
}

fn create_pet_window(app: &mut tauri::App, visible: bool) -> tauri::Result<()> {
    let builder = WebviewWindowBuilder::new(app, "pet", WebviewUrl::App("pet.html".into()))
        .title("墨洞")
        .inner_size(96.0, 96.0)
        .resizable(false)
        .maximizable(false)
        .minimizable(false)
        .closable(false)
        .decorations(false)
        .transparent(true)
        .always_on_top(true)
        .skip_taskbar(true)
        .shadow(false)
        .visible(visible);
    // 仅 macOS:跟随所有工作区(Spaces),对齐 Windows 桌宠常驻桌面的体验。
    // 不在 Windows 上调用,保证 Windows 行为与已验收版本完全一致。
    #[cfg(target_os = "macos")]
    let builder = builder.visible_on_all_workspaces(true);
    builder.build()?;
    Ok(())
}

fn create_tray(app: &mut tauri::App) -> tauri::Result<()> {
    let menu = MenuBuilder::new(app)
        .text("tray-main", "打开墨洞")
        .text("tray-inbox", "打开收件箱")
        .text("tray-pet-show", "显示桌宠")
        .text("tray-pet-hide", "隐藏桌宠")
        .separator()
        .text("app-quit", "退出")
        .build()?;
    let scale = app
        .get_webview_window("main")
        .and_then(|window| window.scale_factor().ok())
        .unwrap_or(1.0);
    let icon = crisp_icon((16.0 * scale).round() as u32)?;
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.set_icon(crisp_icon((32.0 * scale).round() as u32)?);
    }
    TrayIconBuilder::with_id("main-tray")
        .icon(icon)
        .tooltip("墨洞 InkHole")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                invoke_desktop(tray.app_handle(), "ShowMain", Vec::new());
            }
        })
        .build(app)?;
    Ok(())
}

fn handle_menu_event(app: &AppHandle, event: tauri::menu::MenuEvent) {
    let id = event.id().as_ref();
    match id {
        "tray-main" | "pet-main" => invoke_desktop(app, "ShowMain", Vec::new()),
        "tray-inbox" | "pet-inbox" => invoke_desktop(app, "OpenInbox", Vec::new()),
        "tray-pet-show" => invoke_desktop(app, "SetPetVisible", vec![Value::Bool(true)]),
        "tray-pet-hide" => invoke_desktop(app, "SetPetVisible", vec![Value::Bool(false)]),
        "pet-disable" => invoke_desktop(app, "DisablePet", Vec::new()),
        "app-quit" => invoke_desktop(app, "Close", Vec::new()),
        _ => {
            if let Some(instance_id) = id.strip_prefix("pet-select:") {
                invoke_desktop(app, "SelectPeer", vec![json!(instance_id)]);
            }
        }
    }
}

fn invoke_desktop(app: &AppHandle, method: &'static str, args: Vec<Value>) {
    let app = app.clone();
    let state = app.state::<Arc<DesktopState>>().inner().clone();
    tauri::async_runtime::spawn(async move {
        if let Err(error) = state.dispatch(&app, method, args).await {
            let _ = app.emit("status", format!("操作失败：{error:#}"));
        }
    });
}

/// Pick the pre-scaled icon closest to the requested pixel size. Handing the
/// shell a 1024px bitmap makes it downscale with nearest-neighbour, which is
/// what produced the jagged tray icon; these PNGs are Lanczos-scaled offline.
fn crisp_icon(target: u32) -> tauri::Result<Image<'static>> {
    let bytes: &'static [u8] = match target {
        0..=17 => include_bytes!("../../../../desktop/build/windows/icon-16.png"),
        18..=21 => include_bytes!("../../../../desktop/build/windows/icon-20.png"),
        22..=27 => include_bytes!("../../../../desktop/build/windows/icon-24.png"),
        28..=39 => include_bytes!("../../../../desktop/build/windows/icon-32.png"),
        40..=55 => include_bytes!("../../../../desktop/build/windows/icon-48.png"),
        _ => include_bytes!("../../../../desktop/build/windows/icon-64.png"),
    };
    Ok(Image::from_bytes(bytes)?)
}
