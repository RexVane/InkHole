import {Clipboard, Events, Window as AppWindow} from "@wailsio/runtime";
import * as Service from "../bindings/github.com/rexvane/inkhole/desktop/inkholeservice.js";
import type {
    ManualPeerConfig,
    PeerView,
    SSHSettingsInput,
} from "../bindings/github.com/rexvane/inkhole/desktop/models.js";
import {InkHoleAnimation} from "./inkhole.js";

const byID = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const homePage = byID<HTMLElement>("homePage");
const settingsPage = byID<HTMLElement>("settings");
const settingsButton = byID<HTMLButtonElement>("openSettings");
const peerList = byID<HTMLUListElement>("peerList");
const receivedList = byID<HTMLUListElement>("receivedList");
const animation = new InkHoleAnimation(byID<HTMLCanvasElement>("hole"));

let peers: PeerView[] = [];
let selectedID = "";
let activeSendID = "";
let sendSessionID = "";
let receiveSessionID = "";
let pendingOfferID = "";
let appConfig: Record<string, any> = {};
let crossConfig: Record<string, any> = {};

animation.run();

function errorMessage(error: unknown): string {
    if (error instanceof Error) return error.message;
    return String(error || "操作失败");
}

function toast(message: string, error = false): void {
    const item = document.createElement("div");
    item.className = `toast${error ? " error" : ""}`;
    item.textContent = message;
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
    remove.addEventListener("click", () => {
        row.remove();
        showManualEmpty();
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

function updateSSHKeyFields(): void {
    const pasted = byID<HTMLSelectElement>("sshKeyMode").value === "paste";
    byID("sshFileFields").hidden = pasted;
    byID("sshPasteFields").hidden = !pasted;
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
        passphrase: byID<HTMLInputElement>("sshPassphrase").value,
        clearPassphrase: byID<HTMLInputElement>("sshClearPassphrase").checked,
        hostKeySHA256: byID<HTMLInputElement>("sshFingerprint").value.trim(),
    };
}

function renderSSHPeers(values: Record<string, any>[]): void {
    const list = byID<HTMLUListElement>("sshPeerList");
    list.replaceChildren();
    if (!values.length) {
        const empty = document.createElement("li");
        empty.className = "empty-state";
        empty.textContent = "暂无配对设备";
        list.append(empty);
        return;
    }
    for (const peer of values) {
        const item = document.createElement("li");
        item.className = "paired-item";
        const copy = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = String(peer.name || "未命名设备");
        const id = document.createElement("code");
        id.textContent = String(peer.instance_id || "");
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "移除";
        remove.addEventListener("click", () => void run(async () => {
            await Service.RemoveSSHPeer(String(peer.instance_id || ""));
            await refreshCrossConfig();
        }, "已移除配对设备"));
        copy.append(name, id);
        item.append(copy, remove);
        list.append(item);
    }
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
    byID<HTMLInputElement>("sshClearPassphrase").checked = false;
    byID<HTMLInputElement>("sshFingerprint").value = String(profile.host_key_sha256 || "");
    byID("sshState").textContent = ssh.connected ? "已连接" : ssh.enabled ? "正在连接" : "已关闭";
    byID("sshCheckResult").textContent = ssh.hasPassphrase ? "已保存私钥口令" : "";
    renderSSHPeers(Array.isArray(ssh.peers) ? ssh.peers : []);
    updateSSHKeyFields();
}

async function refreshCrossConfig(): Promise<void> {
    crossConfig = (await Service.CrossNetworkConfig()) || {};
    fillSSHConfig();
}

function showHomePage(): void {
    settingsPage.hidden = true;
    homePage.hidden = false;
    settingsButton.classList.remove("active");
    settingsButton.setAttribute("aria-expanded", "false");
}

function showSettingsPage(): void {
    homePage.hidden = true;
    settingsPage.hidden = false;
    settingsButton.classList.add("active");
    settingsButton.setAttribute("aria-expanded", "true");
    settingsPage.querySelector<HTMLElement>(".settings-scroll")?.scrollTo({top: 0});
}

async function openSettings(): Promise<void> {
    if (!settingsPage.hidden) return;
    try {
        const [config, cross, manual] = await Promise.all([
            Service.GetConfig(),
            Service.CrossNetworkConfig(),
            Service.ManualPeers(),
        ]);
        appConfig = config || {};
        crossConfig = cross || {};
        byID<HTMLInputElement>("cfgName").value = String(appConfig.peerName || "");
        byID<HTMLInputElement>("cfgInstance").value = String(appConfig.instanceId || "");
        byID<HTMLInputElement>("cfgInbox").value = String(appConfig.inbox || "");
        byID<HTMLInputElement>("cfgPort").value = String(appConfig.port || 0);
        byID<HTMLInputElement>("cfgSecret").value = "";
        byID<HTMLInputElement>("cfgClearSecret").checked = false;
        byID<HTMLInputElement>("cfgEncrypt").checked = appConfig.encryptionEnabled === true;
        byID<HTMLInputElement>("cfgShowPet").checked = appConfig.showPet !== false;
        byID("actualPort").textContent = appConfig.actualPort ? `当前端口 ${appConfig.actualPort}` : "启动后自动分配";
        byID("secretState").textContent = appConfig.encryptionEnabled
            ? "已启用端到端加密"
            : (appConfig.hasSecret ? "已保存口令，加密未启用" : "未设置端到端口令");
        byID("settingsVersion").textContent = `v${appConfig.version || ""}`;
        byID("updateState").textContent = `当前版本 v${appConfig.version || ""}`;
        byID("settingsMessage").textContent = String(appConfig.warning || "");
        const wormhole = crossConfig.wormhole || {};
        byID<HTMLInputElement>("wormholeRendezvous").value = String(wormhole.rendezvous_url || "");
        byID<HTMLInputElement>("wormholeRelay").value = String(wormhole.transit_relay || "");
        renderManualPeers(manual || []);
        fillSSHConfig();
        showSettingsPage();
    } catch (error) {
        toast(errorMessage(error), true);
    }
}

async function saveSettings(): Promise<void> {
    const save = byID<HTMLButtonElement>("saveSettings");
    save.disabled = true;
    byID("settingsMessage").textContent = "正在保存";
    try {
        await Service.SaveConfig(
            byID<HTMLInputElement>("cfgName").value,
            byID<HTMLInputElement>("cfgInbox").value,
            byID<HTMLInputElement>("cfgSecret").value,
            byID<HTMLInputElement>("cfgClearSecret").checked,
            Number(byID<HTMLInputElement>("cfgPort").value || 0),
            byID<HTMLInputElement>("cfgShowPet").checked,
            byID<HTMLInputElement>("cfgEncrypt").checked,
        );
        await Service.SaveManualPeers(collectManualPeers());
        await Service.SaveWormholeConfig(
            byID<HTMLInputElement>("wormholeRendezvous").value,
            byID<HTMLInputElement>("wormholeRelay").value,
        );
        await Service.SaveSSHConfig(collectSSHInput());
        showHomePage();
        toast("设置已保存");
        await Promise.all([reloadPeers(), reloadRecent()]);
    } catch (error) {
        byID("settingsMessage").textContent = errorMessage(error);
    } finally {
        save.disabled = false;
    }
}

async function createOneTime(folder: boolean): Promise<void> {
    try {
        const paths = folder ? [await Service.ChooseFolder()] : (await Service.ChooseFiles()) || [];
        if (!paths.length || !paths[0]) return;
        const result = await Service.CreateOneTime(paths);
        sendSessionID = String(result?.session_id || "");
        byID("sendCode").textContent = String(result?.code || "");
        byID("sendCodeResult").hidden = false;
        toast("一次性短码已生成");
    } catch (error) {
        toast(errorMessage(error), true);
    }
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
        toast("收到一次性传输请求");
    } else if (eventName === "wormhole.error") {
        toast(String(data.error || "一次性短码连接失败"), true);
        if (String(data.session_id || "") === sendSessionID) byID("sendCodeResult").hidden = true;
        if (String(data.session_id || "") === receiveSessionID) receiveSessionID = "";
    } else if (eventName === "wormhole.ready" && data.role === "receiver") {
        receiveSessionID = "";
        byID("offerResult").hidden = true;
        toast("接收通道已建立");
    } else if (eventName === "ssh.connected") {
        byID("sshState").textContent = "已连接";
    } else if (eventName === "ssh.disconnected") {
        byID("sshState").textContent = "正在重连";
    } else if (eventName === "ssh.paired") {
        toast("SSH 设备配对成功");
        if (!settingsPage.hidden) void refreshCrossConfig();
    } else if (eventName.endsWith(".error") && data.error) {
        toast(String(data.error), true);
    }
}

for (const button of document.querySelectorAll<HTMLButtonElement>("[data-cross-tab]")) {
    button.addEventListener("click", () => {
        const tab = button.dataset.crossTab;
        document.querySelectorAll("[data-cross-tab]").forEach((item) => item.classList.toggle("active", item === button));
        document.querySelectorAll<HTMLElement>("[data-cross-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.crossPanel === tab));
    });
}

settingsButton.setAttribute("aria-expanded", "false");
settingsButton.addEventListener("click", () => void openSettings());
byID("minimiseWindow").addEventListener("click", () => void AppWindow.Minimise());
byID("hideWindow").addEventListener("click", () => void AppWindow.Hide());
document.querySelector(".titlebar")!.addEventListener("dblclick", (event) => {
    if ((event.target as HTMLElement).closest("button")) return;
    void AppWindow.ToggleMaximise();
});
byID("closeSettings").addEventListener("click", showHomePage);
byID("cancelSettings").addEventListener("click", showHomePage);
byID("saveSettings").addEventListener("click", () => void saveSettings());
byID("addManualPeer").addEventListener("click", () => addManualPeerRow());
byID("sshKeyMode").addEventListener("change", updateSSHKeyFields);
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !settingsPage.hidden) showHomePage();
});

