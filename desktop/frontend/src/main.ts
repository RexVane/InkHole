import {Clipboard, Events, Window as AppWindow} from "@wailsio/runtime";
import QRCode from "qrcode";
import * as Service from "../bindings/github.com/rexvane/inkhole/desktop/inkholeservice.js";
import type {
    ManualPeerConfig,
    PeerView,
    SSHPeerConfig,
    SSHSettingsInput,
} from "../bindings/github.com/rexvane/inkhole/desktop/models.js";
import {InkHoleAnimation} from "./inkhole.js";

const byID = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const homePage = byID<HTMLElement>("homePage");
const settingsPage = byID<HTMLElement>("settings");
const settingsScroll = settingsPage.querySelector<HTMLElement>(".settings-scroll")!;
const settingsButton = byID<HTMLButtonElement>("openSettings");
const settingsTitle = byID<HTMLElement>("settingsTitle");
const settingsBack = byID<HTMLButtonElement>("closeSettings");
const settingsCancel = byID<HTMLButtonElement>("cancelSettings");
const settingsSave = byID<HTMLButtonElement>("saveSettings");
const categoryFields = byID<HTMLFieldSetElement>("categoryFields");
const sshFields = byID<HTMLFieldSetElement>("sshFields");
const oneTimeSourceDialog = byID<HTMLDialogElement>("oneTimeSourceDialog");
const sendCodeDialog = byID<HTMLDialogElement>("sendCodeDialog");
const receiveCodeDialog = byID<HTMLDialogElement>("receiveCodeDialog");
const sshFingerprintDialog = byID<HTMLDialogElement>("sshFingerprintDialog");
const usageGuide = byID<HTMLDialogElement>("usageGuide");
const peerList = byID<HTMLUListElement>("peerList");
const receivedList = byID<HTMLUListElement>("receivedList");
const animation = new InkHoleAnimation(byID<HTMLCanvasElement>("hole"));

let peers: PeerView[] = [];
let selectedID = "";
let activeSendID = "";
let sendSessionID = "";
let receiveSessionID = "";
let pendingOfferID = "";
let codeExpiryTimer = 0;
let appConfig: Record<string, any> = {};
let crossConfig: Record<string, any> = {};
let sshPeerDraft: SSHPeerConfig[] = [];
let sshPassphraseDirty = false;
type SettingsState = "closed" | "loading" | "ready" | "saving" | "error";
type InboxCategory = "media" | "archive" | "file" | "folder";

const inboxCategories: InboxCategory[] = ["media", "archive", "file", "folder"];
const usageGuideSeenKey = "inkhole.usage-guide-seen.v1";

let settingsSession = 0;
let settingsState: SettingsState = "closed";

animation.run();

function errorMessage(error: unknown): string {
    const message = error instanceof Error ? error.message : String(error ?? "");
    return message.trim() || "操作失败";
}

function toast(message: string, error = false): void {
    const text = message.trim();
    if (!text) return;
    const item = document.createElement("div");
    item.className = `toast${error ? " error" : ""}`;
    item.textContent = text;
    byID("toastStack").append(item);
    window.setTimeout(() => item.remove(), 4200);
}

async function run(action: () => Promise<unknown>, success = ""): Promise<void> {
    try {
        await action();
        if (success) toast(success);
    } catch (error) {
        toast(errorMessage(error), true);
    }
}

function basename(path: string): string {
    return path.split(/[/\\]/).filter(Boolean).pop() || path;
}

function dirname(path: string): string {
    const parts = path.split(/[/\\]/);
    parts.pop();
    return parts.join(path.includes("\\") ? "\\" : "/") || path;
}

function formatBytes(value: number): string {
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    const amount = value / Math.pow(1024, index);
    return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function setNodeState(message: string, kind: "online" | "error" | "waiting" = "online"): void {
    const state = byID("sidebarStatus");
    state.className = `sidebar-status ${kind}`;
    state.textContent = message;
}

function selectedPeer(): PeerView | undefined {
    return peers.find((peer) => peer.instanceId === selectedID);
}

function updateTarget(): void {
    const peer = selectedPeer();
    const fileButton = byID<HTMLButtonElement>("chooseFiles");
    const folderButton = byID<HTMLButtonElement>("chooseFolder");
    if (!peer) {
        byID("targetName").textContent = peers.length ? "选择目标设备" : "正在发现设备";
        byID("targetMeta").textContent = peers.length ? "从右侧选择发送目标" : "等待附近的墨洞上线";
        byID("targetRoute").hidden = true;
        byID("stageTitle").textContent = "等待选择设备";
        byID("stageSubtitle").textContent = "局域网、Tailscale 与中继设备会汇总显示";
        byID("selectionNote").textContent = "未选择目标设备";
        fileButton.disabled = true;
        folderButton.disabled = true;
        return;
    }
    byID("targetName").textContent = peer.name || "未命名设备";
    byID("targetMeta").textContent = peer.instanceId;
    const route = byID("targetRoute");
    route.hidden = false;
    route.textContent = (peer.routes || []).join(" + ") || peer.transport;
    byID("stageTitle").textContent = `发送到 ${peer.name || "未命名设备"}`;
    byID("stageSubtitle").textContent = peer.instanceId;
    byID("selectionNote").textContent = (peer.routes || []).join(" · ") || peer.transport;
    fileButton.disabled = false;
    folderButton.disabled = false;
}

async function selectPeer(instanceID: string): Promise<void> {
    selectedID = instanceID;
    renderPeers();
    updateTarget();
    try {
        await Service.SelectPeer(instanceID);
    } catch (error) {
        toast(errorMessage(error), true);
    }
}

function renderPeers(): void {
    peerList.replaceChildren();
    byID("deviceCount").textContent = String(peers.length);
    if (!peers.some((peer) => peer.instanceId === selectedID)) selectedID = "";
    if (!peers.length) {
        const empty = document.createElement("li");
        empty.className = "empty-state";
        empty.textContent = "正在发现设备 · 跨网设备可在设置中手动添加";
        peerList.append(empty);
        updateTarget();
        return;
    }
    for (const peer of peers) {
        const item = document.createElement("li");
        item.className = `device-item${peer.instanceId === selectedID ? " selected" : ""}`;
        item.tabIndex = 0;
        item.title = `${peer.name}\n${peer.instanceId}`;
        const avatar = document.createElement("div");
        avatar.className = "device-avatar";
        avatar.textContent = (peer.name || "墨").trim().slice(0, 1).toUpperCase();
        const copy = document.createElement("div");
        copy.className = "device-copy";
        const name = document.createElement("strong");
        name.textContent = peer.name || "未命名设备";
        const id = document.createElement("code");
        id.textContent = peer.instanceId;
        const routes = document.createElement("div");
        routes.className = "device-routes";
        for (const value of peer.routes || [peer.transport]) {
            if (!value) continue;
            const tag = document.createElement("span");
            tag.textContent = value;
            routes.append(tag);
        }
        copy.append(name, id, routes);
        item.append(avatar, copy);
        item.addEventListener("click", () => void selectPeer(peer.instanceId));
        item.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") void selectPeer(peer.instanceId);
        });
        peerList.append(item);
    }
}

