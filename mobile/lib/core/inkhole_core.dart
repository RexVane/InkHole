import 'package:flutter/foundation.dart';
import 'dart:async';
import 'dart:convert';
import 'dart:ffi';
import 'dart:io';
import 'dart:isolate';

import 'package:ffi/ffi.dart';

typedef _AbiVersionNative = Uint32 Function();
typedef _AbiVersion = int Function();
typedef _CreateNative = Int32 Function(
  Pointer<Pointer<Void>> service,
  Pointer<Pointer<Char>> message,
);
typedef _Create = int Function(
  Pointer<Pointer<Void>> service,
  Pointer<Pointer<Char>> message,
);
typedef _CallNative = Int32 Function(
  Pointer<Void> service,
  Pointer<Uint8> request,
  UintPtr requestLength,
  Pointer<Pointer<Char>> json,
);
typedef _Call = int Function(
  Pointer<Void> service,
  Pointer<Uint8> request,
  int requestLength,
  Pointer<Pointer<Char>> json,
);
typedef _PollNative = Int32 Function(
  Pointer<Void> service,
  Int64 timeoutMs,
  Pointer<Pointer<Char>> event,
);
typedef _Poll = int Function(
  Pointer<Void> service,
  int timeoutMs,
  Pointer<Pointer<Char>> event,
);
typedef _CloseNative = Int32 Function(
  Pointer<Void> service,
  Pointer<Pointer<Char>> message,
);
typedef _Close = int Function(
  Pointer<Void> service,
  Pointer<Pointer<Char>> message,
);
typedef _DestroyNative = Int32 Function(
  Pointer<Void> service,
  Pointer<Pointer<Char>> message,
);
typedef _Destroy = int Function(
  Pointer<Void> service,
  Pointer<Pointer<Char>> message,
);
typedef _FreeNative = Void Function(Pointer<Char> value);
typedef _Free = void Function(Pointer<Char> value);

const int _statusOk = 0;
const int _statusNoEvent = 1;

class InkHoleCoreException implements Exception {
  const InkHoleCoreException(this.status, this.message);

  final int status;
  final String message;

  @override
  String toString() => 'InkHole core error ($status): $message';
}

class _NativeInkHole {
  _NativeInkHole(String? libraryPath)
      : _library = _openLibrary(libraryPath) {
    final version = _library
        .lookupFunction<_AbiVersionNative, _AbiVersion>('inkhole_ffi_abi_version');
    if (version() != 1) {
      throw InkHoleCoreException(version(), 'unsupported native ABI version');
    }
    _create = _library.lookupFunction<_CreateNative, _Create>(
      'inkhole_service_create',
    );
    _call = _library.lookupFunction<_CallNative, _Call>('inkhole_service_call');
    _poll = _library.lookupFunction<_PollNative, _Poll>(
      'inkhole_service_poll_event',
    );
    _close = _library.lookupFunction<_CloseNative, _Close>(
      'inkhole_service_close',
    );
    _destroy = _library.lookupFunction<_DestroyNative, _Destroy>(
      'inkhole_service_destroy',
    );
    _free = _library.lookupFunction<_FreeNative, _Free>('inkhole_string_free');
  }

  final DynamicLibrary _library;
  late final _Create _create;
  late final _Call _call;
  late final _Poll _poll;
  late final _Close _close;
  late final _Destroy _destroy;
  late final _Free _free;

  static DynamicLibrary _openLibrary(String? requestedPath) {
    if (requestedPath != null && requestedPath.trim().isNotEmpty) {
      return DynamicLibrary.open(requestedPath);
    }
    if (Platform.isIOS || Platform.isMacOS) {
      return DynamicLibrary.process();
    }
    if (Platform.isWindows) return DynamicLibrary.open('inkhole_ffi.dll');
    if (Platform.isLinux) return DynamicLibrary.open('libinkhole_ffi.so');
    if (Platform.isAndroid) return DynamicLibrary.open('libinkhole_ffi.so');
    throw UnsupportedError('unsupported platform for InkHole native core');
  }

  Pointer<Void> create() {
    final service = calloc<Pointer<Void>>();
    final message = calloc<Pointer<Char>>();
    try {
      final status = _create(service, message);
      if (status != _statusOk) {
        throw InkHoleCoreException(status, _take(message.value));
      }
      return service.value;
    } finally {
      calloc.free(service);
      calloc.free(message);
    }
  }

