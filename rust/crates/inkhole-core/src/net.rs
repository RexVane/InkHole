//! 主机名拨号工具:自实现 Happy Eyeballs(RFC 8305 简化版)加 DNS 兜底。
//!
//! 两个实测踩过的坑(2026-08,见调试记录):
//! 1. 系统 getaddrinfo 常把 AAAA 排在最前(RFC 6724),而国内网络的国际 IPv6
//!    普遍是黑洞:`TcpStream::connect` 顺序拨号会在坏 v6 上耗尽整个超时,
//!    永远轮不到能通的 v4。这里 IPv4 优先、300ms 错峰并行竞速。
//! 2. 系统 DNS 对部分国外域名会整体卡死(getaddrinfo 6 秒无响应而裸 IP 秒连)。
//!    系统解析限短超时,失败后直查公共 DNS(阿里/腾讯/谷歌 UDP:53)。
//!    DNS 结果不承担安全职责:上层有证书指纹固定与 PAKE。

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use tokio::{
    net::{TcpStream, UdpSocket},
    task::JoinSet,
};
use tokio_util::sync::CancellationToken;

use crate::{CoreError, Result};

/// 相邻两次拨号尝试之间的错峰间隔。
const ATTEMPT_STAGGER: Duration = Duration::from_millis(300);
/// 系统解析器的耐心上限;超过就切换公共 DNS 兜底。
const SYSTEM_RESOLVE_TIMEOUT: Duration = Duration::from_millis(2_500);
/// 兜底公共 DNS(国内可直连且未见污染;1.1.1.1 在部分网络被阻断,不列入)。
const FALLBACK_DNS_SERVERS: [SocketAddr; 3] = [
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(223, 5, 5, 5)), 53),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(119, 29, 29, 29)), 53),
    SocketAddr::new(IpAddr::V4(Ipv4Addr::new(8, 8, 8, 8)), 53),
];
const FALLBACK_QUERY_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_DNS_PACKET: usize = 2_048;

/// 解析 `host:port` 并以 IPv4 优先、错峰并行的方式建立 TCP 连接。
/// `host` 为 IP 字面量时直接拨号。整体截止时间由调用方的外层 timeout 控制。
pub(crate) async fn dial_host_port(
    host: &str,
    port: u16,
    cancellation: &CancellationToken,
) -> Result<TcpStream> {
    let host = host.trim().trim_matches(['[', ']']);
    if let Ok(address) = host.parse::<IpAddr>() {
        return connect_one(SocketAddr::new(address, port), cancellation).await;
    }
    let mut addresses = tokio::select! {
        _ = cancellation.cancelled() => return Err(CoreError::Cancelled),
        resolved = resolve(host, port) => resolved?,
    };
    if addresses.is_empty() {
        return Err(CoreError::Protocol(format!(
            "{host} did not resolve to any address"
        )));
    }
    // 稳定排序:v4 在前,保持解析器给出的族内顺序。
    addresses.sort_by_key(|address| u8::from(address.is_ipv6()));
    race_addresses(&addresses, cancellation).await
}

async fn resolve(host: &str, port: u16) -> Result<Vec<SocketAddr>> {
    if let Ok(Ok(addresses)) = tokio::time::timeout(
        SYSTEM_RESOLVE_TIMEOUT,
        tokio::net::lookup_host((host, port)),
    )
    .await
    {
        let addresses = addresses.collect::<Vec<_>>();
        if !addresses.is_empty() {
            return Ok(addresses);
        }
    }
    tracing::debug!(
        host,
        "system DNS unavailable; falling back to public resolvers"
    );
    fallback_resolve(host, port).await
}

/// 直查公共 DNS:对每个服务器并行发 A 与 AAAA 查询,汇总首批成功应答。
async fn fallback_resolve(host: &str, port: u16) -> Result<Vec<SocketAddr>> {
    let mut queries = JoinSet::new();
    for server in FALLBACK_DNS_SERVERS {
        for qtype in [1_u16, 28] {
            let host = host.to_owned();
            queries.spawn(async move {
                tokio::time::timeout(FALLBACK_QUERY_TIMEOUT, query_dns(server, &host, qtype))
                    .await
                    .map_err(|_| CoreError::Protocol(format!("DNS query to {server} timed out")))?
            });
        }
    }
    let mut v4 = Vec::new();
    let mut v6 = Vec::new();
    let mut last_error = None;
    while let Some(finished) = queries.join_next().await {
        match finished {
            Ok(Ok(answers)) => {
                for address in answers {
                    match address {
                        IpAddr::V4(_) if !v4.contains(&address) => v4.push(address),
                        IpAddr::V6(_) if !v6.contains(&address) => v6.push(address),
                        _ => {}
                    }
                }
                // 拿到 v4 应答就够拨号了,不等慢的服务器。
                if !v4.is_empty() {
                    break;
                }
            }
            Ok(Err(error)) => last_error = Some(error.to_string()),
            Err(error) => last_error = Some(format!("DNS task failed: {error}")),
        }
    }
    queries.abort_all();
    let addresses = v4
        .into_iter()
        .chain(v6)
        .map(|address| SocketAddr::new(address, port))
        .collect::<Vec<_>>();
    if addresses.is_empty() {
        return Err(CoreError::Protocol(format!(
            "failed to resolve {host}: {}",
            last_error.unwrap_or_else(|| "no DNS answer".into())
        )));
    }
    Ok(addresses)
}

