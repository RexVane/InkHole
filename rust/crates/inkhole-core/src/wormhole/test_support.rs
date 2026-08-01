use std::{collections::HashMap, sync::Arc};

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::{
    net::{TcpListener, TcpStream},
    sync::{Mutex, mpsc},
    task::{JoinHandle, JoinSet},
};
use tokio_tungstenite::{accept_async, tungstenite::Message};
use tokio_util::sync::CancellationToken;

use super::protocol::WORMHOLE_APP_ID;

type Outbound = mpsc::UnboundedSender<Message>;
type TestResult<T = ()> = std::result::Result<T, String>;

#[derive(Default)]
struct Mailbox {
    messages: Vec<Value>,
    clients: HashMap<String, Outbound>,
}

#[derive(Default)]
struct ServerState {
    next_nameplate: u64,
    mailboxes: HashMap<String, Mailbox>,
}

#[derive(Default)]
struct ConnectionState {
    side: String,
    mailbox: Option<String>,
}

pub(crate) struct MockRendezvous {
    url: String,
    cancellation: CancellationToken,
    task: Option<JoinHandle<TestResult>>,
}

impl MockRendezvous {
    pub(crate) async fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let cancellation = CancellationToken::new();
        let task_cancellation = cancellation.clone();
        let state = Arc::new(Mutex::new(ServerState::default()));
        let task = tokio::spawn(async move {
            let mut connections = JoinSet::new();
            loop {
                tokio::select! {
                    _ = task_cancellation.cancelled() => break,
                    accepted = listener.accept() => {
                        let (stream, _) = accepted.map_err(|error| error.to_string())?;
                        connections.spawn(serve_connection(
                            stream,
                            state.clone(),
                            task_cancellation.child_token(),
                        ));
                    }
                }
            }
            while let Some(result) = connections.join_next().await {
                result.map_err(|error| error.to_string())??;
            }
            Ok(())
        });
        Self {
            url: format!("ws://{address}"),
            cancellation,
            task: Some(task),
        }
    }

    pub(crate) fn url(&self) -> &str {
        &self.url
    }

    pub(crate) async fn close(mut self) {
        self.cancellation.cancel();
        if let Some(task) = self.task.take() {
            task.await.unwrap().unwrap();
        }
    }
}

impl Drop for MockRendezvous {
    fn drop(&mut self) {
        self.cancellation.cancel();
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

async fn serve_connection(
    stream: TcpStream,
    state: Arc<Mutex<ServerState>>,
    cancellation: CancellationToken,
) -> TestResult {
    let websocket = accept_async(stream)
        .await
        .map_err(|error| error.to_string())?;
    let (mut writer, mut reader) = websocket.split();
    let (outbound, mut outbound_rx) = mpsc::unbounded_channel();
    let writer_cancellation = cancellation.clone();
    let writer_task = tokio::spawn(async move {
        loop {
            let message = tokio::select! {
                _ = writer_cancellation.cancelled() => break,
                message = outbound_rx.recv() => match message {
                    Some(message) => message,
                    None => break,
                },
            };
            if writer.send(message).await.is_err() {
                break;
            }
        }
        let _ = writer.close().await;
    });
    send_json(&outbound, json!({ "type": "welcome", "welcome": {} }))?;

    let mut connection = ConnectionState::default();
    let result = async {
        loop {
            let message = tokio::select! {
                _ = cancellation.cancelled() => break,
                message = reader.next() => match message {
                    Some(Ok(message)) => message,
                    Some(Err(error)) => return Err(error.to_string()),
                    None => break,
                },
            };
            let command = match message {
                Message::Text(text) => {
                    serde_json::from_str(&text).map_err(|error| error.to_string())?
                }
                Message::Binary(data) => {
                    serde_json::from_slice(&data).map_err(|error| error.to_string())?
                }
                Message::Ping(data) => {
                    outbound
                        .send(Message::Pong(data))
                        .map_err(|error| error.to_string())?;
                    continue;
                }
                Message::Pong(_) | Message::Frame(_) => continue,
                Message::Close(_) => break,
            };
            process_command(command, &mut connection, &outbound, &state).await?;
        }
        Ok(())
    }
    .await;

    if let Some(mailbox) = &connection.mailbox {
        let mut state = state.lock().await;
        if let Some(mailbox) = state.mailboxes.get_mut(mailbox) {
            mailbox.clients.remove(&connection.side);
        }
    }
    drop(outbound);
    let _ = writer_task.await;
    result
}

async fn process_command(
    command: Value,
    connection: &mut ConnectionState,
    outbound: &Outbound,
    state: &Arc<Mutex<ServerState>>,
) -> TestResult {
    let command_type = required_string(&command, "type")?;
    let id = required_string(&command, "id")?;
    send_json(outbound, json!({ "type": "ack", "id": id }))?;

    match command_type.as_str() {
        "bind" => {
            if required_string(&command, "appid")? != WORMHOLE_APP_ID {
                return Err("unexpected rendezvous app ID".into());
            }
            connection.side = required_string(&command, "side")?;
        }
        "allocate" => {
            let nameplate = {
                let mut state = state.lock().await;
                state.next_nameplate += 1;
                state.next_nameplate.to_string()
            };
            send_json(
                outbound,
                json!({ "type": "allocated", "nameplate": nameplate }),
            )?;
        }
        "claim" => {
            let nameplate = required_string(&command, "nameplate")?;
            let mailbox = format!("mailbox-{nameplate}");
            state
                .lock()
                .await
                .mailboxes
                .entry(mailbox.clone())
                .or_default();
            send_json(outbound, json!({ "type": "claimed", "mailbox": mailbox }))?;
        }
        "open" => {
            if connection.side.is_empty() {
                return Err("rendezvous mailbox opened before bind".into());
            }
            let mailbox_id = required_string(&command, "mailbox")?;
            let history = {
                let mut state = state.lock().await;
                let mailbox = state.mailboxes.entry(mailbox_id.clone()).or_default();
                mailbox
                    .clients
                    .insert(connection.side.clone(), outbound.clone());
                mailbox.messages.clone()
            };
            connection.mailbox = Some(mailbox_id);
            for message in history {
                send_json(outbound, message)?;
            }
        }
        "add" => {
            let mailbox_id = connection
                .mailbox
                .as_ref()
                .ok_or_else(|| "rendezvous message added before open".to_owned())?;
            let message = json!({
                "type": "message",
                "side": connection.side,
                "phase": required_string(&command, "phase")?,
                "body": required_string(&command, "body")?,
            });
            let recipients = {
                let mut state = state.lock().await;
                let mailbox = state.mailboxes.entry(mailbox_id.clone()).or_default();
                mailbox.messages.push(message.clone());
                mailbox.clients.values().cloned().collect::<Vec<_>>()
            };
            for recipient in recipients {
                let _ = send_json(&recipient, message.clone());
            }
        }
        "release" => send_json(outbound, json!({ "type": "released" }))?,
        "close" => send_json(outbound, json!({ "type": "closed" }))?,
        other => return Err(format!("unsupported rendezvous command: {other}")),
    }
    Ok(())
}

fn required_string(value: &Value, field: &str) -> TestResult<String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| format!("rendezvous command has no {field}"))
}

fn send_json(outbound: &Outbound, value: Value) -> TestResult {
    let encoded = serde_json::to_string(&value).map_err(|error| error.to_string())?;
    outbound
        .send(Message::Text(encoded.into()))
        .map_err(|error| error.to_string())
}
