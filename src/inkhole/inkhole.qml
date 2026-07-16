import QtQuick
import QtQuick.Window

// 墨洞桌宠：墨黑核心 + 青色视界光晕，双层吸积弧缓慢反向旋转，
// 传输时外圈亮起青色进度环。小图标大小，无边框透明置顶可拖动。
Window {
    id: win
    // 尺寸由 Python 按屏幕自适应传入(petSizePx，约系统图标基准的 1.5 倍)；未注入时回退 140
    property int petSize: (typeof petSizePx !== 'undefined') ? petSizePx : 140
    width: win.petSize
    height: win.petSize
    visible: true
    color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
    // 初始位置：屏幕右上角偏下一点,贴右边缘(virtualX = 所在屏在虚拟桌面的原点)
    x: Screen.virtualX + Screen.width - win.petSize
    y: Screen.virtualY + Math.round(win.petSize * 0.8)

    property real scaleF: 1.0          // 整体缩放(发送/接收时放大)
    property string hint: ""           // 临时提示文字(2.2s 后消失)
    property string persistentHint: "" // 持续状态(错误信息等，始终显示)

    // ---- 传输进度环 ----
    property real transferPct: -1      // -1=无传输; 0-100 显示进度环
    property string transferKind: ""   // "send"=发送 / "recv"=接收

    // ---- 边缘吸附(悬浮球)状态 ----
    property int edge: -1             // 贴的是哪条边：-1未贴 0左 1右 2上 3下
    property bool collapsed: false    // 是否已收起(大部分滑出屏幕,只留窄条)
    property bool dragging: false     // 正在被拖动(拖动时关掉平滑,跟手)
    property int peek: Math.max(12, Math.round(win.petSize * 0.18))        // 收起后露出的窄条宽度
    property int snapThreshold: Math.max(30, Math.round(win.petSize * 0.7)) // 松手时离边缘多近才吸附

    // 收起/探出时窗口位置平滑滑动(拖动中禁用以保证跟手)
    Behavior on x { enabled: !win.dragging; NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
    Behavior on y { enabled: !win.dragging; NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }

    // 松手时判断是否靠近某条屏幕边缘；是则记下该边并收起,否则恢复自由浮动。
    // 多屏关键:win.x/y 是全部屏幕拼成的虚拟桌面全局坐标,而 Screen.width/height
    // 只是"当前所在屏"的尺寸——必须先减 Screen.virtualX/virtualY 换算成屏内
    // 坐标再算边距,否则副屏上贴边判定/收起位置全部错乱(桌宠飞走/消失)。
    // Screen 附加属性随窗口移动自动切换为所在屏:拖到哪块屏就贴哪块屏的边。
    function decideSnap() {
        var sx = win.x - Screen.virtualX, sy = win.y - Screen.virtualY;
        var dl = sx, dr = Screen.width - (sx + win.petSize);
        var dt = sy, db = Screen.height - (sy + win.petSize);
        var m = Math.min(dl, dr, dt, db);
        if (m > win.snapThreshold) { win.edge = -1; win.collapsed = false; return; }
        if (m === dl) win.edge = 0; else if (m === dr) win.edge = 1;
        else if (m === dt) win.edge = 2; else win.edge = 3;
        win.collapse();
    }
    // 收起：沿贴边方向滑出屏幕,只在边缘留 peek 宽度(所在屏的边)
    function collapse() {
        if (win.edge < 0) return;
        win.collapsed = true;
        if (win.edge === 0) win.x = Screen.virtualX - (win.petSize - win.peek);
        else if (win.edge === 1) win.x = Screen.virtualX + Screen.width - win.peek;
        else if (win.edge === 2) win.y = Screen.virtualY - (win.petSize - win.peek);
        else if (win.edge === 3) win.y = Screen.virtualY + Screen.height - win.peek;
    }
    // 探出：滑回贴边处完整显示(收文件 / 重新拖动用)
    function expand() {
        if (win.edge < 0) return;
        win.collapsed = false;
        if (win.edge === 0) win.x = Screen.virtualX;
        else if (win.edge === 1) win.x = Screen.virtualX + Screen.width - win.petSize;
        else if (win.edge === 2) win.y = Screen.virtualY;
        else if (win.edge === 3) win.y = Screen.virtualY + Screen.height - win.petSize;
    }
    // 探出后无人理睬则自动收回(鼠标不在其上、且未在拖动、且没在传文件)
    Timer {
        id: autoHide
        interval: 800
        onTriggered: if (win.edge >= 0 && !win.collapsed && !dragArea.containsMouse
                         && win.transferPct < 0) win.collapse()
    }

    // 发送/接收时的放大-回弹动画
    SequentialAnimation {
        id: pulse
        NumberAnimation { target: win; property: "scaleF"; to: 1.5; duration: 220; easing.type: Easing.OutQuad }
        NumberAnimation { target: win; property: "scaleF"; to: 1.0; duration: 360; easing.type: Easing.OutBack }
    }

    // ===== 玻璃碎片系统:发送时碎成青玻璃片卷入墨洞;接收时碎片飞拢拼合 =====
    property int shardCols: 4
    property int shardRows: 4
    property real fileIconSize: Math.round(win.width * 0.30)   // 拼合后"文件"的边长
    property real shardProgress: 0.0      // 0=完整拼合, 1=完全碎裂发送(被动画驱动)
    property bool shardEmit: false        // false=发送方向, true=接收方向

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
                property real homeX: col * (shardField.width / win.shardCols)
                property real homeY: row * (shardField.height / win.shardRows)
                property real ang: (index * 137.5) * Math.PI / 180.0
                property real spin: ((index % 3) - 1) * 540
                property real fling: shardField.width * (0.6 + (index % 5) * 0.12)

                width: shardField.width / win.shardCols - 1
                height: shardField.height / win.shardRows - 1
                radius: 1
                antialiasing: true
                // 青玻璃质感:半透明青白 + 细边
                color: Qt.rgba(0.55, 0.95, 0.85, 0.40)
                border.color: Qt.rgba(0.72, 1.0, 0.94, 0.62)
                border.width: 1

                property real p: win.shardProgress
                property real cx: shardField.width / 2 - width / 2
                property real cy: shardField.height / 2 - height / 2
                property real swirl: Math.sin(p * Math.PI) * width * 0.9
                x: homeX + (cx - homeX) * p + Math.cos(ang) * swirl
                y: homeY + (cy - homeY) * p + Math.sin(ang) * swirl
                rotation: spin * p
                scale: 1.0 - 0.95 * p
                opacity: 1.0 - 0.9 * p
            }
        }
    }

    // 发送:碎裂 0 -> 1(完整文件被撕碎卷入墨洞)
    SequentialAnimation {
        id: shatterAbsorb
        ScriptAction { script: { win.shardEmit = false; shardField.visible = true } }
        NumberAnimation { target: win; property: "shardProgress"; from: 0; to: 1; duration: 620; easing.type: Easing.InCubic }
        ScriptAction { script: shardField.visible = false }
    }
    // 接收:拼合 1 -> 0(碎片从墨洞飞回拼成完整文件),停顿后淡出
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

    // 收到对端文件时:若缩在边上,先探出,等滑回完整再播接收动画(否则动画在屏幕外看不见)
    Timer {
        id: emitDelay
        interval: 300
        onTriggered: { win.playEmit(); if (win.edge >= 0) autoHide.restart() }
    }
    function emitWhenVisible() {
        if (win.collapsed) { win.expand(); emitDelay.restart(); }
        else { win.playEmit(); if (win.edge >= 0) autoHide.restart(); }
    }


    Item {
        anchors.fill: parent
        transform: Scale {
            origin.x: win.width / 2; origin.y: win.height / 2
            xScale: win.scaleF; yScale: win.scaleF
        }

        // 墨洞主体：墨黑核心 -> 暗青过渡 -> 青色视界光晕 -> 透明
        Canvas {
            id: holeCanvas
            anchors.fill: parent
            onPaint: {
                var ctx = getContext("2d");
                var w = width, h = height;
                var cx = w / 2, cy = h / 2;
                ctx.clearRect(0, 0, w, h);
                var R = Math.min(w, h) * 0.48;

                var base = ctx.createRadialGradient(cx, cy, 0, cx, cy, R);
                base.addColorStop(0.00, "rgba(0,0,0,1.0)");
                base.addColorStop(0.42, "rgba(2,8,7,1.0)");          // 深墨视界
                base.addColorStop(0.60, "rgba(10,42,37,0.85)");      // 暗青过渡带
                base.addColorStop(0.76, "rgba(88,230,200,0.42)");    // 青色光晕
                base.addColorStop(0.90, "rgba(30,80,70,0.16)");
                base.addColorStop(1.00, "rgba(88,230,200,0.0)");     // 淡出到透明
                ctx.fillStyle = base;
                ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.fill();
            }
        }

        // 吸积弧·内层：两段青弧顺时针缓转(不对称才看得出旋转)
        Item {
            id: disk1
            anchors.fill: parent
            Canvas {
                anchors.fill: parent
                onPaint: {
                    var ctx = getContext("2d");
                    var w = width, h = height, cx = w / 2, cy = h / 2;
                    ctx.clearRect(0, 0, w, h);
                    var r = Math.min(w, h) * 0.335;
                    ctx.lineCap = "round";
                    ctx.lineWidth = Math.max(1.5, w * 0.020);
                    ctx.strokeStyle = "rgba(120,235,205,0.30)";
                    ctx.beginPath(); ctx.arc(cx, cy, r, 0.3, 2.1); ctx.stroke();
                    ctx.lineWidth = Math.max(1, w * 0.012);
                    ctx.strokeStyle = "rgba(120,235,205,0.16)";
                    ctx.beginPath(); ctx.arc(cx, cy, r * 0.88, 3.4, 4.6); ctx.stroke();
                }
            }
            RotationAnimation on rotation {
                from: 0; to: 360; duration: 46000
                loops: Animation.Infinite; running: win.visible
            }
        }
        // 吸积弧·外层：一段更淡的弧逆时针更慢
        Item {
            id: disk2
            anchors.fill: parent
            Canvas {
                anchors.fill: parent
                onPaint: {
                    var ctx = getContext("2d");
                    var w = width, h = height, cx = w / 2, cy = h / 2;
                    ctx.clearRect(0, 0, w, h);
                    var r = Math.min(w, h) * 0.415;
                    ctx.lineCap = "round";
                    ctx.lineWidth = Math.max(1, w * 0.010);
                    ctx.strokeStyle = "rgba(140,240,215,0.13)";
                    ctx.beginPath(); ctx.arc(cx, cy, r, 1.1, 3.5); ctx.stroke();
                }
            }
            RotationAnimation on rotation {
                from: 360; to: 0; duration: 71000
                loops: Animation.Infinite; running: win.visible
            }
        }

        // 光晕呼吸：极缓的透明度起伏，让洞"活着"
        SequentialAnimation {
            running: win.visible
            loops: Animation.Infinite
            NumberAnimation { target: disk1; property: "opacity"; from: 1.0; to: 0.55; duration: 2600; easing.type: Easing.InOutSine }
            NumberAnimation { target: disk1; property: "opacity"; from: 0.55; to: 1.0; duration: 2600; easing.type: Easing.InOutSine }
        }

        // 传输进度环：青色圆弧，发送和接收都从顶部起
        Canvas {
            id: ring
            anchors.fill: parent
            visible: win.transferPct >= 0
            z: 6
            onPaint: {
                var ctx = getContext("2d");
                var w = width, h = height, cx = w / 2, cy = h / 2;
                ctx.clearRect(0, 0, w, h);
                if (win.transferPct < 0) return;
                var r = Math.min(w, h) * 0.455;
                var start = -Math.PI / 2;
                // 底环(暗)
                ctx.lineWidth = Math.max(2, w * 0.030);
                ctx.lineCap = "round";
                ctx.strokeStyle = "rgba(40,90,80,0.35)";
                ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
                // 进度(亮青)
                ctx.strokeStyle = "rgba(96,240,208,0.95)";
                ctx.beginPath();
                ctx.arc(cx, cy, r, start, start + Math.PI * 2 * (win.transferPct / 100));
                ctx.stroke();
            }
        }

        // 提示药丸：半透明墨底 + 细青边，替代裸描边文字
        Rectangle {
            id: hintPill
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.top: parent.top
            anchors.topMargin: 2
            width: hintText.implicitWidth + 14
            height: hintText.implicitHeight + 6
            radius: height / 2
            color: "#d90a1210"
            border.color: "#4058e6c8"
            border.width: 1
            visible: hintText.text.length > 0
            z: 7
            Text {
                id: hintText
                anchors.centerIn: parent
                text: win.hint.length > 0 ? win.hint : win.persistentHint
                color: "#d8fff8"
                font.pixelSize: Math.max(9, Math.round(win.width * 0.075))
            }
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
            win.decideSnap();
        }
        onClicked: function(m) {
            if (m.button === Qt.RightButton) bridge.showMenu();   // 右键弹菜单
        }
        onEntered: { if (win.collapsed) win.expand() }
        onExited: { if (win.edge >= 0 && !win.dragging) autoHide.restart() }
    }


    // 接收桌面拖来的文件 -> 发送动画 -> 入发送队列；拖文件靠近收起的窄条时自动探出
    DropArea {
        anchors.fill: parent
        onEntered: { if (win.collapsed) win.expand(); win.hint = "松手发送" }
        onExited: { win.hint = ""; if (win.edge >= 0 && !dragArea.containsMouse) autoHide.restart() }
        onDropped: function(drop) {
            win.hint = ""
            if (drop.hasUrls) {
                if (bridge.hasTarget()) {
                    for (var i = 0; i < drop.urls.length; i++)
                        bridge.dropFile(drop.urls[i].toString());
                    win.playAbsorb(drop.x, drop.y);
                } else {
                    win.hint = bridge.missingTargetMessage()
                }
            }
            if (win.edge >= 0) autoHide.restart()
        }
    }


    // 来自 Python 的事件 -> 提示/动画
    Connections {
        target: bridge
        function onAbsorb(name) { win.hint = "发送 " + name }
        function onEmit_out(name) {
            win.hint = "接收 " + name;
            win.emitWhenVisible();
        }
        function onStatus(s) { win.hint = s }
        function onPeersChanged() { win.persistentHint = bridge.peerStatus() }
        function onErrorState(msg) { win.persistentHint = msg }
        function onProgress(kind, pct) {
            win.transferKind = kind;
            win.transferPct = pct;
            ring.requestPaint();
            if (win.collapsed) win.expand();          // 传输中探出来给用户看进度
            if (pct >= 100) ringHide.restart();
        }
    }

    // 传完后进度环稍作停留再消失
    Timer {
        id: ringHide
        interval: 900
        onTriggered: { win.transferPct = -1; if (win.edge >= 0) autoHide.restart() }
    }

    // hint 非空时倒计时清除；每次内容变化都重置倒计时——
    // 连续刷新的进度提示(↑ 文件 45%)不会中途被清掉闪烁
    Timer { id: hintClear; interval: 2200; onTriggered: win.hint = "" }
    onHintChanged: { if (win.hint.length > 0) hintClear.restart(); else hintClear.stop() }

    // 启动后初始化持续状态
    Timer {
        interval: 300; running: true; repeat: false
        onTriggered: win.persistentHint = bridge.peerStatus()
    }

    // 启动后稍候自动吸附到右边缘并收起(留窄条),不占地方
    Timer {
        id: startupSnap
        interval: 700; running: true; repeat: false
        onTriggered: { win.edge = 1; win.collapse(); }
    }
}