function renderRecent(paths: string[]): void {
    receivedList.replaceChildren();
    byID("recentCount").textContent = String(paths.length);
    if (!paths.length) {
        const empty = document.createElement("li");
        empty.className = "empty-state";
        empty.textContent = "暂无接收记录";
        receivedList.append(empty);
        return;
    }
    for (const path of paths) {
        const item = document.createElement("li");
        item.className = "recent-item";
        const icon = document.createElement("span");
        icon.className = "file-icon";
        icon.textContent = "▤";
        const copy = document.createElement("div");
        copy.className = "recent-copy";
        const name = document.createElement("strong");
        name.textContent = basename(path);
        name.title = path;
        const parent = document.createElement("span");
        parent.textContent = dirname(path);
        const open = document.createElement("button");
        open.type = "button";
        open.title = "打开";
        open.setAttribute("aria-label", `打开 ${basename(path)}`);
        open.textContent = "↗";
        open.addEventListener("click", () => void run(() => Service.OpenPath(path)));
        copy.append(name, parent);
        item.append(icon, copy, open);
        receivedList.append(item);
    }
}

async function reloadPeers(): Promise<void> {
    peers = (await Service.Peers()) || [];
    const saved = await Service.GetSelected();
    if (saved) selectedID = saved;
    renderPeers();
    updateTarget();
}

async function reloadRecent(): Promise<void> {
    renderRecent((await Service.RecentFiles()) || []);
}

async function sendPaths(paths: string[]): Promise<void> {
    if (!paths.length) return;
    let peer = selectedPeer();
    if (!peer && peers.length === 1) {
        await selectPeer(peers[0].instanceId);
        peer = peers[0];
    }
    if (!peer) {
        toast("请先选择发送目标", true);
        return;
    }
    try {
        activeSendID = await Service.SendPaths(peer.instanceId, paths);
        animation.active = true;
        animation.progress = 0;
        byID("cancelSend").hidden = false;
        byID("transferStrip").hidden = false;
        byID("transferName").textContent = paths.length === 1 ? basename(paths[0]) : `${paths.length} 项内容`;
        byID("transferDetail").textContent = "正在建立传输通道";
    } catch (error) {
        toast(errorMessage(error), true);
    }
}

let recvIdleTimer = 0;

function hideTransferStrip(): void {
    if (activeSendID) return;
    byID("transferStrip").hidden = true;
    animation.active = false;
    animation.progress = -1;
}

function setProgress(data: Record<string, any>): void {
    const kind = String(data.kind || "send");
    // 发送进行中时发送条优先，接收进度不抢显示。
    if (kind !== "send" && activeSendID) return;
    const total = Number(data.total || 0);
    const done = Number(data.done || 0);
    const ratio = total > 0 ? Math.max(0, Math.min(1, done / total)) : 0;
    byID("transferStrip").hidden = false;
    byID("transferName").textContent =
        (kind === "recv" ? "接收 · " : "") + String(data.filename || "正在传输");
    byID("transferDetail").textContent = `${formatBytes(done)} / ${formatBytes(total)} · ${Math.round(ratio * 100)}%`;
    byID<HTMLProgressElement>("transferProgress").value = ratio;
    animation.active = true;
    animation.progress = ratio;
    if (kind === "recv") {
        // 接收没有 transfer-finished 收尾事件：完成后短暂展示即收起，
        // 对端中途取消则在无进度 8 秒后收起，避免进度条永久卡住。
        window.clearTimeout(recvIdleTimer);
        recvIdleTimer = window.setTimeout(hideTransferStrip,
            total > 0 && done >= total ? 2200 : 8000);
    }
}

function finishProgress(data: Record<string, any>): void {
    const succeeded = Number(data.succeeded || 0);
    const total = Number(data.total || 0);
    window.clearTimeout(recvIdleTimer);
    byID("transferName").textContent = succeeded === total ? "传输完成" : "传输结束";
    byID("transferDetail").textContent = `${succeeded} / ${total} 项成功`;
    byID<HTMLProgressElement>("transferProgress").value = total > 0 ? succeeded / total : 0;
    byID("cancelSend").hidden = true;
    activeSendID = "";
    animation.active = false;
    animation.progress = succeeded === total ? 1 : -1;
    window.setTimeout(() => {
        if (!activeSendID) {
            byID("transferStrip").hidden = true;
            animation.progress = -1;
        }
    }, 2200);
}

let manualPeerSaveTimer = 0;
function scheduleManualPeerSave(): void {
    window.clearTimeout(manualPeerSaveTimer);
    manualPeerSaveTimer = window.setTimeout(() => {
        const peers = collectManualPeers();
        // 空 host 的未完成行不触发保存;有 host 即自动持久化,无需点保存。
        if (peers.some((peer) => !peer.host)) return;
        void run(async () => {
            await Service.SaveManualPeers(peers);
        }, "已自动保存固定地址设备");
    }, 700);
}

