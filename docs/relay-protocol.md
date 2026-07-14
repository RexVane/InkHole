# 墨洞 SSH 远程协议 v1

远程模式只依赖标准 SSH 的 SFTP、remote forwarding 和 direct-tcpip channel。
局域网模式不使用本协议。

## 会话与发现

每个客户端通过已固定主机密钥的 SSH 会话登录同一账户，并请求：

```text
remote bind = 127.0.0.1:0
```

OpenSSH 分配一个只在服务器回环地址可达的端口。客户端随后通过 SFTP 原子写入
`~/.cache/inkhole/peers/<32-hex-device-id>.json`：

```json
{"id":"...","mac":"...","name":"...","port":12345,"public_key":"...","v":1}
```

记录上限 8 KiB、名称上限 80 个字符，租约 75 秒。`mac` 是以下 transcript 的
HMAC-SHA256，使用 base64url 无填充编码：

```text
group_key = HMAC-SHA256(
  SHA256(normalized_private_key_text),
  "inkhole ssh relay registry v1"
)

registry_transcript = "inkhole-ssh-registry-v1\0"
  || LP16(device_id) || LP16(name) || port_BE16 || LP16(public_key)
```

私钥文本规范化规则是：UTF-8 BOM 可选、CRLF/CR 转为 LF、去掉首尾空白，然后补
一个 LF。设备必须使用同一份私钥文本才能验证彼此的登记。

## 数据通道

发送方在自己的 SSH transport 上创建 direct-tcpip channel，目标为
`127.0.0.1:<receiver-port>`。OpenSSH 将它接入接收方已登记的 remote forward。
随机端口不需要也不应暴露到公网。

通道首先发送经组密钥认证的握手：

```text
[4B "ISSH"] [4B JSON length BE] [JSON offer]
```

offer 包含版本、transfer UUID、发送/接收设备 ID 和发送方 P-256 公钥。HMAC
覆盖固定顺序的全部字段并绑定接收方；接收方拒绝错误目标、错误 HMAC、已见过的
transfer ID，以及与在线登记不一致的公钥。握手通过后返回明文 `0x01`，失败时
关闭通道。

## 端到端密钥

每个设备持久化一个 P-256 身份密钥。每次传输执行 ECDH，并通过 HKDF-SHA256
派生独立的 32-byte 密钥：

```text
transcript = "inkhole-relay-v1\0"
             || LP16(transfer_id) || LP16(sender_id) || LP16(receiver_id)
salt       = SHA256(transcript)
info       = "inkhole relay transfer key\0" || transcript
key        = HKDF-SHA256(ECDH_shared, salt, info, 32)
```

`LP16` 是 2-byte big-endian 长度加 UTF-8 字节。Python 与 Kotlin 共用固定向量
`tests/vectors/relay_crypto_v1.json`。

## 加密帧

SSH channel 上的每个密文帧带 4-byte big-endian 帧长，内部格式为：

```text
[1B version=1] [1B direction] [8B sequence BE] [AES-256-GCM ciphertext+tag]
nonce = "IRF" || direction || sequence_BE64
AAD   = "IRF1" || LP16(transfer_id) || LP16(sender_id) || LP16(receiver_id)
        || version || direction || sequence_BE64
```

方向 `0` 是文件流，方向 `1` 是 ACK。每个方向从序号 0 开始严格递增；篡改、重放、
乱序、错误方向或错误设备 transcript 都会失败。单帧明文最多 64 KiB。

## WHPP 与落盘

方向 0 的加密明文是完整 WHPP 字节流：

```text
[4B "WHPP"] [4B JSON length BE] [JSON header] [size B file data]
```

文件名也在端到端密文内。接收方验证 size、清洗文件名、检查磁盘空间，将内容写入
随机 `.part` 文件并 `fsync`，完成后原子改名。成功后方向 1 返回加密 `0x01`，
失败返回加密 `0x00`。协议不提供断点续传、离线暂存或服务器文件缓存。
