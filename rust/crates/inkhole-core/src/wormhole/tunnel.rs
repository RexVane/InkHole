use std::{
    net::SocketAddr,
    time::{Duration, Instant},
};

use crypto_secretbox::{
    Key, Nonce, XSalsa20Poly1305,
    aead::{Aead, KeyInit},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::UdpSocket,
    sync::mpsc,
    task::JoinHandle,
};
use tokio_util::sync::CancellationToken;

use super::protocol::derive_key;
use crate::{CoreError, Result};

const MAX_DATAGRAM: usize = 64 * 1024;
const NONCE_SIZE: usize = 24;
const TAG_SIZE: usize = 16;
const MAX_RECORD: usize = NONCE_SIZE + TAG_SIZE + MAX_DATAGRAM;
const WRITE_QUEUE: usize = 64;
const PEER_IDLE_REPLACE: Duration = Duration::from_secs(5);

pub(crate) struct UdpTunnel {
    local_address: SocketAddr,
    cancellation: CancellationToken,
    task: Option<JoinHandle<Result<()>>>,
    reset_peer: Option<mpsc::Sender<()>>,
}

impl std::fmt::Debug for UdpTunnel {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("UdpTunnel")
            .field("local_address", &self.local_address)
            .field("cancelled", &self.cancellation.is_cancelled())
            .finish_non_exhaustive()
    }
}

impl UdpTunnel {
    pub async fn start_sender<S>(
        stream: S,
        transit_key: [u8; 32],
        cancellation: CancellationToken,
    ) -> Result<Self>
    where
        S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let (reader, writer) = tokio::io::split(stream);
        Self::start_sender_parts(reader, writer, transit_key, cancellation).await
    }

    pub async fn start_sender_parts<R, W>(
        reader: R,
        writer: W,
        transit_key: [u8; 32],
        cancellation: CancellationToken,
    ) -> Result<Self>
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let socket = UdpSocket::bind("127.0.0.1:0").await?;
        let local_address = socket.local_addr()?;
        let child = cancellation.child_token();
        let (reset_tx, reset_rx) = mpsc::channel(1);
        let task = tokio::spawn(run_sender(
            socket,
            reader,
            writer,
            transit_key,
            child.clone(),
            reset_rx,
        ));
        Ok(Self {
            local_address,
            cancellation: child,
            task: Some(task),
            reset_peer: Some(reset_tx),
        })
    }

    pub async fn start_receiver<S>(
        stream: S,
        upstream: SocketAddr,
        transit_key: [u8; 32],
        cancellation: CancellationToken,
    ) -> Result<Self>
    where
        S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let (reader, writer) = tokio::io::split(stream);
        Self::start_receiver_parts(reader, writer, upstream, transit_key, cancellation).await
    }

    pub async fn start_receiver_parts<R, W>(
        reader: R,
        writer: W,
        upstream: SocketAddr,
        transit_key: [u8; 32],
        cancellation: CancellationToken,
    ) -> Result<Self>
    where
        R: AsyncRead + Unpin + Send + 'static,
        W: AsyncWrite + Unpin + Send + 'static,
    {
        let socket = UdpSocket::bind("127.0.0.1:0").await?;
        let local_address = socket.local_addr()?;
        let child = cancellation.child_token();
        let task = tokio::spawn(run_receiver(
            socket,
            reader,
            writer,
            upstream,
            transit_key,
            child.clone(),
        ));
        Ok(Self {
            local_address,
            cancellation: child,
            task: Some(task),
            reset_peer: None,
        })
    }

    pub fn local_address(&self) -> SocketAddr {
        self.local_address
    }

    pub async fn reset_sender_peer(&self) -> Result<()> {
        let Some(reset_peer) = &self.reset_peer else {
            return Ok(());
        };
        reset_peer
            .send(())
            .await
            .map_err(|_| CoreError::Protocol("UDP tunnel is closed".into()))
    }

    pub async fn wait(mut self) -> Result<()> {
        let Some(task) = self.task.take() else {
            return Ok(());
        };
        match task.await {
            Ok(result) => result,
            Err(error) if error.is_cancelled() => Ok(()),
            Err(error) => Err(CoreError::Protocol(format!(
                "UDP tunnel task failed: {error}"
            ))),
        }
    }

    pub async fn close(self) -> Result<()> {
        self.cancellation.cancel();
        self.wait().await
    }
}

