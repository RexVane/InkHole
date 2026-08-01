use std::{collections::VecDeque, time::Duration};

use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use tokio::net::TcpStream;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async, tungstenite::Message};
use tokio_util::sync::CancellationToken;

use crate::{CoreError, Result};

const MAX_RENDEZVOUS_MESSAGE: usize = 64 * 1024;
const MAX_PENDING_MESSAGES: usize = 64;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
const WELCOME_TIMEOUT: Duration = Duration::from_secs(10);

type RendezvousSocket = WebSocketStream<MaybeTlsStream<TcpStream>>;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub(super) struct MailboxMessage {
    pub side: String,
    pub phase: String,
    pub body: String,
}

pub(super) struct RendezvousClient {
    socket: RendezvousSocket,
    pub(super) side_id: String,
    mailbox_id: String,
    nameplate: Option<String>,
    pending: VecDeque<Value>,
    mailbox: VecDeque<MailboxMessage>,
}

impl RendezvousClient {
    pub async fn connect(url: &str, side_id: String, app_id: &str) -> Result<Self> {
        let url = url.trim();
        if !matches!(url.split_once("://"), Some(("ws" | "wss", _))) {
            return Err(CoreError::InvalidRequest(
                "rendezvous URL must use ws:// or wss://".into(),
            ));
        }
        let (socket, _) = tokio::time::timeout(CONNECT_TIMEOUT, connect_async(url))
            .await
            .map_err(|_| protocol_error("connect rendezvous timed out"))?
            .map_err(|error| protocol_error(format!("connect rendezvous: {error}")))?;
        let mut client = Self {
            socket,
            side_id,
            mailbox_id: String::new(),
            nameplate: None,
            pending: VecDeque::new(),
            mailbox: VecDeque::new(),
        };
        let welcome = tokio::time::timeout(WELCOME_TIMEOUT, client.read_type("welcome"))
            .await
            .map_err(|_| protocol_error("rendezvous welcome timed out"))??;
        if let Some(error) = welcome
            .get("welcome")
            .and_then(|value| value.get("error"))
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            return Err(protocol_error(format!(
                "rendezvous rejected client: {error}"
            )));
        }
        client
            .send_command(
                "bind",
                json!({
                    "side": client.side_id,
                    "appid": app_id,
                    "client_version": ["inkhole-rust", env!("CARGO_PKG_VERSION")],
                }),
            )
            .await?;
        Ok(client)
    }

    pub async fn create_mailbox(&mut self) -> Result<String> {
        self.send_command("allocate", Value::Object(Map::new()))
            .await?;
        let allocated = self.read_type("allocated").await?;
        let nameplate = required_string(&allocated, "nameplate")?;
        self.claim_and_open(&nameplate).await?;
        Ok(nameplate)
    }

    pub async fn attach_mailbox(&mut self, nameplate: &str) -> Result<()> {
        if nameplate.is_empty() || !nameplate.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(CoreError::InvalidRequest(
                "short code has an invalid nameplate".into(),
            ));
        }
        self.claim_and_open(nameplate).await
    }

    async fn claim_and_open(&mut self, nameplate: &str) -> Result<()> {
        self.send_command("claim", json!({ "nameplate": nameplate }))
            .await?;
        let claimed = self.read_type("claimed").await?;
        let mailbox = required_string(&claimed, "mailbox")?;
        self.send_command("open", json!({ "mailbox": mailbox }))
            .await?;
        self.mailbox_id = mailbox;
        self.nameplate = Some(nameplate.to_owned());
        Ok(())
    }

    pub async fn add_message(&mut self, phase: &str, body: &str) -> Result<()> {
        if phase.is_empty() || phase.len() > 64 || body.len() > MAX_RENDEZVOUS_MESSAGE {
            return Err(CoreError::InvalidRequest(
                "rendezvous mailbox message is invalid".into(),
            ));
        }
        self.send_command("add", json!({ "phase": phase, "body": body }))
            .await
    }

    pub async fn next_peer_message(&mut self) -> Result<MailboxMessage> {
        loop {
            if let Some(message) = self.mailbox.pop_front() {
                if self.nameplate.is_some() {
                    self.release_nameplate().await?;
                }
                return Ok(message);
            }
            let value = self.read_socket_value().await?;
            self.dispatch(value)?;
        }
    }

    /// Keeps the websocket serviced while nothing else is driving it — answering server pings and
    /// buffering anything the peer sends early. Returns the client, buffers intact, once `stop`
    /// fires so the normal flow can resume where it left off.
    pub async fn pump(mut self, stop: CancellationToken) -> Result<Self> {
        loop {
            let value = tokio::select! {
                _ = stop.cancelled() => return Ok(self),
                value = self.read_socket_value() => value?,
            };
            self.dispatch(value)?;
        }
    }

    pub async fn close(mut self, mood: &str) -> Result<()> {
        if !self.mailbox_id.is_empty() {
            let mailbox = self.mailbox_id.clone();
            self.send_command(
                "close",
                json!({
                    "mailbox": mailbox,
                    "mood": if mood.is_empty() { "happy" } else { mood },
                }),
            )
            .await?;
            let _ = self.read_type("closed").await?;
        }
        self.socket
            .close(None)
            .await
            .map_err(|error| protocol_error(format!("close rendezvous socket: {error}")))
    }

    async fn release_nameplate(&mut self) -> Result<()> {
        let Some(nameplate) = self.nameplate.take() else {
            return Ok(());
        };
        self.send_command("release", json!({ "nameplate": nameplate }))
            .await?;
        let _ = self.read_type("released").await?;
        Ok(())
    }

    async fn send_command(&mut self, command: &str, fields: Value) -> Result<()> {
        let id = format!("{:04x}", rand::random::<u16>());
        let mut object = match fields {
            Value::Object(object) => object,
            _ => {
                return Err(CoreError::InvalidRequest(
                    "rendezvous command fields must be an object".into(),
                ));
            }
        };
        object.insert("type".into(), Value::String(command.to_owned()));
        object.insert("id".into(), Value::String(id.clone()));
        let encoded = serde_json::to_string(&object)?;
        if encoded.len() > MAX_RENDEZVOUS_MESSAGE {
            return Err(CoreError::InvalidRequest(
                "rendezvous command is too large".into(),
            ));
        }
        self.socket
            .send(Message::Text(encoded.into()))
            .await
            .map_err(|error| protocol_error(format!("write rendezvous command: {error}")))?;
        let acknowledgement = self.read_type("ack").await?;
        if acknowledgement.get("id").and_then(Value::as_str) != Some(id.as_str()) {
            return Err(protocol_error("rendezvous acknowledgement ID mismatch"));
        }
        Ok(())
    }

    async fn read_type(&mut self, expected: &str) -> Result<Value> {
        loop {
            if let Some(index) = self
                .pending
                .iter()
                .position(|value| message_type(value) == Some(expected))
            {
                return self
                    .pending
                    .remove(index)
                    .ok_or_else(|| protocol_error("pending rendezvous message disappeared"));
            }
            let value = self.read_socket_value().await?;
            if message_type(&value) == Some(expected) {
                return Ok(value);
            }
            self.dispatch(value)?;
        }
    }

    fn dispatch(&mut self, value: Value) -> Result<()> {
        match message_type(&value) {
            Some("message") => {
                let message: MailboxMessage = serde_json::from_value(value)?;
                if message.side != self.side_id {
                    if self.mailbox.len() >= MAX_PENDING_MESSAGES {
                        return Err(protocol_error("too many pending mailbox messages"));
                    }
                    self.mailbox.push_back(message);
                }
            }
            Some("error") => {
                let message = value
                    .get("error")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown rendezvous error");
                return Err(protocol_error(format!(
                    "rendezvous server error: {message}"
                )));
            }
            Some(_) => {
                if self.pending.len() >= MAX_PENDING_MESSAGES {
                    return Err(protocol_error("too many pending rendezvous messages"));
                }
                self.pending.push_back(value);
            }
            None => return Err(protocol_error("rendezvous message has no type")),
        }
        Ok(())
    }

    async fn read_socket_value(&mut self) -> Result<Value> {
        loop {
            let message = self
                .socket
                .next()
                .await
                .ok_or_else(|| protocol_error("rendezvous connection ended"))?
                .map_err(|error| protocol_error(format!("read rendezvous message: {error}")))?;
            let data = match message {
                Message::Text(text) => text.as_bytes().to_vec(),
                Message::Binary(data) => data.to_vec(),
                Message::Ping(data) => {
                    self.socket
                        .send(Message::Pong(data))
                        .await
                        .map_err(|error| {
                            protocol_error(format!("reply to rendezvous ping: {error}"))
                        })?;
                    continue;
                }
                Message::Pong(_) => continue,
                Message::Close(_) => {
                    return Err(protocol_error("rendezvous server closed the connection"));
                }
                Message::Frame(_) => continue,
            };
            if data.len() > MAX_RENDEZVOUS_MESSAGE {
                return Err(protocol_error("rendezvous message is too large"));
            }
            return serde_json::from_slice(&data).map_err(Into::into);
        }
    }
}

fn message_type(value: &Value) -> Option<&str> {
    value.get("type").and_then(Value::as_str)
}

fn required_string(value: &Value, field: &str) -> Result<String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| protocol_error(format!("rendezvous response has no {field}")))
}

fn protocol_error(message: impl Into<String>) -> CoreError {
    CoreError::Protocol(message.into())
}
