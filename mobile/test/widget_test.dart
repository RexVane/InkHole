import 'package:flutter_test/flutter_test.dart';
import 'package:inkhole_mobile/main.dart';

void main() {
  testWidgets('renders the unchanged InkHole transfer surface', (tester) async {
    await tester.pumpWidget(const InkHoleApp());
    expect(find.text('墨洞'), findsOneWidget);
    expect(find.text('已接收'), findsOneWidget);
  });
}