byID("chooseFiles").addEventListener("click", () => void run(async () => {
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
byID("chooseInbox").addEventListener("click", () => void run(async () => {
    const path = await Service.ChooseInbox();
    if (path) byID<HTMLInputElement>("cfgInbox").value = path;
}));
byID("chooseSSHKey").addEventListener("click", () => void run(async () => {
    const paths = (await Service.ChooseFiles()) || [];
    if (paths[0]) byID<HTMLInputElement>("sshKeyPath").value = paths[0];
}));

byID("createOneTime").addEventListener("click", () => void createOneTime(false));
byID("createOneTimeFolder").addEventListener("click", () => void createOneTime(true));
byID("copySendCode").addEventListener("click", () => void run(() => Clipboard.SetText(byID("sendCode").textContent || ""), "短码已复制"));
byID("cancelOneTime").addEventListener("click", () => void run(async () => {
    if (sendSessionID) await Service.CancelTransportSession(sendSessionID);
    sendSessionID = "";
    byID("sendCodeResult").hidden = true;
}, "一次性传输已取消"));
byID("joinOneTime").addEventListener("click", () => void run(async () => {
    const result = await Service.JoinOneTime(byID<HTMLInputElement>("receiveCode").value);
    receiveSessionID = String(result?.session_id || "");
    toast("正在等待发送端");
}));
byID("acceptOffer").addEventListener("click", () => void run(async () => {
    if (pendingOfferID) await Service.AcceptOneTime(pendingOfferID);
    byID("offerResult").hidden = true;
}, "已接受传输"));
byID("rejectOffer").addEventListener("click", () => void run(async () => {
    if (pendingOfferID) await Service.RejectOneTime(pendingOfferID);
    pendingOfferID = "";
    byID("offerResult").hidden = true;
}, "已拒绝传输"));

byID("checkSSH").addEventListener("click", () => void run(async () => {
    const result = await Service.CheckSSH(collectSSHInput());
    const fingerprint = String(result?.fingerprint || "");
    byID<HTMLInputElement>("sshFingerprint").value = fingerprint;
    byID("sshCheckResult").textContent = `${fingerprint} · ${String(result?.server_version || "")}`;
}, "SSH 登录与主机指纹验证通过"));
byID("createSSHPairing").addEventListener("click", () => void run(async () => {
    const result = await Service.CreateSSHPairing();
    byID("sshPairCreatedCode").textContent = String(result?.code || "");
    byID("sshPairResult").hidden = false;
}, "配对码已生成"));
byID("copySSHPairCode").addEventListener("click", () => void run(() => Clipboard.SetText(byID("sshPairCreatedCode").textContent || ""), "配对码已复制"));
byID("joinSSHPairing").addEventListener("click", () => void run(async () => {
    await Service.JoinSSHPairing(byID<HTMLInputElement>("sshPairCode").value);
    await refreshCrossConfig();
}, "设备配对成功"));

byID("checkUpdate").addEventListener("click", () => void run(async () => {
    byID("updateState").textContent = "正在检查更新";
    const result = await Service.CheckForUpdate();
    const current = String(result?.current || "");
    const latest = String(result?.latest || "");
    byID("updateState").textContent = result?.available ? `发现新版本 v${latest}，当前 v${current}` : `已是最新版本 v${current}`;
}, "更新检查完成"));
byID("openReleases").addEventListener("click", () => void run(() => Service.OpenReleases()));
byID("openRepository").addEventListener("click", () => void run(() => Service.OpenRepository()));

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

void initialise();