  String call(Pointer<Void> service, String request) {
    final bytes = utf8.encode(request);
    final input = calloc<Uint8>(bytes.isEmpty ? 1 : bytes.length);
    final output = calloc<Pointer<Char>>();
    try {
      input.asTypedList(bytes.length).setAll(0, bytes);
      final status = _call(service, input, bytes.length, output);
      final value = _take(output.value);
      if (status != _statusOk) {
        throw InkHoleCoreException(status, value);
      }
      return value;
    } finally {
      calloc.free(input);
      calloc.free(output);
    }
  }

  String? poll(Pointer<Void> service) {
    final output = calloc<Pointer<Char>>();
    try {
      final status = _poll(service, 0, output);
      if (status == _statusNoEvent) return null;
      final value = _take(output.value);
      if (status != _statusOk) {
        throw InkHoleCoreException(status, value);
      }
      return value;
    } finally {
      calloc.free(output);
    }
  }

  void close(Pointer<Void> service) {
    _callLifecycle(_close, service);
  }

  void destroy(Pointer<Void> service) {
    _callLifecycle(_destroy, service);
  }

  void _callLifecycle(
    int Function(Pointer<Void>, Pointer<Pointer<Char>>) function,
    Pointer<Void> service,
  ) {
    final output = calloc<Pointer<Char>>();
    try {
      final status = function(service, output);
      final value = _take(output.value);
      if (status != _statusOk) {
        throw InkHoleCoreException(status, value);
      }
    } finally {
      calloc.free(output);
    }
  }

  String _take(Pointer<Char> value) {
    if (value == nullptr) return '';
    try {
      return value.cast<Utf8>().toDartString();
    } finally {
      _free(value);
    }
  }
}

void _nativeWorker(List<dynamic> arguments) {
  final parent = arguments[0] as SendPort;
  final libraryPath = arguments[1] as String?;
  final commands = ReceivePort();
  late final _NativeInkHole native;
  late final Pointer<Void> service;
  Timer? pollTimer;

  try {
    native = _NativeInkHole(libraryPath);
    service = native.create();
    parent.send(<String, dynamic>{'type': 'ready', 'port': commands.sendPort});
  } catch (error) {
    parent.send(<String, dynamic>{
      'type': 'fatal',
      'error': error.toString(),
    });
    commands.close();
    return;
  }

  var pollFailed = false;

  void pollEvents() {
    if (pollFailed) return;
    for (var index = 0; index < 32; index++) {
      try {
        final raw = native.poll(service);
        if (raw == null) return;
        parent.send(<String, dynamic>{
          'type': 'event',
          'event': jsonDecode(raw),
        });
      } catch (error) {
        // Polling cannot recover on its own; stop the timer so the failure is
        // reported exactly once instead of every tick.
        pollFailed = true;
        pollTimer?.cancel();
        parent.send(<String, dynamic>{
          'type': 'fatal',
          'error': error.toString(),
        });
        return;
      }
    }
  }

  pollTimer = Timer.periodic(const Duration(milliseconds: 100), (_) {
    pollEvents();
  });

  commands.listen((dynamic raw) {
    final message = Map<String, dynamic>.from(raw as Map);
    final operation = message['operation'];
    if (operation == 'shutdown') {
      pollTimer?.cancel();
      try {
        native.close(service);
        native.destroy(service);
      } catch (_) {
        // The owner is shutting down; the native allocation is still released.
      }
      parent.send(<String, dynamic>{
        'type': 'shutdown',
        'id': message['id'],
      });
      commands.close();
      Isolate.exit();
    }

    if (operation != 'call') return;
    final id = message['id'];
    try {
      final request = jsonEncode(<String, dynamic>{
        'id': id.toString(),
        'method': message['method'],
        'params': message['params'] ?? <String, dynamic>{},
      });
      final rawResponse = native.call(service, request);
      final response = Map<String, dynamic>.from(jsonDecode(rawResponse) as Map);
      if (response['ok'] != true) {
        throw InkHoleCoreException(
          0,
          response['error']?.toString() ?? 'core request failed',
        );
      }
      parent.send(<String, dynamic>{
        'type': 'response',
        'id': id,
        'result': response['result'],
      });
    } catch (error) {
      parent.send(<String, dynamic>{
        'type': 'error',
        'id': id,
        'error': error.toString(),
      });
    }
  });
}