function addManualPeerRow(peer?: ManualPeerConfig): void {
    const list = byID("manualPeerList");
    list.querySelector(".empty-state")?.remove();
    const row = document.createElement("div");
    row.className = "manual-peer-row";
    row.dataset.instance = peer?.instance_id || "";
    row.dataset.fingerprint = peer?.fingerprint || "";
    const name = document.createElement("input");
    name.placeholder = "备注";
    name.value = peer?.name || "";
    name.dataset.field = "name";
    const host = document.createElement("input");
    host.placeholder = "IP 或主机名";
    host.value = peer?.host || "";
    host.dataset.field = "host";
    const port = document.createElement("input");
    port.type = "number";
    port.min = "1";
    port.max = "65535";
    port.placeholder = "端口";
    port.value = peer?.port ? String(peer.port) : "";
    port.dataset.field = "port";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.title = "删除";
    remove.setAttribute("aria-label", "删除固定地址设备");
    remove.textContent = "×";
    for (const input of [name, host, port]) {
        input.addEventListener("change", scheduleManualPeerSave);
    }
    remove.addEventListener("click", () => {
        row.remove();
        showManualEmpty();
        scheduleManualPeerSave();
    });
    row.append(name, host, port, remove);
    list.append(row);
}

function showManualEmpty(): void {
    const list = byID("manualPeerList");
    if (list.querySelector(".manual-peer-row")) return;
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "未添加固定地址设备";
    list.append(empty);
}

function renderManualPeers(values: ManualPeerConfig[]): void {
    const list = byID("manualPeerList");
    list.replaceChildren();
    values.forEach((peer) => addManualPeerRow(peer));
    showManualEmpty();
}

function collectManualPeers(): ManualPeerConfig[] {
    const result: ManualPeerConfig[] = [];
    for (const row of byID("manualPeerList").querySelectorAll<HTMLElement>(".manual-peer-row")) {
        const value = (field: string) => row.querySelector<HTMLInputElement>(`[data-field="${field}"]`)!.value.trim();
        const host = value("host");
        if (!host && !value("name") && !value("port")) continue;
        result.push({
            name: value("name"),
            host,
            port: Number(value("port")),
            instance_id: row.dataset.instance || undefined,
            fingerprint: row.dataset.fingerprint || undefined,
        });
    }
    return result;
}

function categoryInput(category: InboxCategory): HTMLInputElement {
    return document.querySelector<HTMLInputElement>(`[data-category="${category}"]`)!;
}

function fillInboxClassification(): void {
    const directories = (appConfig.inboxCategoryDirs || {}) as Record<string, unknown>;
    const enabled = appConfig.inboxAutoClassify === true;
    byID<HTMLInputElement>("cfgAutoClassify").checked = enabled;
    for (const category of inboxCategories) {
        categoryInput(category).value = String(directories[category] || "");
    }
    categoryFields.disabled = !enabled;
}

function collectInboxCategoryDirs(): Record<InboxCategory, string> {
    return {
        media: categoryInput("media").value.trim(),
        archive: categoryInput("archive").value.trim(),
        file: categoryInput("file").value.trim(),
        folder: categoryInput("folder").value.trim(),
    };
}

function updateClassificationControls(): void {
    categoryFields.disabled = !byID<HTMLInputElement>("cfgAutoClassify").checked;
}

function updateEncryptionControls(): void {
    const enabled = byID<HTMLInputElement>("cfgEncrypt").checked;
    const secret = byID<HTMLInputElement>("cfgSecret");
    secret.disabled = !enabled;
    secret.placeholder = enabled
        ? (appConfig.hasSecret ? "留空保持不变" : "启用端到端加密后必填")
        : "启用端到端加密后可填写";
}

function updateSSHKeyFields(): void {
    const pasted = byID<HTMLSelectElement>("sshKeyMode").value === "paste";
    byID("sshFileFields").hidden = pasted;
    byID("sshPasteFields").hidden = !pasted;
}

function updateSSHControls(): void {
    sshFields.disabled = !byID<HTMLInputElement>("sshEnabled").checked;
    updateSSHKeyFields();
}

function collectSSHInput(): SSHSettingsInput {
    return {
        enabled: byID<HTMLInputElement>("sshEnabled").checked,
        host: byID<HTMLInputElement>("sshHost").value.trim(),
        port: Number(byID<HTMLInputElement>("sshPort").value || 22),
        user: byID<HTMLInputElement>("sshUser").value.trim(),
        privateKeyMode: byID<HTMLSelectElement>("sshKeyMode").value,
        privateKeyPath: byID<HTMLInputElement>("sshKeyPath").value.trim(),
        pastedKey: byID<HTMLTextAreaElement>("sshPastedKey").value,
        passphrase: sshPassphraseDirty ? byID<HTMLInputElement>("sshPassphrase").value : "",
        clearPassphrase: sshPassphraseDirty && !byID<HTMLInputElement>("sshPassphrase").value,
        hostKeySHA256: byID<HTMLInputElement>("sshFingerprint").value.trim(),
        peers: sshPeerDraft.map((peer) => ({...peer})),
    };
}

function sshPeerDraftValue(value: Record<string, any>): SSHPeerConfig {
    return {
        id: String(value.id || ""),
        name: String(value.name || ""),
        instance_id: String(value.instance_id || ""),
        remote_port: Number(value.remote_port || 0),
        noise_public: String(value.noise_public || ""),
        end_to_end: value.end_to_end !== false,
    };
}

function renderSSHPeers(): void {
    const list = byID<HTMLUListElement>("sshPeerList");
    list.replaceChildren();
    if (!sshPeerDraft.length) {
        const empty = document.createElement("li");
        empty.className = "empty-state";
        empty.textContent = "暂无配对设备";
        list.append(empty);
        return;
    }
    sshPeerDraft.forEach((peer, index) => {
        const item = document.createElement("li");
        item.className = "paired-item";
        const copy = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = String(peer.name || "未命名设备");
        const id = document.createElement("code");
        id.textContent = String(peer.instance_id || "");
        const encryption = document.createElement("label");
        encryption.className = "check-row peer-encryption";
        const encryptionInput = document.createElement("input");
        encryptionInput.type = "checkbox";
        encryptionInput.checked = peer.end_to_end !== false;
        encryptionInput.addEventListener("change", () => {
            sshPeerDraft[index].end_to_end = encryptionInput.checked;
            if (!encryptionInput.checked) {
                toast("已关闭外层加密，VPS 管理员可能读取传输内容和元数据", true);
            }
        });
        encryption.append(encryptionInput, "外层加密");
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "删除";
        remove.addEventListener("click", () => {
            if (readySettingsSession() === null) return;
            sshPeerDraft.splice(index, 1);
            renderSSHPeers();
            byID("settingsMessage").textContent = "配对设备更改将在保存后生效";
        });
        copy.append(name, id);
        item.append(copy, encryption, remove);
        list.append(item);
    });
}

