import 'package:flutter/material.dart';

import 'home_page.dart';

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
      theme: ThemeData(
        brightness: Brightness.dark,
        scaffoldBackgroundColor: const Color(0xff080e13),
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xff58e6c8),
          brightness: Brightness.dark,
        ),
        useMaterial3: true,
        fontFamily: 'sans',
      ),
      home: const HomePage(),
    );
  }
}
