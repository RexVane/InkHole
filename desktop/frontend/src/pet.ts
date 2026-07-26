import {Events, Window as AppWindow} from "@wailsio/runtime";
import * as Service from "../bindings/github.com/rexvane/inkhole/desktop/inkholeservice.js";
import {InkHoleAnimation} from "./inkhole.js";

type Edge = -1 | 0 | 1 | 2 | 3;
type Point = {x: number; y: number};
type Area = {X: number; Y: number; Width: number; Height: number};

interface DragState {
    screenX: number;
    screenY: number;
    dragging: boolean;
}

interface SavedPosition extends Point {
    edge: Edge;
}

const positionKey = "inkhole.pet.position.v1";
const canvas = document.getElementById("hole") as HTMLCanvasElement;
const status = document.getElementById("petStatus") as HTMLDivElement;
const animation = new InkHoleAnimation(canvas);
let activeSendID = "";
let clearTimer = 0;
let collapseTimer = 0;
let dragState: DragState | null = null;
let dragFinishing = false;
let edge: Edge = -1;
let collapsed = false;
let screenArea: Area | null = null;

animation.run();

function showStatus(message: string, hold = 2200): void {
    window.clearTimeout(clearTimer);
    status.textContent = message;
    document.body.title = message;
    if (hold > 0) {
        clearTimer = window.setTimeout(() => {
            status.textContent = "";
        }, hold);
    }
}

function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error || "操作失败");
}

async function animateWindowPosition(target: Point, duration = 260): Promise<void> {
    await Service.MovePetTo(Math.round(target.x), Math.round(target.y), duration);
}

async function currentScreenArea(): Promise<Area> {
    const value = await Service.GetPetScreenArea();
    if (!value || !Number.isFinite(value.X) || !Number.isFinite(value.Y) ||
        !Number.isFinite(value.Width) || !Number.isFinite(value.Height) ||
        Number(value.Width) <= 0 || Number(value.Height) <= 0) {
        throw new Error("无法获取屏幕信息");
    }
    return {
        X: Number(value.X), Y: Number(value.Y),
        Width: Number(value.Width), Height: Number(value.Height),
    };
}

function savePosition(position: Point): void {
    const value: SavedPosition = {x: Math.round(position.x), y: Math.round(position.y), edge};
    localStorage.setItem(positionKey, JSON.stringify(value));
}

function readPosition(): SavedPosition | null {
    try {
        const value = JSON.parse(localStorage.getItem(positionKey) || "null") as SavedPosition | null;
        if (!value || !Number.isFinite(value.x) || !Number.isFinite(value.y) ||
            ![-1, 0, 1, 2, 3].includes(value.edge)) return null;
        return value;
    } catch {
        return null;
    }
}

function expandedPosition(area: Area, size: {width: number; height: number}, position: Point): Point {
    const current = {...position};
    if (edge === 0) current.x = area.X;
    if (edge === 1) current.x = area.X + area.Width - size.width;
    if (edge === 2) current.y = area.Y;
    if (edge === 3) current.y = area.Y + area.Height - size.height;
    current.x = Math.max(area.X, Math.min(current.x, area.X + area.Width - size.width));
    current.y = Math.max(area.Y, Math.min(current.y, area.Y + area.Height - size.height));
    return current;
}

async function expand(): Promise<void> {
    window.clearTimeout(collapseTimer);
    if (edge < 0 || !collapsed || !screenArea) return;
    const size = await AppWindow.Size();
    const current = await AppWindow.Position();
    const target = {...current};
    if (edge === 0) target.x = screenArea.X;
    if (edge === 1) target.x = screenArea.X + screenArea.Width - size.width;
    if (edge === 2) target.y = screenArea.Y;
    if (edge === 3) target.y = screenArea.Y + screenArea.Height - size.height;
    collapsed = false;
    await animateWindowPosition(target);
}

async function collapse(fromSnap = false): Promise<void> {
    if (edge < 0 || collapsed || activeSendID || !screenArea || dragState || (dragFinishing && !fromSnap)) return;
    const size = await AppWindow.Size();
    const current = await AppWindow.Position();
    const peek = Math.max(12, Math.round(Math.min(size.width, size.height) * 0.18));
    const target = {...current};
    if (edge === 0) target.x = screenArea.X - (size.width - peek);
    if (edge === 1) target.x = screenArea.X + screenArea.Width - peek;
    if (edge === 2) target.y = screenArea.Y - (size.height - peek);
    if (edge === 3) target.y = screenArea.Y + screenArea.Height - peek;
    collapsed = true;
    await animateWindowPosition(target);
}

function scheduleCollapse(delay = 800): void {
    window.clearTimeout(collapseTimer);
    collapseTimer = window.setTimeout(() => void collapse(), delay);
}

async function snap(position: Point): Promise<void> {
    const size = await AppWindow.Size();
    // The legacy QML attaches to Screen.width/height, not availableGeometry:
    // the physical screen edge remains the boundary even beside the Dock.
    const area = await currentScreenArea();
    const distances = [
        position.x - area.X,
        area.X + area.Width - (position.x + size.width),
        position.y - area.Y,
        area.Y + area.Height - (position.y + size.height),
    ];
    let closest = 0;
    for (let index = 1; index < distances.length; index++) {
        if (distances[index] < distances[closest]) closest = index;
    }
    const threshold = Math.max(30, Math.round(Math.min(size.width, size.height) * 0.7));
    if (distances[closest] > threshold) {
        edge = -1;
        screenArea = null;
        collapsed = false;
        savePosition(position);
        return;
    }
    edge = closest as Edge;
    screenArea = area;
    collapsed = false;
    const expanded = {...position};
    if (edge === 0) expanded.x = area.X;
    if (edge === 1) expanded.x = area.X + area.Width - size.width;
    if (edge === 2) expanded.y = area.Y;
    if (edge === 3) expanded.y = area.Y + area.Height - size.height;
    savePosition(expanded);
    await collapse(true);
}