function fillSSHConfig(): void {
    const ssh = crossConfig.ssh || {};
    const profile = ssh.profile || {};
    byID<HTMLInputElement>("sshEnabled").checked = Boolean(ssh.enabled);
    byID<HTMLInputElement>("sshHost").value = String(profile.host || "");
    byID<HTMLInputElement>("sshPort").value = String(profile.port || 22);
    byID<HTMLInputElement>("sshUser").value = String(profile.user || "");
    byID<HTMLSelectElement>("sshKeyMode").value = profile.private_key_mode === "paste" ? "paste" : "file";
    byID<HTMLInputElement>("sshKeyPath").value = String(profile.private_key_path || "");
    byID<HTMLTextAreaElement>("sshPastedKey").value = "";
    byID<HTMLInputElement>("sshPassphrase").value = "";
    sshPassphraseDirty = false;
    byID<HTMLInputElement>("sshFingerprint").value = String(profile.host_key_sha256 || "");
    sshPeerDraft = (Array.isArray(ssh.peers) ? ssh.peers : []).map(sshPeerDraftValue);
    fillSSHRuntimeState();
    renderSSHPeers();
    updateSSHControls();
}

function fillSSHRuntimeState(): void {
    const ssh = crossConfig.ssh || {};
    byID("sshState").textContent = ssh.connected ? "已连接" : ssh.enabled ? "正在连接" : "已关闭";
    byID("sshCheckResult").textContent = ssh.hasPassphrase ? "已保存私钥口令" : "";
}

function isCurrentSettingsSession(session: number, state: SettingsState = "ready"): boolean {
    return settingsSession === session && settingsState === state && !settingsPage.hidden;
}

function readySettingsSession(): number | null {
    return settingsState === "ready" && !settingsPage.hidden ? settingsSession : null;
}

async function refreshCrossConfig(session: number, syncPeers = false): Promise<void> {
    const refreshed = (await Service.CrossNetworkConfig()) || {};
    if (!isCurrentSettingsSession(session)) return;
    crossConfig = refreshed;
    // Background connection events must not replace form values the user is editing.
    fillSSHRuntimeState();
    if (syncPeers) {
        const peers = Array.isArray(crossConfig.ssh?.peers) ? crossConfig.ssh.peers : [];
        sshPeerDraft = peers.map(sshPeerDraftValue);
        renderSSHPeers();
    }
}

function showPage(page: "home" | "settings"): void {
    const showSettings = page === "settings";
    homePage.hidden = showSettings;
    settingsPage.hidden = !showSettings;
    settingsButton.setAttribute("aria-pressed", String(showSettings));
    const target = showSettings ? settingsPage : homePage;
    target.classList.remove("page-enter");
    void target.offsetWidth;
    target.classList.add("page-enter");
}

function setSettingsState(state: SettingsState): void {
    settingsState = state;
    const busy = state === "loading" || state === "saving";
    const contentDisabled = state !== "ready";
    if (busy) settingsPage.setAttribute("aria-busy", "true");
    else settingsPage.removeAttribute("aria-busy");
    settingsScroll.inert = contentDisabled;
    settingsScroll.setAttribute("aria-disabled", String(contentDisabled));
    settingsBack.disabled = state === "saving";
    settingsCancel.disabled = state === "saving";
    settingsSave.disabled = state !== "ready";
}

function closeSettings(): void {
    if (settingsState === "saving" || settingsState === "closed" || settingsPage.hidden) return;
    settingsSession += 1;
    setSettingsState("closed");
    showPage("home");
    settingsButton.focus({preventScroll: true});
}

async function openSettings(): Promise<void> {
    if (settingsState !== "closed" || !settingsPage.hidden) return;
    const session = ++settingsSession;
    window.scrollTo(0, 0);
    setSettingsState("loading");
    showPage("settings");
    settingsScroll.scrollTop = 0;
    byID("settingsMessage").textContent = "正在加载设置";
    settingsTitle.focus({preventScroll: true});
    try {
        const [config, cross, manual, autostartEnabled] = await Promise.all([
            Service.GetConfig(),
            Service.CrossNetworkConfig(),
            Service.ManualPeers(),
            Service.AutostartEnabled(),
        ]);
        if (!isCurrentSettingsSession(session, "loading")) return;
        appConfig = config || {};
        crossConfig = cross || {};
        byID<HTMLInputElement>("cfgName").value = String(appConfig.peerName || "");
        byID<HTMLInputElement>("cfgInstance").value = String(appConfig.instanceId || "");
        byID<HTMLInputElement>("cfgInbox").value = String(appConfig.inbox || "");
        byID<HTMLInputElement>("cfgPort").value = String(appConfig.port || 0);
        byID<HTMLInputElement>("cfgSecret").value = "";
        byID<HTMLInputElement>("cfgEncrypt").checked = appConfig.encryptionEnabled === true;
        byID<HTMLInputElement>("cfgShowPet").checked = appConfig.showPet !== false;
        byID<HTMLInputElement>("cfgAutostart").checked = autostartEnabled === true;
        appConfig.autostartEnabled = autostartEnabled === true;
        fillInboxClassification();
        updateEncryptionControls();
        byID("actualPort").textContent = appConfig.actualPort ? `当前端口 ${appConfig.actualPort}` : "启动后自动分配";
        byID("secretState").textContent = appConfig.encryptionEnabled
            ? "已启用端到端加密"
            : (appConfig.hasSecret ? "已保存口令，加密未启用" : "未设置端到端口令");
        byID("updateState").textContent = `当前版本 v${appConfig.version || ""}`;
        byID("openReleases").hidden = true;
        byID("settingsMessage").textContent = String(appConfig.warning || "");
        const wormhole = crossConfig.wormhole || {};
        byID<HTMLInputElement>("wormholeRendezvous").value = String(wormhole.rendezvous_url || "");
        byID<HTMLInputElement>("wormholeRelay").value = String(wormhole.transit_relay || "");
        renderManualPeers(manual || []);
        fillSSHConfig();
        setSettingsState("ready");
        settingsScroll.scrollTop = 0;
    } catch (error) {
        if (!isCurrentSettingsSession(session, "loading")) return;
        byID("settingsMessage").textContent = errorMessage(error);
        setSettingsState("error");
        toast(errorMessage(error), true);
    }
}

