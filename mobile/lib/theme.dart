import 'package:flutter/material.dart';

// ==================== 墨洞色板 ====================
// 色值逐条对应旧版 Android(Compose) 的 InkHoleUI.kt，换技术不换视觉。

/// 墨黑底
const Color inkBgDark = Color(0xFF05070A);
const Color inkBgCard = Color(0xFF0D1416);
const Color inkBgCardActive = Color(0xFF11201D);

/// 视界青
const Color inkTeal = Color(0xFF58E6C8);
const Color inkTealSoft = Color(0xFF7FEFD8);
const Color inkTealDim = Color(0xFF1E4A42);

const Color inkTextPrimary = Color(0xFFE3F2EC);
const Color inkTextSecondary = Color(0xFF7FA098);
const Color inkTextDim = Color(0xFF48605A);
const Color inkBorder = Color(0xFF172623);

/// 旧版 MainActivity 里错误提示用的暖红
const Color inkDanger = Color(0xFFF08A7C);

// 连接方式标签，色值取自桌面端 style.css 的 .route-badge：
// 文字 #83e8d3，描边 rgba(90,216,192,.3)（0.3 alpha ≈ 0x4D）。
const Color inkRouteBadgeText = Color(0xFF83E8D3);
const Color inkRouteBadgeBorder = Color(0x4D5AD8C0);

/// 与旧版 InkHoleTheme 对齐的深色主题。
ThemeData buildInkHoleTheme() {
  const ColorScheme scheme = ColorScheme.dark(
    primary: inkTeal,
    onPrimary: inkBgDark,
    secondary: inkTeal,
    onSecondary: inkBgDark,
    surface: inkBgCard,
    onSurface: inkTextPrimary,
    error: inkDanger,
    onError: inkBgDark,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: inkBgDark,
    canvasColor: inkBgDark,
    dividerColor: inkBorder,
    iconTheme: const IconThemeData(color: inkTextSecondary),
    inputDecorationTheme: const InputDecorationTheme(
      isDense: true,
      labelStyle: TextStyle(color: inkTextSecondary, fontSize: 13),
      floatingLabelStyle: TextStyle(color: inkTeal, fontSize: 13),
      border: OutlineInputBorder(),
      enabledBorder: OutlineInputBorder(
        borderSide: BorderSide(color: inkBorder),
      ),
      focusedBorder: OutlineInputBorder(
        borderSide: BorderSide(color: inkTeal),
      ),
    ),
  );
}