async fn query_dns(server: SocketAddr, host: &str, qtype: u16) -> Result<Vec<IpAddr>> {
    let id: u16 = rand::random();
    let query = build_dns_query(id, host, qtype)?;
    let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0)).await?;
    socket.connect(server).await?;
    socket.send(&query).await?;
    let mut packet = vec![0_u8; MAX_DNS_PACKET];
    let size = socket.recv(&mut packet).await?;
    parse_dns_answers(&packet[..size], id, qtype)
}

fn build_dns_query(id: u16, host: &str, qtype: u16) -> Result<Vec<u8>> {
    let mut packet = Vec::with_capacity(32 + host.len());
    packet.extend_from_slice(&id.to_be_bytes());
    packet.extend_from_slice(&[0x01, 0x00]); // RD=1
    packet.extend_from_slice(&[0, 1, 0, 0, 0, 0, 0, 0]); // QD=1
    for label in host.trim_end_matches('.').split('.') {
        let bytes = label.as_bytes();
        if bytes.is_empty() || bytes.len() > 63 {
            return Err(CoreError::Protocol(format!("invalid DNS label in {host}")));
        }
        packet.push(bytes.len() as u8);
        packet.extend_from_slice(bytes);
    }
    packet.push(0);
    packet.extend_from_slice(&qtype.to_be_bytes());
    packet.extend_from_slice(&[0, 1]); // IN
    Ok(packet)
}

fn parse_dns_answers(packet: &[u8], id: u16, qtype: u16) -> Result<Vec<IpAddr>> {
    let malformed = || CoreError::Protocol("malformed DNS response".into());
    if packet.len() < 12 || packet[..2] != id.to_be_bytes() {
        return Err(malformed());
    }
    if packet[3] & 0x0f != 0 {
        return Err(CoreError::Protocol(format!(
            "DNS server returned rcode {}",
            packet[3] & 0x0f
        )));
    }
    let questions = u16::from_be_bytes([packet[4], packet[5]]) as usize;
    let answers = u16::from_be_bytes([packet[6], packet[7]]) as usize;
    let mut cursor = 12_usize;
    for _ in 0..questions {
        cursor = skip_dns_name(packet, cursor).ok_or_else(malformed)?;
        cursor = cursor
            .checked_add(4)
            .filter(|c| *c <= packet.len())
            .ok_or_else(malformed)?;
    }
    let mut collected = Vec::new();
    for _ in 0..answers {
        cursor = skip_dns_name(packet, cursor).ok_or_else(malformed)?;
        if cursor + 10 > packet.len() {
            return Err(malformed());
        }
        let answer_type = u16::from_be_bytes([packet[cursor], packet[cursor + 1]]);
        let rdlength = u16::from_be_bytes([packet[cursor + 8], packet[cursor + 9]]) as usize;
        cursor += 10;
        if cursor + rdlength > packet.len() {
            return Err(malformed());
        }
        let rdata = &packet[cursor..cursor + rdlength];
        cursor += rdlength;
        if answer_type != qtype {
            continue;
        }
        match (qtype, rdlength) {
            (1, 4) => {
                collected.push(IpAddr::V4(Ipv4Addr::new(
                    rdata[0], rdata[1], rdata[2], rdata[3],
                )));
            }
            (28, 16) => {
                let mut segments = [0_u8; 16];
                segments.copy_from_slice(rdata);
                collected.push(IpAddr::V6(Ipv6Addr::from(segments)));
            }
            _ => {}
        }
    }
    Ok(collected)
}

/// 跳过一个(可能压缩的)DNS 名称,返回其后的偏移。
fn skip_dns_name(packet: &[u8], mut cursor: usize) -> Option<usize> {
    loop {
        let length = *packet.get(cursor)?;
        if length & 0xc0 == 0xc0 {
            return Some(cursor + 2).filter(|c| *c <= packet.len());
        }
        if length == 0 {
            return Some(cursor + 1);
        }
        cursor = cursor.checked_add(1 + length as usize)?;
        if cursor > packet.len() {
            return None;
        }
    }
}

async fn connect_one(address: SocketAddr, cancellation: &CancellationToken) -> Result<TcpStream> {
    tokio::select! {
        _ = cancellation.cancelled() => Err(CoreError::Cancelled),
        connected = TcpStream::connect(address) => Ok(connected?),
    }
}