async function saveSettings(): Promise<void> {
    const session = readySettingsSession();
    if (session === null) return;
    const values = {
        peerName: byID<HTMLInputElement>("cfgName").value,
        inbox: byID<HTMLInputElement>("cfgInbox").value,
        secret: byID<HTMLInputElement>("cfgSecret").value,
        port: Number(byID<HTMLInputElement>("cfgPort").value || 0),
        showPet: byID<HTMLInputElement>("cfgShowPet").checked,
        autostart: byID<HTMLInputElement>("cfgAutostart").checked,
        encryptionEnabled: byID<HTMLInputElement>("cfgEncrypt").checked,
        autoClassify: byID<HTMLInputElement>("cfgAutoClassify").checked,
        categoryDirs: collectInboxCategoryDirs(),
        manualPeers: collectManualPeers(),
        wormholeRendezvous: byID<HTMLInputElement>("wormholeRendezvous").value,
        wormholeRelay: byID<HTMLInputElement>("wormholeRelay").value,
        ssh: collectSSHInput(),
    };
    if (!values.peerName.trim()) {
        byID("settingsMessage").textContent = "设备名称不能为空";
        byID<HTMLInputElement>("cfgName").focus({preventScroll: true});
        return;
    }
    if (!values.inbox.trim()) {
        byID("settingsMessage").textContent = "默认目录不能为空";
        byID<HTMLButtonElement>("chooseInbox").focus({preventScroll: true});
        return;
    }
    if (!Number.isInteger(values.port) || values.port < 0 || values.port > 65535) {
        byID("settingsMessage").textContent = "本机监听端口必须为 0 到 65535 的整数";
        byID<HTMLInputElement>("cfgPort").focus({preventScroll: true});
        return;
    }
    if (values.encryptionEnabled && !values.secret && appConfig.hasSecret !== true) {
        byID("settingsMessage").textContent = "启用端到端加密后必须填写加密口令";
        byID<HTMLInputElement>("cfgSecret").focus({preventScroll: true});
        return;
    }
    if (values.manualPeers.some((peer) => !peer.host || !Number.isInteger(peer.port) || peer.port < 1 || peer.port > 65535)) {
        byID("settingsMessage").textContent = "固定地址设备的地址或端口无效";
        byID<HTMLButtonElement>("addManualPeer").focus({preventScroll: true});
        return;
    }
    if (values.ssh.enabled) {
        if (!values.ssh.host || !values.ssh.user || !Number.isInteger(values.ssh.port) || values.ssh.port < 1 || values.ssh.port > 65535) {
            byID("settingsMessage").textContent = "SSH 主机、端口或用户无效";
            byID<HTMLInputElement>("sshHost").focus({preventScroll: true});
            return;
        }
        const hasPrivateKey = values.ssh.privateKeyMode === "paste"
            ? Boolean(values.ssh.pastedKey || crossConfig.ssh?.hasPastedKey)
            : Boolean(values.ssh.privateKeyPath);
        if (!hasPrivateKey) {
            byID("settingsMessage").textContent = "请先选择或粘贴 SSH 私钥";
            byID<HTMLButtonElement>("chooseSSHKey").focus({preventScroll: true});
            return;
        }
        if (!values.ssh.hostKeySHA256) {
            byID("settingsMessage").textContent = "请先检测并确认 SSH 主机指纹";
            byID<HTMLButtonElement>("checkSSH").focus({preventScroll: true});
            return;
        }
    }
    setSettingsState("saving");
    byID("settingsMessage").textContent = "正在保存";
    try {
        await Service.SaveConfig(
            values.peerName,
            values.inbox,
            values.secret,
            false,
            values.port,
            values.showPet,
            values.encryptionEnabled,
        );
        await Service.SaveInboxClassification(values.autoClassify, values.categoryDirs);
        await Service.SaveManualPeers(values.manualPeers);
        await Service.SaveWormholeConfig(
            values.wormholeRendezvous,
            values.wormholeRelay,
        );
        await Service.SaveSSHConfig(values.ssh);
        const actual = await Service.SetAutostart(values.autostart);
        if (actual !== values.autostart) {
            throw new Error("系统未能应用开机自启设置");
        }
        appConfig.autostartEnabled = actual;
        if (!isCurrentSettingsSession(session, "saving")) return;
        setSettingsState("ready");
        closeSettings();
        toast("设置已保存");
        void Promise.all([reloadPeers(), reloadRecent()]).catch((error) => {
            toast(errorMessage(error), true);
        });
    } catch (error) {
        if (!isCurrentSettingsSession(session, "saving")) return;
        byID("settingsMessage").textContent = errorMessage(error);
        setSettingsState("ready");
        settingsSave.focus({preventScroll: true});
    }
}

function showModal(dialog: HTMLDialogElement): void {
    if (!dialog.open) dialog.showModal();
}

function closeModal(dialog: HTMLDialogElement): void {
    if (dialog.open) dialog.close();
}

function confirmSSHFingerprint(fingerprint: string): Promise<boolean> {
    byID("sshFingerprintCandidate").textContent = fingerprint;
    sshFingerprintDialog.returnValue = "";
    showModal(sshFingerprintDialog);
    return new Promise((resolve) => {
        sshFingerprintDialog.addEventListener("close", () => {
            resolve(sshFingerprintDialog.returnValue === "trust");
        }, {once: true});
    });
}

function stopCodeCountdown(): void {
    window.clearInterval(codeExpiryTimer);
    codeExpiryTimer = 0;
}

function startCodeCountdown(value: string): void {
    stopCodeCountdown();
    const expiry = Date.parse(value);
    if (!Number.isFinite(expiry)) {
        byID("sendCodeExpiry").textContent = "";
        return;
    }
    const tick = () => {
        const seconds = Math.max(0, Math.floor((expiry - Date.now()) / 1000));
        if (seconds <= 0) {
            byID("sendCodeState").textContent = "短码已过期";
            byID("sendCodeExpiry").textContent = "";
            byID<HTMLButtonElement>("copySendCode").disabled = true;
            stopCodeCountdown();
            return;
        }
        byID("sendCodeExpiry").textContent = `${Math.floor(seconds / 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")} 后失效`;
    };
    tick();
    codeExpiryTimer = window.setInterval(tick, 1000);
}

