import 'package:flutter/material.dart';

// ==================== 墨洞 Cyber-Zen 赛博禅意色板 ====================
// 深度对齐 Tailwind 配置与设计稿：Void 深渊背景 + Jade 翡翠青光

/// 深渊墨黑底色系
const Color void950 = Color(0xFF040708);
const Color bgAbyss = Color(0xFF050909);
const Color surfaceLowest = Color(0xFF080F10);
const Color surfaceContainer = Color(0xFF0D1516);
const Color surfaceLow = Color(0xFF151D1E);
const Color surfaceHigh = Color(0xFF121D1E);
const Color surfaceActive = Color(0xFF11201D);
const Color surfaceBorder = Color(0xFF172623);
const Color surfaceBorderSubtle = Color(0x33172623);

/// 翡翠青玉强调色系 (Jade & Phosphor Cyan)
const Color jade200 = Color(0xFF8BFFD3);
const Color jade300 = Color(0xFF7EEDC6);
const Color jade400 = Color(0xFF52E5B5);
const Color jade500 = Color(0xFF2DD4BF);
const Color jade600 = Color(0xFF0D9488);
const Color jade900 = Color(0xFF042F2E);

/// 辅助强调色
const Color secondaryTeal = Color(0xFF44E2CD);
const Color badgeRoute = Color(0xFF83E8D3);
const Color badgeRouteBorder = Color(0x4D52E5B5);
const Color borderLuminescent = Color(0x2952E5B5);

/// 文字色彩梯度
const Color textPrimary = Color(0xFFE8F3EE);
const Color textMuted = Color(0xFF8CA39E);
const Color textDim = Color(0xFF4A605D);

/// 告警与危险
const Color dangerCoral = Color(0xFFF08A7C);
const Color dangerContainer = Color(0xFF3D1612);

/// 向后兼容旧色板命名映射
const Color inkBgDark = bgAbyss;
const Color inkBgCard = surfaceContainer;
const Color inkBgCardActive = surfaceActive;
const Color inkTeal = jade400;
const Color inkTealSoft = jade300;
const Color inkTealDim = jade600;
const Color inkTextPrimary = textPrimary;
const Color inkTextSecondary = textMuted;
const Color inkTextDim = textDim;
const Color inkBorder = surfaceBorder;
const Color inkDanger = dangerCoral;
const Color inkRouteBadgeText = badgeRoute;
const Color inkRouteBadgeBorder = badgeRouteBorder;

/// 构建 Cyber-Zen 主题
ThemeData buildInkHoleTheme() {
  const ColorScheme scheme = ColorScheme.dark(
    primary: jade400,
    onPrimary: bgAbyss,
    secondary: secondaryTeal,
    onSecondary: bgAbyss,
    surface: surfaceContainer,
    onSurface: textPrimary,
    error: dangerCoral,
    onError: bgAbyss,
  );

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: bgAbyss,
    canvasColor: bgAbyss,
    dividerColor: surfaceBorder,
    cardColor: surfaceContainer,
    iconTheme: const IconThemeData(color: textMuted),
    fontFamilyFallback: const <String>[
      'PingFang SC',
      'Heiti SC',
      'Microsoft YaHei',
      'sans-serif',
    ],
    appBarTheme: const AppBarTheme(
      backgroundColor: Colors.transparent,
      elevation: 0,
      centerTitle: false,
      titleTextStyle: TextStyle(
        color: textPrimary,
        fontSize: 16,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.5,
      ),
      iconTheme: IconThemeData(color: textMuted),
    ),
    inputDecorationTheme: InputDecorationTheme(
      isDense: true,
      filled: true,
      fillColor: surfaceActive,
      labelStyle: const TextStyle(color: textMuted, fontSize: 13),
      floatingLabelStyle: const TextStyle(color: jade400, fontSize: 13),
      contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: surfaceBorder),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: surfaceBorder),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(10),
        borderSide: const BorderSide(color: jade400, width: 1.5),
      ),
    ),
  );
}
