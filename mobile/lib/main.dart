import 'package:flutter/material.dart';

import 'home_page.dart';
import 'theme.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const InkHoleApp());
}

class InkHoleApp extends StatelessWidget {
  const InkHoleApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: '墨洞',
      debugShowCheckedModeBanner: false,
      theme: buildInkHoleTheme(),
      home: const HomePage(),
    );
  }
}