async function showSendCode(result: Record<string, any>): Promise<void> {
    const code = String(result.code || "");
    const uri = String(result.uri || `inkhole://receive?code=${encodeURIComponent(code)}`);
    byID("sendCode").textContent = code;
    byID("sendCodeResult").hidden = false;
    byID("sendCodeState").textContent = "等待接收方连接";
    byID<HTMLButtonElement>("copySendCode").disabled = false;
    byID<HTMLButtonElement>("cancelOneTime").textContent = "取消发送";
    const canvas = byID<HTMLCanvasElement>("sendCodeQR");
    canvas.hidden = false;
    try {
        await QRCode.toCanvas(canvas, uri, {
            width: 176,
            margin: 1,
            color: {dark: "#07110f", light: "#edf5f2"},
        });
    } catch {
        canvas.hidden = true;
    }
    startCodeCountdown(String(result.expires_at || ""));
    showModal(sendCodeDialog);
}

async function createOneTime(folder: boolean): Promise<void> {
    if (sendSessionID) {
        showModal(sendCodeDialog);
        return;
    }
    try {
        const paths = folder ? [await Service.ChooseFolder()] : (await Service.ChooseFiles()) || [];
        if (!paths.length || !paths[0]) return;
        const result = (await Service.CreateOneTime(paths)) || {};
        sendSessionID = String(result.session_id || "");
        await showSendCode(result);
    } catch (error) {
        toast(errorMessage(error), true);
    }
}

async function cancelOneTime(): Promise<void> {
    const sessionID = sendSessionID;
    sendSessionID = "";
    stopCodeCountdown();
    closeModal(sendCodeDialog);
    if (sessionID) await Service.CancelTransportSession(sessionID);
}

function openReceiveCode(): void {
    byID("receiveCodeState").textContent = receiveSessionID ? "正在等待发送端" : "输入发送端显示的一次性短码";
    byID("offerResult").hidden = !pendingOfferID;
    showModal(receiveCodeDialog);
    window.setTimeout(() => byID<HTMLInputElement>("receiveCode").focus(), 0);
}

async function cancelOneTimeReceive(): Promise<void> {
    const offerID = pendingOfferID;
    const sessionID = receiveSessionID;
    pendingOfferID = "";
    receiveSessionID = "";
    closeModal(receiveCodeDialog);
    if (offerID) await Service.RejectOneTime(offerID);
    else if (sessionID) await Service.CancelTransportSession(sessionID);
}

function showUsageGuide(): void {
    showModal(usageGuide);
}

function closeUsageGuide(): void {
    localStorage.setItem(usageGuideSeenKey, "1");
    closeModal(usageGuide);
}

function offerSummary(summary: Record<string, any>): string {
    const count = Number(summary.item_count || 0);
    const size = formatBytes(Number(summary.total_bytes || 0));
    const names = Array.isArray(summary.names) ? summary.names.slice(0, 3).join("、") : "";
    return `${count} 项 · ${size}${names ? ` · ${names}` : ""}`;
}

function handleTransport(payload: Record<string, any>): void {
    const eventName = String(payload.event || "");
    const data = (payload.data || payload) as Record<string, any>;
    if (eventName === "wormhole.offer") {
        pendingOfferID = String(data.session_id || "");
        const summary = (data.summary || {}) as Record<string, any>;
        byID("offerTitle").textContent = String(summary.device_name || "未知设备");
        byID("offerSummary").textContent = offerSummary(summary);
        byID("offerResult").hidden = false;
        byID("receiveCodeState").textContent = "确认接收内容";
        showModal(receiveCodeDialog);
        toast("收到一次性传输请求");
    } else if (eventName === "wormhole.error") {
        toast(String(data.error || "一次性短码连接失败"), true);
        if (String(data.session_id || "") === sendSessionID) {
            sendSessionID = "";
            stopCodeCountdown();
            byID("sendCodeState").textContent = "短码会话已结束";
            byID("sendCodeExpiry").textContent = "";
            byID<HTMLButtonElement>("cancelOneTime").textContent = "关闭";
        }
        if (String(data.session_id || "") === receiveSessionID) {
            receiveSessionID = "";
            byID("receiveCodeState").textContent = String(data.error || "连接失败");
            byID<HTMLButtonElement>("joinOneTime").disabled = false;
        }
    } else if (eventName === "wormhole.ready" && data.role === "sender") {
        sendSessionID = "";
        stopCodeCountdown();
        byID("sendCodeState").textContent = "接收方已确认，正在发送";
        byID("sendCodeExpiry").textContent = "";
        window.setTimeout(() => closeModal(sendCodeDialog), 700);
    } else if (eventName === "wormhole.ready" && data.role === "receiver") {
        receiveSessionID = "";
        pendingOfferID = "";
        byID("offerResult").hidden = true;
        closeModal(receiveCodeDialog);
        toast("接收通道已建立");
    } else if (eventName === "ssh.connected") {
        byID("sshState").textContent = "已连接";
    } else if (eventName === "ssh.disconnected") {
        byID("sshState").textContent = "正在重连";
    } else if (eventName === "ssh.paired") {
        toast("SSH 设备配对成功");
        const session = readySettingsSession();
        if (session !== null) {
            void refreshCrossConfig(session, true).catch((error) => toast(errorMessage(error), true));
        }
    } else if (eventName.endsWith(".error") && data.error) {
        toast(String(data.error), true);
    }
}