class InkHoleCore {
  /// 进程级单例:前台服务让进程常驻,Activity 重建时必须复用同一核心
  /// 实例,否则旧实例的会话继续占用端口导致 Address already in use。
  factory InkHoleCore({String? libraryPath}) =>
      _instance ??= InkHoleCore.internal(libraryPath: libraryPath);

  @visibleForTesting
  InkHoleCore.internal({this.libraryPath});

  static InkHoleCore? _instance;

  final String? libraryPath;
  final StreamController<Map<String, dynamic>> _events =
      StreamController<Map<String, dynamic>>.broadcast();
  final Map<Object, Completer<dynamic>> _pending = <Object, Completer<dynamic>>{};
  Isolate? _isolate;
  ReceivePort? _receive;
  SendPort? _commands;
  Completer<void>? _ready;
  Future<void>? _starting;
  bool _shutdownTimedOut = false;
  bool _disposed = false;
  int _nextRequest = 1;

  Stream<Map<String, dynamic>> get events => _events.stream;

  /// 启动原生核心。失败时抛出异常，调用方可重试。
  ///
  /// worker 上报 fatal 时 [events] 会收到一条 `core.fatal` 事件，
  /// 且所有在途请求以错误结束——调用方必须监听该事件，否则用户看到的是
  /// 静默失灵(下一次 call() 会再 spawn 出第二个原生服务)。
  Future<void> start() {
    if (_disposed) {
      return Future<void>.error(StateError('InkHole core was disposed'));
    }
    if (_commands != null) return Future<void>.value();
    final inFlight = _starting;
    if (inFlight != null) return inFlight;
    final attempt = _spawn();
    _starting = attempt;
    return attempt.whenComplete(() {
      if (identical(_starting, attempt)) _starting = null;
    });
  }

  Future<void> _spawn() async {
    final receive = ReceivePort();
    _receive = receive;
    final ready = Completer<void>();
    _ready = ready;
    receive.listen(_handleMessage);
    try {
      _isolate = await Isolate.spawn<List<dynamic>>(
        _nativeWorker,
        <dynamic>[receive.sendPort, libraryPath],
        debugName: 'inkhole-rust-core',
      );
      await ready.future;
    } catch (_) {
      // Leave the core in a clean state so a later start() can retry.
      receive.close();
      _isolate?.kill(priority: Isolate.immediate);
      _isolate = null;
      _receive = null;
      _commands = null;
      _ready = null;
      rethrow;
    }
  }

  Future<Map<String, dynamic>> call(
    String method, [
    Map<String, dynamic> params = const <String, dynamic>{},
  ]) async {
    await start();
    final id = _nextRequest++;
    final completer = Completer<dynamic>();
    _pending[id] = completer;
    try {
      _commands!.send(<String, dynamic>{
        'operation': 'call',
        'id': id,
        'method': method,
        'params': params,
      });
    } catch (error) {
      // SendPort 已关(worker isolate 被外部杀掉):Completer 必须显式清理,
      // 否则永远留在 _pending 里导致 map 缓慢膨胀。
      _pending.remove(id);
      completer.completeError(
        StateError('InkHole core channel is closed: $error'),
      );
    }
    final result = await completer.future;
    if (result == null) return <String, dynamic>{};
    if (result is Map) return Map<String, dynamic>.from(result);
    throw StateError('InkHole method $method returned a non-object result');
  }