async function restorePosition(): Promise<void> {
    const size = await AppWindow.Size();
    const saved = readPosition();
    screenArea = await currentScreenArea();
    if (!saved) {
        edge = 1;
        const position = {
            x: screenArea.X + screenArea.Width - size.width,
            y: screenArea.Y + Math.round(size.height * 0.8),
        };
        await AppWindow.SetPosition(position.x, position.y);
        savePosition(position);
        scheduleCollapse(700);
        return;
	}
	edge = saved.edge;
	await AppWindow.SetPosition(saved.x, saved.y);
	// Resolve the screen after restoring so positions on a still-connected
	// secondary display remain there. expandedPosition also clamps free
	// (edge=-1) positions, recovering coordinates saved on a removed display.
	screenArea = await currentScreenArea();
	const target = expandedPosition(screenArea, size, saved);
	await AppWindow.SetPosition(target.x, target.y);
	savePosition(target);
	if (edge >= 0) {
		scheduleCollapse(1200);
	} else {
		// Recover positions written by the earlier broken snap implementation:
		// it could save edge=-1 even though the pet was exactly on a screen edge.
		// A legitimately free position cannot be this close because snap() would
		// already have attached it when it was saved.
		const distances = [
			target.x - screenArea.X,
			screenArea.X + screenArea.Width - (target.x + size.width),
			target.y - screenArea.Y,
			screenArea.Y + screenArea.Height - (target.y + size.height),
		];
		const threshold = Math.max(30, Math.round(Math.min(size.width, size.height) * 0.7));
		if (Math.min(...distances) <= threshold) await snap(target);
	}
}

async function send(paths: string[]): Promise<void> {
    if (!paths.length) return;
    try {
        await expand();
        activeSendID = await Service.SendToSelected(paths);
        animation.active = true;
        animation.progress = 0;
        showStatus("正在发送", 0);
    } catch (error) {
        animation.active = false;
        animation.progress = -1;
        showStatus(errorMessage(error), 4200);
        scheduleCollapse();
    }
}

async function finishNativeDrag(state: DragState): Promise<void> {
    dragFinishing = true;
    try {
        await Service.DragPet();
        // The native drag has ended at this point. Clear the active gesture
        // before snap() so collapse(true) is not rejected as "still dragging".
        if (dragState === state) dragState = null;
        const position = await AppWindow.Position();
        await snap(position);
    } catch (error) {
        showStatus(errorMessage(error), 4200);
    } finally {
        if (dragState === state) dragState = null;
        canvas.style.cursor = "grab";
        dragFinishing = false;
    }
}

canvas.addEventListener("mousedown", (event) => {
    if (event.button !== 0 || dragState || dragFinishing) return;
    window.clearTimeout(collapseTimer);
    void Service.CancelPetMotion();
    dragState = {
        screenX: event.screenX,
        screenY: event.screenY,
        dragging: false,
    };
    canvas.style.cursor = "grabbing";
});

window.addEventListener("mousemove", (event) => {
    if (!dragState) return;
    const dx = event.screenX - dragState.screenX;
    const dy = event.screenY - dragState.screenY;
    if (!dragState.dragging && Math.abs(dx) + Math.abs(dy) > 4) {
        dragState.dragging = true;
        void finishNativeDrag(dragState);
    }
});

window.addEventListener("mouseup", (event) => {
    if (event.button !== 0) return;
    const current = dragState;
    if (!current || current.dragging) return;
    dragState = null;
    canvas.style.cursor = "grab";
    void Service.ShowMain();
});
canvas.addEventListener("pointerenter", () => void expand());
canvas.addEventListener("pointerleave", () => scheduleCollapse());
// 右键交给 Wails 原生菜单(--custom-contextmenu: petMenu),不在此拦截。

Events.On("files-dropped", (event) => {
    const data = (event.data || {}) as Record<string, any>;
    if (data.window === "pet" && Array.isArray(data.files)) void send(data.files as string[]);
});
Events.On("progress", (event) => {
    const data = (event.data || {}) as Record<string, any>;
    if (data.kind !== "send") return;
    const done = Number(data.done || 0);
    const total = Number(data.total || 0);
    animation.active = true;
    animation.progress = total > 0 ? Math.min(1, done / total) : 0;
    showStatus(`${String(data.filename || "发送中")} ${Math.round(animation.progress * 100)}%`, 0);
    void expand();
});
Events.On("sent", (event) => {
    const data = (event.data || {}) as Record<string, any>;
    if (!data.ok) showStatus(String(data.error || "发送失败"), 4200);
});
Events.On("transfer-finished", (event) => {
    const data = (event.data || {}) as Record<string, any>;
    if (activeSendID && data.sendId !== activeSendID) return;
    const succeeded = Number(data.succeeded || 0);
    const total = Number(data.total || 0);
    animation.active = false;
    animation.progress = succeeded === total ? 1 : -1;
    activeSendID = "";
    showStatus(succeeded === total ? "发送完成" : `${succeeded}/${total} 项成功`);
    window.setTimeout(() => {
        if (!activeSendID) animation.progress = -1;
    }, 2200);
    scheduleCollapse(2600);
});
Events.On("status", (event) => {
    const message = String(event.data || "");
    if (message.includes("异常") || message.includes("失败")) showStatus(message, 4200);
});

void restorePosition().catch((error) => showStatus(errorMessage(error), 4200));