settingsButton.addEventListener("click", () => void openSettings());
for (const tab of document.querySelectorAll<HTMLButtonElement>(".cross-tab")) {
    tab.addEventListener("click", () => {
        for (const other of document.querySelectorAll<HTMLButtonElement>(".cross-tab")) {
            other.setAttribute("aria-selected", String(other === tab));
        }
        for (const panel of document.querySelectorAll<HTMLElement>(".cross-panel")) {
            panel.hidden = panel.id !== tab.getAttribute("aria-controls");
        }
    });
}
byID("minimiseWindow").addEventListener("click", () => void AppWindow.Minimise());
byID("hideWindow").addEventListener("click", () => void AppWindow.Hide());
document.querySelector(".titlebar")!.addEventListener("dblclick", (event) => {
    if ((event.target as HTMLElement).closest("button")) return;
    void AppWindow.ToggleMaximise();
});
byID("closeSettings").addEventListener("click", closeSettings);
byID("cancelSettings").addEventListener("click", closeSettings);
byID("saveSettings").addEventListener("click", () => void saveSettings());
byID("addManualPeer").addEventListener("click", () => addManualPeerRow());
byID("sshKeyMode").addEventListener("change", updateSSHKeyFields);
byID("sshEnabled").addEventListener("change", updateSSHControls);
byID("sshPassphrase").addEventListener("input", () => {
    sshPassphraseDirty = true;
});
for (const id of ["sshHost", "sshPort"]) {
    byID(id).addEventListener("input", () => {
        byID<HTMLInputElement>("sshFingerprint").value = "";
        byID("sshCheckResult").textContent = "主机地址已更改，请重新检测指纹";
    });
}
byID("cfgAutoClassify").addEventListener("change", updateClassificationControls);
byID("cfgEncrypt").addEventListener("change", updateEncryptionControls);
for (const row of document.querySelectorAll<HTMLLabelElement>(".toggle-row")) {
    row.addEventListener("mousedown", (event) => event.preventDefault());
}

byID("chooseFiles").addEventListener("click", () => void run(async () => {
    const paths = (await Service.ChooseFiles()) || [];
    await sendPaths(paths);
}));
byID("hole").addEventListener("click", () => void run(async () => {
    const paths = (await Service.ChooseFiles()) || [];
    await sendPaths(paths);
}));

byID("chooseFolder").addEventListener("click", () => void run(async () => {
    const path = await Service.ChooseFolder();
    if (path) await sendPaths([path]);
}));
byID("cancelSend").addEventListener("click", () => void run(async () => {
    if (activeSendID) await Service.CancelSend(activeSendID);
}, "正在停止传输"));
byID("openInbox").addEventListener("click", () => void run(() => Service.OpenInbox()));
byID("clearRecent").addEventListener("click", () => void run(async () => {
    await Service.ClearRecent();
    renderRecent([]);
}, "接收记录已清空"));
byID("chooseInbox").addEventListener("click", () => {
    const session = readySettingsSession();
    if (session === null) return;
    void run(async () => {
        const path = await Service.ChooseInbox();
        if (path && isCurrentSettingsSession(session)) {
            byID<HTMLInputElement>("cfgInbox").value = path;
        }
    });
});
for (const button of document.querySelectorAll<HTMLButtonElement>("[data-choose-category]")) {
    button.addEventListener("click", () => {
        const session = readySettingsSession();
        const category = button.dataset.chooseCategory as InboxCategory;
        if (session === null || !inboxCategories.includes(category)) return;
        void run(async () => {
            const path = await Service.ChooseInboxCategory(
                category,
                categoryInput(category).value,
                byID<HTMLInputElement>("cfgInbox").value,
            );
            if (path && isCurrentSettingsSession(session)) categoryInput(category).value = path;
        });
    });
}
for (const button of document.querySelectorAll<HTMLButtonElement>("[data-reset-category]")) {
    button.addEventListener("click", () => {
        const category = button.dataset.resetCategory as InboxCategory;
        if (inboxCategories.includes(category)) categoryInput(category).value = "";
    });
}
byID("chooseSSHKey").addEventListener("click", () => {
    const session = readySettingsSession();
    if (session === null) return;
    void run(async () => {
        const paths = (await Service.ChooseFiles()) || [];
        if (paths[0] && isCurrentSettingsSession(session)) {
            byID<HTMLInputElement>("sshKeyPath").value = paths[0];
        }
    });
});

byID("createOneTime").addEventListener("click", () => showModal(oneTimeSourceDialog));
byID("chooseOneTimeFiles").addEventListener("click", () => {
    closeModal(oneTimeSourceDialog);
    void createOneTime(false);
});
byID("chooseOneTimeFolder").addEventListener("click", () => {
    closeModal(oneTimeSourceDialog);
    void createOneTime(true);
});
byID("closeOneTimeSource").addEventListener("click", () => closeModal(oneTimeSourceDialog));
oneTimeSourceDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeModal(oneTimeSourceDialog);
});
byID("openReceiveCode").addEventListener("click", openReceiveCode);
byID("copySendCode").addEventListener("click", () => void run(async () => {
    await Clipboard.SetText(byID("sendCode").textContent || "");
    byID("sendCodeState").textContent = "短码已复制";
}));
byID("cancelOneTime").addEventListener("click", () => void run(cancelOneTime));
byID("closeSendCode").addEventListener("click", () => void run(cancelOneTime));
sendCodeDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    void run(cancelOneTime);
});
byID("closeReceiveCode").addEventListener("click", () => void run(cancelOneTimeReceive));
receiveCodeDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    void run(cancelOneTimeReceive);
});
byID("joinOneTime").addEventListener("click", () => {
    const button = byID<HTMLButtonElement>("joinOneTime");
    button.disabled = true;
    byID("receiveCodeState").textContent = "正在验证短码并连接发送端";
    void run(async () => {
        try {
            const result = await Service.JoinOneTime(byID<HTMLInputElement>("receiveCode").value);
            receiveSessionID = String(result?.session_id || "");
            byID("receiveCodeState").textContent = "正在等待发送端";
        } finally {
            button.disabled = false;
        }
    });
});
byID("acceptOffer").addEventListener("click", () => void run(async () => {
    const offerID = pendingOfferID;
    if (offerID) await Service.AcceptOneTime(offerID);
    pendingOfferID = "";
    byID("offerResult").hidden = true;
    closeModal(receiveCodeDialog);
}, "已接受传输"));
byID("rejectOffer").addEventListener("click", () => void run(cancelOneTimeReceive, "已拒绝传输"));

