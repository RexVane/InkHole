package main

// 墨洞 InkHole 桌面壳：Wails v3 + 共享 Go 核心。
// 两个窗口：主工作台 + 桌宠挂件（透明置顶小窗，墨洞动画）。

import (
	"embed"
	"log"
	"math"
	"os/exec"
	"runtime"
	"strconv"
	"strings"

	"github.com/wailsapp/wails/v3/pkg/application"
	"github.com/wailsapp/wails/v3/pkg/events"
)

//go:embed all:frontend/dist
var assets embed.FS

//go:embed build/appicon.png
var trayIcon []byte

// adaptivePetSize mirrors the Python shell's _adaptive_pet_size: pet side
// length = system icon base × 1.5. On macOS the Dock tile size is the base
// (users who enlarge their Dock get a matching pet); elsewhere, and when
// the Dock size is unreadable, the common 64px icon base applies.
func adaptivePetSize() int {
	iconBase := 64
	if runtime.GOOS == "darwin" {
		out, err := exec.Command("defaults", "read", "com.apple.dock", "tilesize").Output()
		if err == nil {
			if parsed, parseErr := strconv.ParseFloat(
				strings.TrimSpace(string(out)), 64); parseErr == nil &&
				parsed >= 16 && parsed <= 256 {
				iconBase = int(parsed)
			}
		}
	}
	return petSizeFromIconBase(iconBase)
}

func petSizeFromIconBase(iconBase int) int {
	// Python's round(), used by the legacy shell, rounds exact halves to even.
	return int(math.RoundToEven(float64(iconBase) * 1.5))
}

func main() {
	service := NewInkHoleService()
	app := application.New(application.Options{
		Name:        "墨洞 InkHole",
		Description: "跨平台文件传输 · 局域网自动发现",
		Services: []application.Service{
			application.NewService(service),
		},
		Assets: application.AssetOptions{
			Handler: application.AssetFileServerFS(assets),
		},
		Mac: application.MacOptions{
			ApplicationShouldTerminateAfterLastWindowClosed: false,
		},
		Windows:    application.WindowsOptions{DisableQuitOnLastWindowClosed: true},
		OnShutdown: service.Close,
	})
	service.setApp(app)

	mainWindow := app.Window.NewWithOptions(application.WebviewWindowOptions{
		Name:                  "main",
		Title:                 "墨洞 InkHole",
		Width:                 960,
		Height:                640,
		MinWidth:              720,
		MinHeight:             480,
		BackgroundColour:      application.NewRGB(10, 15, 16),
		URL:                   "/",
		EnableFileDrop:        true,
		MinimiseButtonState:   application.ButtonHidden,
		MaximiseButtonState:   application.ButtonHidden,
		CloseButtonState:      application.ButtonHidden,
		FullscreenButtonState: application.ButtonHidden,
		Mac: application.MacWindow{
			InvisibleTitleBarHeight: 58,
			Backdrop:                application.MacBackdropTranslucent,
			TitleBar:                application.MacTitleBarHidden,
		},
	})

	petSize := adaptivePetSize()
	petWindow := app.Window.NewWithOptions(application.WebviewWindowOptions{
		Name:             "pet",
		Title:            "墨洞",
		Width:            petSize,
		Height:           petSize,
		Frameless:        true,
		AlwaysOnTop:      true,
		DisableResize:    true,
		BackgroundType:   application.BackgroundTypeTransparent,
		BackgroundColour: application.NewRGBA(0, 0, 0, 0),
		URL:              "/pet.html",
		EnableFileDrop:   true,
		Mac: application.MacWindow{
			Backdrop:      application.MacBackdropTransparent,
			DisableShadow: true,
			TitleBar:      application.MacTitleBarHidden,
			WindowLevel:   application.MacWindowLevelFloating,
			CollectionBehavior: application.MacWindowCollectionBehaviorCanJoinAllSpaces |
				application.MacWindowCollectionBehaviorFullScreenAuxiliary,
		},
	})
	service.setPetWindow(petWindow)

	// 桌宠右键菜单——沿用旧版 Qt 桌宠的动作语义:「关闭桌宠」只收起挂件
	// (等于设置里关掉开关),程序继续在托盘运行;「退出程序」才整体退出。
	petMenu := application.NewContextMenu("petMenu")
	petMenu.Add("打开主界面").OnClick(func(*application.Context) { service.ShowMain() })
	petMenu.Add("打开收件箱").OnClick(func(*application.Context) { _ = service.OpenInbox() })
	petMenu.AddSeparator()
	petMenu.Add("关闭桌宠").OnClick(func(*application.Context) { service.DisablePet() })
	petMenu.Add("退出程序").OnClick(func(*application.Context) { app.Quit() })

	mainWindow.RegisterHook(events.Common.WindowClosing, func(event *application.WindowEvent) {
		mainWindow.Hide()
		event.Cancel()
	})
	mainWindow.OnWindowEvent(events.Common.WindowRuntimeReady, func(*application.WindowEvent) {
		mainWindow.Show()
		mainWindow.Focus()
	})
	registerFileDrop := func(window *application.WebviewWindow, name string) {
		window.OnWindowEvent(events.Common.WindowFilesDropped, func(event *application.WindowEvent) {
			files := event.Context().DroppedFiles()
			if len(files) == 0 {
				return
			}
			app.Event.Emit("files-dropped", map[string]any{"window": name, "files": files})
		})
	}
	registerFileDrop(mainWindow, "main")
	registerFileDrop(petWindow, "pet")

	tray := app.SystemTray.New()
	tray.SetTooltip("墨洞 InkHole")
	tray.SetIcon(trayIcon)
	menu := app.NewMenu()
	menu.Add("打开墨洞").OnClick(func(*application.Context) { service.ShowMain() })
	menu.Add("打开收件箱").OnClick(func(*application.Context) { _ = service.OpenInbox() })
	menu.Add("显示桌宠").OnClick(func(*application.Context) { service.SetPetVisible(true) })
	menu.Add("隐藏桌宠").OnClick(func(*application.Context) { service.SetPetVisible(false) })
	menu.AddSeparator()
	menu.Add("退出").OnClick(func(*application.Context) { app.Quit() })
	tray.SetMenu(menu)
	tray.OnClick(service.ShowMain)

	app.Event.OnApplicationEvent(events.Common.ApplicationStarted,
		func(*application.ApplicationEvent) {
			mainWindow.Show()
			go func() {
				if err := service.Start(); err != nil {
					service.emit("status", "启动失败："+err.Error())
					return
				}
				config, err := service.GetConfig()
				if err == nil {
					visible, _ := config["showPet"].(bool)
					service.SetPetVisible(visible)
				}
			}()
		})
	if runtime.GOOS == "darwin" {
		app.Event.OnApplicationEvent(events.Mac.ApplicationShouldHandleReopen,
			func(*application.ApplicationEvent) { service.ShowMain() })
	}

	if err := app.Run(); err != nil {
		log.Fatal(err)
	}
}