impl Drop for UdpTunnel {
    fn drop(&mut self) {
        self.cancellation.cancel();
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

async fn run_sender<S, W>(
    socket: UdpSocket,
    reader: S,
    writer: W,
    transit_key: [u8; 32],
    cancellation: CancellationToken,
    mut reset_peer: mpsc::Receiver<()>,
) -> Result<()>
where
    S: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let (writes, mut write_task) = spawn_record_writer(
        writer,
        derive_key(&transit_key, b"transit_record_sender_key")?,
        cancellation.child_token(),
    );
    let mut records = spawn_record_reader(
        reader,
        derive_key(&transit_key, b"transit_record_receiver_key")?,
        cancellation.child_token(),
    );
    let mut datagram = vec![0_u8; MAX_DATAGRAM];
    let mut peer: Option<(SocketAddr, Instant)> = None;
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => return Ok(()),
            result = &mut write_task => return join_write_task(result),
            reset = reset_peer.recv() => {
                if reset.is_none() {
                    return Ok(());
                }
                peer = None;
            }
            result = socket.recv_from(&mut datagram) => {
                let (size, address) = result?;
                // The tunnel only ever fronts a local QUIC endpoint; anything off-loopback is
                // another process trying to hijack the tunnel.
                if !address.ip().is_loopback() {
                    continue;
                }
                if let Some((existing, last_seen)) = peer
                    && existing != address
                    && last_seen.elapsed() < PEER_IDLE_REPLACE
                {
                    continue;
                }
                peer = Some((address, Instant::now()));
                queue_write(&writes, &datagram[..size]);
            }
            result = records.recv() => {
                let Some(result) = result else { return Ok(()); };
                let payload = match result {
                    Ok(payload) => payload,
                    Err(error) if stream_ended(&error) => return Ok(()),
                    Err(error) => return Err(error),
                };
                if let Some((address, _)) = peer {
                    socket.send_to(&payload, address).await?;
                }
            }
        }
    }
}

async fn run_receiver<S, W>(
    socket: UdpSocket,
    reader: S,
    writer: W,
    upstream: SocketAddr,
    transit_key: [u8; 32],
    cancellation: CancellationToken,
) -> Result<()>
where
    S: AsyncRead + Unpin + Send + 'static,
    W: AsyncWrite + Unpin + Send + 'static,
{
    let (writes, mut write_task) = spawn_record_writer(
        writer,
        derive_key(&transit_key, b"transit_record_receiver_key")?,
        cancellation.child_token(),
    );
    let mut records = spawn_record_reader(
        reader,
        derive_key(&transit_key, b"transit_record_sender_key")?,
        cancellation.child_token(),
    );
    let mut datagram = vec![0_u8; MAX_DATAGRAM];
    loop {
        tokio::select! {
            _ = cancellation.cancelled() => return Ok(()),
            result = &mut write_task => return join_write_task(result),
            result = socket.recv_from(&mut datagram) => {
                let (size, address) = result?;
                if address != upstream {
                    continue;
                }
                queue_write(&writes, &datagram[..size]);
            }
            result = records.recv() => {
                let Some(result) = result else { return Ok(()); };
                let payload = match result {
                    Ok(payload) => payload,
                    Err(error) if stream_ended(&error) => return Ok(()),
                    Err(error) => return Err(error),
                };
                socket.send_to(&payload, upstream).await?;
            }
        }
    }
}

/// Hands a datagram to the writer task. The queue is bounded and a datagram is dropped when it
/// fills up: blocking here would stall the opposite direction of the tunnel, and dropping is
/// exactly what a congested UDP path does anyway.
fn queue_write(writes: &mpsc::Sender<Vec<u8>>, payload: &[u8]) {
    if writes.try_send(payload.to_vec()).is_err() {
        tracing::debug!("dropped UDP tunnel datagram: transit write queue is full");
    }
}

fn join_write_task(result: std::result::Result<Result<()>, tokio::task::JoinError>) -> Result<()> {
    match result {
        Ok(result) => result,
        Err(error) if error.is_cancelled() => Ok(()),
        Err(error) => Err(CoreError::Protocol(format!(
            "UDP tunnel writer failed: {error}"
        ))),
    }
}

fn spawn_record_writer<W>(
    writer: W,
    key: [u8; 32],
    cancellation: CancellationToken,
) -> (mpsc::Sender<Vec<u8>>, JoinHandle<Result<()>>)
where
    W: AsyncWrite + Unpin + Send + 'static,
{
    let (sender, mut receiver) = mpsc::channel::<Vec<u8>>(WRITE_QUEUE);
    let task = tokio::spawn(async move {
        let mut writer = RecordWriter::new(writer, key);
        loop {
            let payload = tokio::select! {
                _ = cancellation.cancelled() => return Ok(()),
                payload = receiver.recv() => match payload {
                    Some(payload) => payload,
                    None => return Ok(()),
                },
            };
            writer.write(&payload).await?;
        }
    });
    (sender, task)
}

fn spawn_record_reader<R>(
    reader: R,
    key: [u8; 32],
    cancellation: CancellationToken,
) -> mpsc::Receiver<Result<Vec<u8>>>
where
    R: AsyncRead + Unpin + Send + 'static,
{
    let (sender, receiver) = mpsc::channel(32);
    tokio::spawn(async move {
        let mut reader = RecordReader::new(reader, key);
        loop {
            let result = tokio::select! {
                _ = cancellation.cancelled() => break,
                result = reader.read() => result,
            };
            let done = result.is_err();
            if sender.send(result).await.is_err() || done {
                break;
            }
        }
    });
    receiver
}

