import QtQuick
import QtQuick.Window

// 虫洞桌宠：黑洞吞噬感 + 乳白色吸积盘，小图标大小，无边框透明置顶可拖动。
Window {
    id: win
    // 尺寸由 Python 按屏幕自适应传入(petSizePx，约系统图标基准的 1.5 倍)；未注入时回退 140
    property int petSize: (typeof petSizePx !== 'undefined') ? petSizePx : 140
    width: win.petSize
    height: win.petSize
    visible: true
    color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
    // 初始位置：屏幕右上角偏下一点,贴右边缘
    x: Screen.width - win.petSize
    y: Math.round(win.petSize * 0.8)

    property real scaleF: 1.0         // 整体缩放(吸入/喷出时放大)
    property string hint: ""          // 临时提示文字(2.2s 后消失)
    property string persistentHint: "" // 持续状态(设备搜索/发现数量，始终显示)

    // ---- 边缘吸附(悬浮球)状态 ----
    property int edge: -1             // 贴的是哪条边：-1未贴 0左 1右 2上 3下
    property bool collapsed: false    // 是否已收起(大部分滑出屏幕,只留窄条)
    property bool dragging: false     // 正在被拖动(拖动时关掉平滑,跟手)
    property int peek: Math.max(12, Math.round(win.petSize * 0.18))        // 收起后露出的窄条宽度
    property int snapThreshold: Math.max(30, Math.round(win.petSize * 0.7)) // 松手时离边缘多近才吸附

    // 收起/探出时窗口位置平滑滑动(拖动中禁用以保证跟手)
    Behavior on x { enabled: !win.dragging; NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
    Behavior on y { enabled: !win.dragging; NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }

    // 松手时判断是否靠近某条屏幕边缘；是则记下该边并收起,否则恢复自由浮动
    function decideSnap() {
        var dl = win.x, dr = Screen.width - (win.x + win.petSize);
        var dt = win.y, db = Screen.height - (win.y + win.petSize);
        var m = Math.min(dl, dr, dt, db);
        if (m > win.snapThreshold) { win.edge = -1; win.collapsed = false; return; }
        if (m === dl) win.edge = 0; else if (m === dr) win.edge = 1;
        else if (m === dt) win.edge = 2; else win.edge = 3;
        win.collapse();
    }
    // 收起：沿贴边方向滑出屏幕,只在边缘留 peek 宽度
    function collapse() {
        if (win.edge < 0) return;
        win.collapsed = true;
        if (win.edge === 0) win.x = -(win.petSize - win.peek);
        else if (win.edge === 1) win.x = Screen.width - win.peek;
        else if (win.edge === 2) win.y = -(win.petSize - win.peek);
        else if (win.edge === 3) win.y = Screen.height - win.peek;
    }
    // 探出：滑回贴边处完整显示(收文件 / 重新拖动用)
    function expand() {
        if (win.edge < 0) return;
        win.collapsed = false;
        if (win.edge === 0) win.x = 0;
        else if (win.edge === 1) win.x = Screen.width - win.petSize;
        else if (win.edge === 2) win.y = 0;
        else if (win.edge === 3) win.y = Screen.height - win.petSize;
    }
    // 探出后无人理睬则自动收回(鼠标不在其上、且未在拖动)
    Timer {
        id: autoHide
        interval: 800
        onTriggered: if (win.edge >= 0 && !win.collapsed && !dragArea.containsMouse) win.collapse()
    }


    // 吸入/喷出时的放大-回弹动画
    SequentialAnimation {
        id: pulse
        NumberAnimation { target: win; property: "scaleF"; to: 1.5; duration: 220; easing.type: Easing.OutQuad }
        NumberAnimation { target: win; property: "scaleF"; to: 1.0; duration: 360; easing.type: Easing.OutBack }
    }

    // ===== 玻璃碎片系统:文件吸入时碎成玻璃片卷入黑洞;吐出时碎片飞拢拼合 =====
    // 设计:一个文件 = N 块半透明玻璃碎片,平时拼成一个方形"文件";
    //   吸入 -> 每片各自旋转、缩小、螺旋卷向黑洞中心;
    //   吐出 -> 每片从中心四散弹出 -> 旋转放大飞回原位,拼回完整文件。
    property int shardCols: 4
    property int shardRows: 4
    property real fileIconSize: Math.round(win.width * 0.30)   // 拼合后"文件"的边长
    property real shardProgress: 0.0      // 0=完整拼合, 1=完全碎裂吸入(被动画驱动)
    property bool shardEmit: false        // false=吸入方向, true=吐出方向

    Item {
        id: shardField
        width: win.fileIconSize; height: win.fileIconSize
        x: win.width / 2 - width / 2
        y: win.height / 2 - height / 2
        visible: false
        z: 5

        Repeater {
            model: win.shardCols * win.shardRows
            Rectangle {
                id: shard
                property int col: index % win.shardCols
                property int row: Math.floor(index / win.shardCols)
                // 每片在"完整文件"里的原位
                property real homeX: col * (shardField.width / win.shardCols)
                property real homeY: row * (shardField.height / win.shardRows)
                // 每片被吸时的随机飞散方向(用 index 生成稳定的伪随机角度/距离)
                property real ang: (index * 137.5) * Math.PI / 180.0
                property real spin: ((index % 3) - 1) * 540      // 旋转量
                property real fling: shardField.width * (0.6 + (index % 5) * 0.12)

                width: shardField.width / win.shardCols - 1
                height: shardField.height / win.shardRows - 1
                radius: 1
                antialiasing: true
                // 玻璃质感:淡青白半透明 + 细边
                color: Qt.rgba(0.88, 0.93, 1.0, 0.42)
                border.color: Qt.rgba(1, 1, 1, 0.65)
                border.width: 1

                // p=进度(0完整拼合 -> 1碎裂卷入黑洞中心)。位置在"原位"与"中心"间插值,
                // 叠加按 ang 的横向散开,使吸入/吐出有螺旋飞散感而非直线收拢。
                property real p: win.shardProgress
                property real cx: shardField.width / 2 - width / 2     // 碎片场中心
                property real cy: shardField.height / 2 - height / 2
                property real swirl: Math.sin(p * Math.PI) * width * 0.9   // 中途最大散开,两端归零
                x: homeX + (cx - homeX) * p + Math.cos(ang) * swirl
                y: homeY + (cy - homeY) * p + Math.sin(ang) * swirl
                rotation: spin * p
                scale: 1.0 - 0.95 * p
                opacity: 1.0 - 0.9 * p
            }
        }
    }

    // 吸入:碎裂 0 -> 1(完整文件被撕碎卷入黑洞)
    SequentialAnimation {
        id: shatterAbsorb
        ScriptAction { script: { win.shardEmit = false; shardField.visible = true } }
        NumberAnimation { target: win; property: "shardProgress"; from: 0; to: 1; duration: 620; easing.type: Easing.InCubic }
        ScriptAction { script: shardField.visible = false }
    }
    // 吐出:拼合 1 -> 0(碎片从黑洞飞回拼成完整文件),停顿后淡出
    SequentialAnimation {
        id: shatterEmit
        ScriptAction { script: { win.shardEmit = true; win.shardProgress = 1; shardField.visible = true } }
        NumberAnimation { target: win; property: "shardProgress"; from: 1; to: 0; duration: 680; easing.type: Easing.OutBack }
        PauseAnimation { duration: 600 }
        NumberAnimation { target: shardField; property: "opacity"; from: 1; to: 0; duration: 320 }
        ScriptAction { script: { shardField.visible = false; shardField.opacity = 1 } }
    }

    function playAbsorb(fx, fy) {
        shardField.opacity = 1;
        shatterAbsorb.restart();
        pulse.restart();
    }
    function playEmit() {
        shardField.opacity = 1;
        shatterEmit.restart();
        pulse.restart();
    }

    // 收到对端文件时:若缩在边上,先探出,等滑回完整再播吐出动画(否则动画在屏幕外看不见)
    Timer {
        id: emitDelay
        interval: 300                 // 略大于探出动画时长(260ms),确保已完整露出
        onTriggered: { win.playEmit(); if (win.edge >= 0) autoHide.restart() }
    }
    function emitWhenVisible() {
        if (win.collapsed) { win.expand(); emitDelay.restart(); }   // 先探出,延后播
        else { win.playEmit(); if (win.edge >= 0) autoHide.restart(); }
    }


    Item {
        anchors.fill: parent
        transform: Scale {
            origin.x: win.width / 2; origin.y: win.height / 2
            xScale: win.scaleF; yScale: win.scaleF
        }

        // 黑洞主体：中间黑向外围平滑渐变
        Canvas {
            id: holeCanvas
            anchors.fill: parent
            onPaint: {
                var ctx = getContext("2d");
                var w = width, h = height;
                var cx = w / 2, cy = h / 2;
                ctx.clearRect(0, 0, w, h);
                var R = Math.min(w, h) * 0.48;   // 黑洞撑满挂件窗口(与窗口同尺寸感)

                // 中心纯黑 -> 暗过渡 -> 乳白光晕 -> 边缘完全透明
                var base = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
                base.addColorStop(0.00, "rgba(0,0,0,1.0)");
                base.addColorStop(0.42, "rgba(3,3,8,1.0)");          // 深黑视界
                base.addColorStop(0.60, "rgba(72,68,86,0.75)");      // 暗->亮过渡带
                base.addColorStop(0.78, "rgba(238,236,244,0.40)");   // 乳白光晕
                base.addColorStop(1.00, "rgba(255,255,255,0.0)");    // 淡出到透明
                ctx.fillStyle = base;
                ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();
            }
        }

        // 提示文字：临时提示优先，消失后显示持续状态(设备数/搜索中)
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: 4
            text: win.hint.length > 0 ? win.hint : win.persistentHint
            color: "white"
            font.pixelSize: Math.max(9, Math.round(win.width * 0.08))
            style: Text.Outline; styleColor: "#000000"
            visible: text.length > 0
        }
    }

    // 拖动窗口到桌面任意位置；松手时判断是否贴边收起；悬停在收起窄条上则探出
    MouseArea {
        id: dragArea
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        hoverEnabled: true
        property point press
        onPressed: function(m) { press = Qt.point(m.x, m.y); win.dragging = true }
        onPositionChanged: function(m) {
            if (m.buttons & Qt.LeftButton) {
                win.x += m.x - press.x;
                win.y += m.y - press.y;
            }
        }
        onReleased: function(m) {
            win.dragging = false;
            win.decideSnap();      // 松手:靠近边缘则吸附收起,否则自由浮动
        }
        onClicked: function(m) {
            if (m.button === Qt.RightButton) bridge.showMenu();   // 右键弹菜单(打开收件箱/暂停/状态/退出)
        }
        // 鼠标移到收起的窄条上 -> 探出恢复
        onEntered: { if (win.collapsed) win.expand() }
        // 鼠标离开且已贴边 -> 启动倒计时自动收回
        onExited: { if (win.edge >= 0 && !win.dragging) autoHide.restart() }
    }


    // 接收桌面拖来的文件 -> 吸入动画 -> 发送；拖文件靠近收起的窄条时自动探出
    DropArea {
        anchors.fill: parent
        onEntered: { if (win.collapsed) win.expand(); win.hint = "松手吸入" }
        onExited: { win.hint = ""; if (win.edge >= 0 && !dragArea.containsMouse) autoHide.restart() }
        onDropped: function(drop) {
            win.hint = ""
            if (drop.hasUrls) {
                if (bridge.hasTarget()) {
                    for (var i = 0; i < drop.urls.length; i++)
                        bridge.dropFile(drop.urls[i].toString());
                    win.playAbsorb(drop.x, drop.y);   // 有目标才播吸入动画
                } else {
                    win.hint = "右键选择目标设备"
                }
            }
            if (win.edge >= 0) autoHide.restart()
        }
    }


    // 来自 Python 的事件 -> 提示/动画(吸入动画已在松手时播放，这里只确认结果)
    Connections {
        target: bridge
        function onAbsorb(name) { win.hint = "吸入 " + name }
        function onEmit_out(name) {
            win.hint = "吐出 " + name;
            win.emitWhenVisible();      // 若缩在边上,先探出再播吐出动画,保证可见
        }
        function onStatus(s) { win.hint = s }
        function onPeersChanged() { win.persistentHint = bridge.peerStatus() }
        function onErrorState(msg) { win.persistentHint = msg }
    }

    // hint 非空时自动倒计时清除，临时提示消失后回到持续状态
    Timer { interval: 2200; running: win.hint.length > 0; onTriggered: win.hint = "" }

    // 启动后初始化持续状态(搜索设备中…/已发现N台设备)
    Timer {
        interval: 300; running: true; repeat: false
        onTriggered: win.persistentHint = bridge.peerStatus()
    }

    // 启动后稍候自动吸附到右边缘并收起(留窄条),不占地方
    Timer {
        id: startupSnap
        interval: 700; running: true; repeat: false
        onTriggered: { win.edge = 1; win.collapse(); }   // edge=1 右边
    }
}