byID("checkSSH").addEventListener("click", () => {
    const session = readySettingsSession();
    if (session === null) return;
    const button = byID<HTMLButtonElement>("checkSSH");
    const input = collectSSHInput();
    button.disabled = true;
    void (async () => {
        try {
            const result = await Service.CheckSSH(input);
            if (!isCurrentSettingsSession(session)) return;
            const fingerprint = String(result?.fingerprint || "").trim();
            if (!fingerprint) throw new Error("SSH 服务未返回主机指纹");
            const trusted = await confirmSSHFingerprint(fingerprint);
            if (!trusted || !isCurrentSettingsSession(session)) return;
            byID<HTMLInputElement>("sshFingerprint").value = fingerprint;
            byID("sshCheckResult").textContent = `${fingerprint} · ${String(result?.server_version || "")}`;
            toast("SSH 主机指纹已确认并固定");
        } catch (error) {
            toast(errorMessage(error), true);
        } finally {
            button.disabled = false;
        }
    })();
});
byID("cancelSSHFingerprint").addEventListener("click", () => sshFingerprintDialog.close("cancel"));
byID("trustSSHFingerprint").addEventListener("click", () => sshFingerprintDialog.close("trust"));
byID("createSSHPairing").addEventListener("click", () => {
    const session = readySettingsSession();
    if (session === null) return;
    void run(async () => {
        const result = await Service.CreateSSHPairing();
        if (!isCurrentSettingsSession(session)) return;
        byID("sshPairCreatedCode").textContent = String(result?.code || "");
        byID("sshPairResult").hidden = false;
    }, "配对码已生成");
});
byID("copySSHPairCode").addEventListener("click", () => void run(() => Clipboard.SetText(byID("sshPairCreatedCode").textContent || ""), "配对码已复制"));
byID("joinSSHPairing").addEventListener("click", () => {
    const session = readySettingsSession();
    if (session === null) return;
    const code = byID<HTMLInputElement>("sshPairCode").value;
    void run(async () => {
        await Service.JoinSSHPairing(code);
        await refreshCrossConfig(session, true);
    }, "设备配对成功");
});

Events.On("update-progress", (event) => {
    const percent = Number((event.data as any)?.percent ?? 0);
    byID("updateState").textContent = `正在下载更新 ${percent}%`;
});

async function downloadUpdate(url: string): Promise<void> {
    if (!url) {
        await Service.OpenReleases();
        return;
    }
    byID("updateState").textContent = "正在下载更新 0%";
    try {
        const outcome = await (window as any).__TAURI__.core.invoke("frontend_call", {method: "DownloadUpdate", args: [url]});
        if (outcome?.restart) {
            byID("updateState").textContent = "安装程序已启动，墨洞即将退出";
        } else {
            byID("updateState").textContent = "已打开更新镜像，请将墨洞拖入「应用程序」完成更新";
        }
    } catch (error) {
        byID("updateState").textContent = errorMessage(error);
        toast(errorMessage(error), true);
        await Service.OpenReleases();
    }
}

byID("checkUpdate").addEventListener("click", () => {
    const session = readySettingsSession();
    if (session === null) return;
    byID("updateState").textContent = "正在检查更新";
    void run(async () => {
        const result = await Service.CheckForUpdate();
        if (!isCurrentSettingsSession(session)) return;
        const current = String(result?.current || "");
        const latest = String(result?.latest || "");
        byID("updateState").textContent = result?.available ? `发现新版本 v${latest}，当前 v${current}` : `已是最新版本 v${current}`;
        byID("openReleases").hidden = !result?.available;
        if (result?.available) {
            const dialog = (window as any).__TAURI__?.dialog;
            const confirmed = dialog?.ask
                ? await dialog.ask(`发现新版本 v${latest}（当前 v${current}），是否立即更新？`, {title: "墨洞更新", kind: "info", okLabel: "立即更新", cancelLabel: "稍后"})
                : false;
            if (confirmed) await downloadUpdate(String(result?.downloadUrl || ""));
        }
    }, "更新检查完成");
});
byID("openReleases").addEventListener("click", () => void run(() => Service.OpenReleases()));
byID("openRepository").addEventListener("click", () => void run(() => Service.OpenRepository()));
byID("openUsageGuide").addEventListener("click", showUsageGuide);
byID("closeUsageGuide").addEventListener("click", closeUsageGuide);
byID("confirmUsageGuide").addEventListener("click", closeUsageGuide);
usageGuide.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeUsageGuide();
});

Events.On("peers", (event) => {
    peers = Array.isArray(event.data) ? event.data as PeerView[] : [];
    renderPeers();
    updateTarget();
});
Events.On("selection", (event) => {
    selectedID = String((event.data as Record<string, any>)?.instanceId || "");
    renderPeers();
    updateTarget();
});
Events.On("status", (event) => setNodeState(String(event.data || "墨洞已开启")));
Events.On("progress", (event) => setProgress((event.data || {}) as Record<string, any>));
Events.On("transfer-finished", (event) => finishProgress((event.data || {}) as Record<string, any>));
Events.On("sent", (event) => {
    const data = (event.data || {}) as Record<string, any>;
    if (!data.ok) toast(`${String(data.name || "文件")}：${String(data.error || "发送失败")}`, true);
});
Events.On("received", () => void reloadRecent());
Events.On("recent", (event) => renderRecent(Array.isArray(event.data) ? event.data as string[] : []));
Events.On("transport", (event) => handleTransport((event.data || {}) as Record<string, any>));
Events.On("files-dropped", (event) => {
    const data = (event.data || {}) as Record<string, any>;
    if (data.window === "main" && Array.isArray(data.files)) void sendPaths(data.files as string[]);
});

async function initialise(): Promise<void> {
    setNodeState("正在启动共享传输核心", "waiting");
    try {
        await Service.Start();
        appConfig = (await Service.GetConfig()) || {};
        setNodeState(`墨洞已开启 · ${String(appConfig.peerName || "本机")}`);
        await Promise.all([reloadPeers(), reloadRecent()]);
        if (!localStorage.getItem(usageGuideSeenKey)) {
            window.setTimeout(showUsageGuide, 350);
        }
    } catch (error) {
        setNodeState(errorMessage(error), "error");
        toast(errorMessage(error), true);
    }
}

// Coming back to the window is the moment a stale device list is visible, so
// that is when discovery is asked to run hot: an announcement burst, back to
// back mDNS sweeps and a faster liveness cadence for a few seconds. The core
// keeps its own quiet cadence otherwise, so this costs nothing while idle.
function nudgeDiscovery(): void {
    void Service.RefreshDiscovery().catch(() => {});
}

window.addEventListener("focus", nudgeDiscovery);
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) nudgeDiscovery();
});
window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.querySelector("dialog[open]") && !settingsPage.hidden) {
        event.preventDefault();
        closeSettings();
    }
});

void initialise();
