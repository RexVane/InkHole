type EventPayload = {name: string; data: unknown; sender?: string};
type EventCallback = (event: EventPayload) => void;
type Unlisten = () => void;

declare global {
  interface Window {
    __TAURI__: {
      core: {
        invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
      };
      dpi: {
        PhysicalPosition: new (x: number, y: number) => unknown;
      };
      event: {
        listen<T>(name: string, callback: (event: {payload: T}) => void): Promise<Unlisten>;
      };
      webview: {
        getCurrentWebview(): {
          label: string;
          onDragDropEvent(callback: (event: {payload: DragDropPayload}) => void): Promise<Unlisten>;
        };
      };
      window: {
        getCurrentWindow(): TauriWindow;
      };
    };
  }
}

type DragDropPayload =
  | {type: "enter" | "over"; position: {x: number; y: number}; paths?: string[]}
  | {type: "drop"; position: {x: number; y: number}; paths: string[]}
  | {type: "leave"};

interface TauriWindow {
  outerPosition(): Promise<{x: number; y: number}>;
  outerSize(): Promise<{width: number; height: number}>;
  scaleFactor(): Promise<number>;
  setPosition(position: unknown): Promise<void>;
  hide(): Promise<void>;
  minimize(): Promise<void>;
  toggleMaximize(): Promise<void>;
  startDragging(): Promise<void>;
}

const methodNames = new Map<number, string>([
  [1606092712, "AcceptOneTime"],
  [3564958705, "AutostartEnabled"],
  [4195178976, "CancelPetMotion"],
  [2554164107, "CancelSend"],
  [1280695090, "CancelTransportSession"],
  [1094046213, "CheckForUpdate"],
  [27151125, "CheckSSH"],
  [3665540653, "ChooseFiles"],
  [1460320850, "ChooseFolder"],
  [3353811010, "ChooseInbox"],
  [4281227866, "ChooseInboxCategory"],
  [1651455851, "ClearRecent"],
  [3358856085, "Close"],
  [963152884, "CreateOneTime"],
  [949770729, "CreateSSHPairing"],
  [3432393247, "CrossNetworkConfig"],
  [901081746, "DisablePet"],
  [3952069318, "DragPet"],
  [2299530779, "GetConfig"],
  [1654588883, "GetPetScreenArea"],
  [2964859496, "GetSelected"],
  [774114716, "JoinOneTime"],
  [3582432961, "JoinSSHPairing"],
  [3580292482, "ManualPeers"],
  [1723196142, "MovePetTo"],
  [3310189257, "OpenInbox"],
  [4119985044, "OpenPath"],
  [656087303, "OpenPetMenu"],
  [1155708841, "OpenReleases"],
  [322226103, "OpenRepository"],
  [1667773428, "Peers"],
  [2597322811, "RecentFiles"],
  [287367892, "RefreshDiscovery"],
  [776819465, "RejectOneTime"],
  [3599902979, "RemoveSSHPeer"],
  [236602228, "SaveConfig"],
  [4185740830, "SaveInboxClassification"],
  [74294601, "SaveManualPeers"],
  [1643904814, "SaveSSHConfig"],
  [2778361349, "SaveWormholeConfig"],
  [3137040255, "SelectPeer"],
  [3022705747, "SendPaths"],
  [519415057, "SendToSelected"],
  [468953190, "SetAutostart"],
  [2827133472, "SetPetVisible"],
  [274662635, "ShowMain"],
  [3502938117, "Start"],
  [4248441943, "Stop"],
]);

const localListeners = new Map<string, Set<EventCallback>>();
const tauriListeners = new Map<string, Promise<Unlisten>>();

function dispatch(name: string, data: unknown, sender?: string): void {
  for (const callback of localListeners.get(name) || []) {
    try {
      callback({name, data, sender});
    } catch {
      // Match the Wails runtime: one listener must not break the others.
    }
  }
}

function ensureTauriListener(name: string): void {
  if (tauriListeners.has(name)) return;
  tauriListeners.set(name, window.__TAURI__.event.listen(name, (event) => {
    dispatch(name, event.payload);
  }));
}

export const Call = {
  ByID<T = unknown>(id: number, ...args: unknown[]): Promise<T> {
    const method = methodNames.get(id);
    if (!method) return Promise.reject(new Error(`unknown desktop method id: ${id}`));
    return window.__TAURI__.core.invoke<T>("frontend_call", {method, args});
  },
};

export const Events = {
  On(name: string, callback: EventCallback): Unlisten {
    let listeners = localListeners.get(name);
    if (!listeners) {
      listeners = new Set();
      localListeners.set(name, listeners);
      ensureTauriListener(name);
    }
    listeners.add(callback);
    return () => listeners?.delete(callback);
  },
};

export const Clipboard = {
  async SetText(value: string): Promise<void> {
    await navigator.clipboard.writeText(value);
  },
  async Text(): Promise<string> {
    return navigator.clipboard.readText();
  },
};

function currentWindow(): TauriWindow {
  return window.__TAURI__.window.getCurrentWindow();
}

export const Window = {
  async Position(): Promise<{x: number; y: number}> {
    return currentWindow().outerPosition();
  },
  async Size(): Promise<{width: number; height: number}> {
    return currentWindow().outerSize();
  },
  async SetPosition(x: number, y: number): Promise<void> {
    const position = new window.__TAURI__.dpi.PhysicalPosition(x, y);
    await currentWindow().setPosition(position);
  },
  async Hide(): Promise<void> {
    await currentWindow().hide();
  },
  async Minimise(): Promise<void> {
    await currentWindow().minimize();
  },
  async ToggleMaximise(): Promise<void> {
    await currentWindow().toggleMaximize();
  },
};

export const Create = {Events: {}};

function clearDropTarget(): void {
  document.querySelectorAll(".file-drop-target-active").forEach((element) => {
    element.classList.remove("file-drop-target-active");
  });
}

async function updateDropTarget(position: {x: number; y: number}): Promise<void> {
  const scale = await currentWindow().scaleFactor();
  const element = document.elementFromPoint(position.x / scale, position.y / scale);
  const target = element?.closest<HTMLElement>("[data-file-drop-target]");
  clearDropTarget();
  target?.classList.add("file-drop-target-active");
}

if (typeof window !== "undefined") {
  window.addEventListener("DOMContentLoaded", () => {
    document.querySelector<HTMLElement>(".titlebar")?.addEventListener("mousedown", (event) => {
      if (event.button !== 0 || (event.target as Element).closest("button, input, a")) return;
      void currentWindow().startDragging();
    });

    const webview = window.__TAURI__.webview.getCurrentWebview();
    void webview.onDragDropEvent((event) => {
      if (event.payload.type === "leave") {
        clearDropTarget();
        return;
      }
      if (event.payload.type === "drop") {
        clearDropTarget();
        dispatch("files-dropped", {
          window: webview.label,
          files: event.payload.paths,
        });
        return;
      }
      void updateDropTarget(event.payload.position);
    });
  });
}

export type CancellablePromise<T> = Promise<T>;
export const CancellablePromise = Promise;