  Future<void> close() async {
    final commands = _commands;
    if (commands == null) return;
    var handedOff = false;
    final id = _nextRequest++;
    final completer = Completer<dynamic>();
    _pending[id] = completer;
    try {
      commands.send(<String, dynamic>{'operation': 'shutdown', 'id': id});
    } catch (_) {
      // SendPort 已关,直接走强制清理。
    }
    // worker 的命令处理是串行的:若正阻塞在 in-flight native 调用(如大文件
    // lan.send),shutdown 命令排队等待,completer 永不完成。
    //
    // 超时必须覆盖 Rust 侧的完整预算:FFI close 最长 10s(CLOSE_TIMEOUT)
    // 之后 destroy 还要 2s(RUNTIME_SHUTDOWN_TIMEOUT)。若这里只等 3s 就
    // 强杀 isolate,worker 会在 close() 中途被打断,destroy() 永远执行不到,
    // 整个 tokio runtime 与 QUIC/UDP 监听器随之泄漏——旧核心继续占着
    // 41300/41301 端口收文件。故给 13s(10s + 2s + 1s 余量)。
    _shutdownTimedOut = false;
    try {
      await completer.future.timeout(_shutdownBudget);
      handedOff = true;
    } on TimeoutException {
      _shutdownTimedOut = true;
    }
    _receive?.close();
    final isolate = _isolate;
    if (!handedOff) {
      // 只有真正超时才强杀:此时 worker 大概率卡在 native 调用里。
      isolate?.kill(priority: Isolate.immediate);
    } else {
      // 已收到 shutdown 回执,worker 正在自行 Isolate.exit();给一小段缓冲
      // 避免在 destroy() 刚跑完时多补一刀。
      isolate?.kill(priority: Isolate.beforeNextEvent);
    }
    _isolate = null;
    _receive = null;
    _commands = null;
    for (final pending in _pending.values) {
      if (!pending.isCompleted) {
        pending.completeError(StateError('InkHole core was closed'));
      }
    }
    _pending.clear();
  }

  /// 关停时是否因超时被迫强杀 isolate。
  ///
  /// 为 true 说明原生核心可能没走完 destroy，调用方应提示用户重启应用，
  /// 否则残留核心会继续占用监听端口。
  bool get shutdownTimedOut => _shutdownTimedOut;

  /// 与 Rust 侧预算对齐:CLOSE_TIMEOUT(10s) + RUNTIME_SHUTDOWN_TIMEOUT(2s) + 余量。
  static const Duration _shutdownBudget = Duration(seconds: 13);

  /// 关停并释放。默认**不**关闭事件流——本类是进程级单例，关闭事件流会让
  /// 后续所有监听者永久收不到事件(Activity 重建即触发)。
  ///
  /// [release] 为 true 时才彻底关闭并允许下次访问重建单例，仅供测试使用。
  Future<void> dispose({bool release = false}) async {
    await close();
    if (!release) return;
    _disposed = true;
    if (!_events.isClosed) await _events.close();
    if (identical(_instance, this)) _instance = null;
  }

  void _handleMessage(dynamic raw) {
    final message = Map<String, dynamic>.from(raw as Map);
    switch (message['type']) {
      case 'ready':
        _commands = message['port'] as SendPort;
        if (!(_ready?.isCompleted ?? true)) _ready!.complete();
      case 'event':
        final event = message['event'];
        if (event is Map) _emit(Map<String, dynamic>.from(event));
      case 'response':
      case 'shutdown':
        _complete(message['id'], message['result']);
      case 'error':
        _completeError(message['id'], StateError(message['error'].toString()));
      case 'fatal':
        final reason = message['error'].toString();
        final error = StateError(reason);
        if (!(_ready?.isCompleted ?? true)) _ready!.completeError(error);
        for (final pending in _pending.values) {
          if (!pending.isCompleted) pending.completeError(error);
        }
        _pending.clear();
        final ReceivePort? receive = _receive;
        _receive = null;
        _commands = null;
        _ready = null;
        _isolate?.kill(priority: Isolate.immediate);
        _isolate = null;
        receive?.close();
        _emit(<String, dynamic>{
          'event': 'core.fatal',
          'data': <String, dynamic>{'message': reason},
        });
    }
  }

  /// 向事件流投递一条事件。
  ///
  /// start() 与 close() 天然存在竞争:关闭流程里 worker 可能正好推来最后
  /// 一批事件，若此时事件流已关闭，`add` 会抛 StateError。事件是尽力而为的
  /// 通道，投递失败直接丢弃即可。
  void _emit(Map<String, dynamic> event) {
    if (_events.isClosed) return;
    try {
      _events.add(event);
    } on StateError {
      // 事件流在写入瞬间被关闭,丢弃这条事件。
    }
  }

  void _complete(Object? id, dynamic value) {
    final completer = _pending.remove(id);
    if (completer != null && !completer.isCompleted) completer.complete(value);
  }

  void _completeError(Object? id, Object error) {
    final completer = _pending.remove(id);
    if (completer != null && !completer.isCompleted) completer.completeError(error);
  }
}