async fn race_addresses(
    addresses: &[SocketAddr],
    cancellation: &CancellationToken,
) -> Result<TcpStream> {
    let mut attempts: JoinSet<std::result::Result<TcpStream, std::io::Error>> = JoinSet::new();
    let mut last_error: Option<String> = None;
    let mut remaining = addresses.iter().copied();
    let mut started = 0_usize;

    loop {
        // 维持错峰节奏:上一个尝试未成功前,每 300ms 追加下一个候选地址。
        while started == 0 {
            match remaining.next() {
                Some(address) => {
                    attempts.spawn(async move { TcpStream::connect(address).await });
                    started += 1;
                }
                None if attempts.is_empty() => {
                    return Err(CoreError::Protocol(format!(
                        "all addresses failed: {}",
                        last_error.unwrap_or_else(|| "no attempt was made".into())
                    )));
                }
                None => break,
            }
        }
        tokio::select! {
            _ = cancellation.cancelled() => {
                attempts.abort_all();
                return Err(CoreError::Cancelled);
            }
            _ = tokio::time::sleep(ATTEMPT_STAGGER), if remaining.len() > 0 => {
                if let Some(address) = remaining.next() {
                    attempts.spawn(async move { TcpStream::connect(address).await });
                }
            }
            finished = attempts.join_next(), if !attempts.is_empty() => {
                match finished {
                    Some(Ok(Ok(stream))) => {
                        attempts.abort_all();
                        return Ok(stream);
                    }
                    Some(Ok(Err(error))) => {
                        last_error = Some(error.to_string());
                        started = started.saturating_sub(1);
                    }
                    Some(Err(error)) => {
                        last_error = Some(format!("dial task failed: {error}"));
                        started = started.saturating_sub(1);
                    }
                    None => {}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::{io::AsyncWriteExt, net::TcpListener};

    #[tokio::test]
    async fn prefers_ipv4_and_wins_despite_dead_candidates() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let good = listener.local_addr().unwrap();
        tokio::spawn(async move {
            if let Ok((mut stream, _)) = listener.accept().await {
                let _ = stream.write_all(b"ok").await;
            }
        });
        // 一个立即拒绝的坏地址(环回上无人监听的端口)排在前面也不阻塞成功。
        let refused = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let dead = refused.local_addr().unwrap();
        drop(refused);
        let addresses = vec![dead, good];
        let stream = race_addresses(&addresses, &CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(stream.peer_addr().unwrap(), good);
    }

    #[tokio::test]
    async fn sorts_ipv6_after_ipv4_and_dials_ip_literals_directly() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let good = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let _ = listener.accept().await;
        });
        let stream = dial_host_port("127.0.0.1", good.port(), &CancellationToken::new())
            .await
            .unwrap();
        assert_eq!(stream.peer_addr().unwrap(), good);

        let mut addresses = [
            "[::1]:9".parse::<SocketAddr>().unwrap(),
            "127.0.0.1:9".parse::<SocketAddr>().unwrap(),
        ];
        addresses.sort_by_key(|address| u8::from(address.is_ipv6()));
        assert!(addresses[0].is_ipv4());
    }

    #[tokio::test]
    async fn reports_the_last_error_when_every_address_fails() {
        let refused = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let dead = refused.local_addr().unwrap();
        drop(refused);
        let result = race_addresses(&[dead], &CancellationToken::new()).await;
        assert!(
            matches!(result, Err(CoreError::Protocol(message)) if message.contains("all addresses failed"))
        );
    }

    #[test]
    fn builds_and_parses_dns_round_trip() {
        let query = build_dns_query(0x1234, "relay.magic-wormhole.io", 1).unwrap();
        assert_eq!(&query[..2], &[0x12, 0x34]);
        assert_eq!(query[query.len() - 4..], [0, 1, 0, 1]);

        // 合成一个带压缩指针的应答:x.io A 1.2.3.4
        let mut response = Vec::new();
        response.extend_from_slice(&[0x12, 0x34]); // id
        response.extend_from_slice(&[0x81, 0x80]); // QR/RD/RA, rcode 0
        response.extend_from_slice(&[0, 1, 0, 1, 0, 0, 0, 0]);
        response.extend_from_slice(&[1, b'x', 2, b'i', b'o', 0, 0, 1, 0, 1]); // question
        response.extend_from_slice(&[0xc0, 0x0c]); // name pointer
        response.extend_from_slice(&[0, 1, 0, 1, 0, 0, 0, 60, 0, 4, 1, 2, 3, 4]);
        let answers = parse_dns_answers(&response, 0x1234, 1).unwrap();
        assert_eq!(answers, vec![IpAddr::V4(Ipv4Addr::new(1, 2, 3, 4))]);

        assert!(parse_dns_answers(&response, 0x9999, 1).is_err());
        let mut refused = response.clone();
        refused[3] = 0x85; // rcode 5
        assert!(parse_dns_answers(&refused, 0x1234, 1).is_err());
    }
}