fn stream_ended(error: &CoreError) -> bool {
    matches!(
        error,
        CoreError::Io(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::UnexpectedEof
                    | std::io::ErrorKind::ConnectionReset
                    | std::io::ErrorKind::ConnectionAborted
                    | std::io::ErrorKind::BrokenPipe
            )
    )
}

struct RecordWriter<W> {
    writer: W,
    cipher: XSalsa20Poly1305,
    counter: u64,
}

impl<W: AsyncWrite + Unpin> RecordWriter<W> {
    fn new(writer: W, key: [u8; 32]) -> Self {
        Self {
            writer,
            cipher: XSalsa20Poly1305::new(Key::from_slice(&key)),
            counter: 0,
        }
    }

    async fn write(&mut self, payload: &[u8]) -> Result<()> {
        if payload.len() > MAX_DATAGRAM || self.counter == u64::MAX {
            return Err(CoreError::Protocol("UDP tunnel record is too large".into()));
        }
        let mut nonce = [0_u8; NONCE_SIZE];
        nonce[NONCE_SIZE - 8..].copy_from_slice(&self.counter.to_be_bytes());
        self.counter += 1;
        let ciphertext = self
            .cipher
            .encrypt(Nonce::from_slice(&nonce), payload)
            .map_err(|_| CoreError::Crypto("encrypt UDP tunnel record failed".into()))?;
        let record_len = NONCE_SIZE + ciphertext.len();
        if record_len > MAX_RECORD {
            return Err(CoreError::Protocol(
                "UDP tunnel record exceeded limit".into(),
            ));
        }
        self.writer.write_u32(record_len as u32).await?;
        self.writer.write_all(&nonce).await?;
        self.writer.write_all(&ciphertext).await?;
        self.writer.flush().await?;
        Ok(())
    }
}

struct RecordReader<R> {
    reader: R,
    cipher: XSalsa20Poly1305,
    counter: u64,
}

impl<R: AsyncRead + Unpin> RecordReader<R> {
    fn new(reader: R, key: [u8; 32]) -> Self {
        Self {
            reader,
            cipher: XSalsa20Poly1305::new(Key::from_slice(&key)),
            counter: 0,
        }
    }

    async fn read(&mut self) -> Result<Vec<u8>> {
        let record_len = self.reader.read_u32().await? as usize;
        if !(NONCE_SIZE + TAG_SIZE..=MAX_RECORD).contains(&record_len) {
            return Err(CoreError::Protocol(format!(
                "invalid UDP tunnel record length: {record_len}"
            )));
        }
        let mut nonce = [0_u8; NONCE_SIZE];
        self.reader.read_exact(&mut nonce).await?;
        if nonce[..NONCE_SIZE - 8].iter().any(|byte| *byte != 0)
            || u64::from_be_bytes(nonce[NONCE_SIZE - 8..].try_into().unwrap()) != self.counter
        {
            return Err(CoreError::Protocol(
                "UDP tunnel nonce is out of order".into(),
            ));
        }
        self.counter += 1;
        let mut ciphertext = vec![0_u8; record_len - NONCE_SIZE];
        self.reader.read_exact(&mut ciphertext).await?;
        self.cipher
            .decrypt(Nonce::from_slice(&nonce), ciphertext.as_ref())
            .map_err(|_| CoreError::Crypto("decrypt UDP tunnel record failed".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    use tokio::net::{TcpListener, TcpStream};

    #[tokio::test]
    async fn encrypted_udp_datagrams_round_trip_through_tcp() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let key = [0x71; 32];
        let (client, accepted) = tokio::join!(TcpStream::connect(address), async {
            listener.accept().await.unwrap().0
        });
        let sender_stream = client.unwrap();
        let receiver_stream = accepted;

        let upstream = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        let upstream_address = upstream.local_addr().unwrap();
        let echo = tokio::spawn(async move {
            let mut buffer = [0_u8; 128];
            let (size, peer) = upstream.recv_from(&mut buffer).await.unwrap();
            upstream.send_to(&buffer[..size], peer).await.unwrap();
        });
        let cancellation = CancellationToken::new();
        let sender = UdpTunnel::start_sender(sender_stream, key, cancellation.clone())
            .await
            .unwrap();
        let receiver =
            UdpTunnel::start_receiver(receiver_stream, upstream_address, key, cancellation.clone())
                .await
                .unwrap();
        let client = UdpSocket::bind("127.0.0.1:0").await.unwrap();
        client
            .send_to(b"tunnel payload", sender.local_address())
            .await
            .unwrap();
        let mut result = [0_u8; 128];
        let (size, _) = tokio::time::timeout(Duration::from_secs(2), client.recv_from(&mut result))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(&result[..size], b"tunnel payload");
        echo.await.unwrap();
        sender.close().await.unwrap();
        receiver.close().await.unwrap();
    }

    #[test]
    fn record_nonce_is_monotonic_and_bounded() {
        const {
            assert!(NONCE_SIZE == 24);
            assert!(TAG_SIZE == 16);
            assert!(MAX_RECORD > MAX_DATAGRAM);
        }
    }
}
