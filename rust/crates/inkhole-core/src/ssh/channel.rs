use std::{
    io,
    pin::Pin,
    task::{Context, Poll, ready},
};

use russh::{Channel, ChannelMsg, ChannelReadHalf, ChannelWriteHalf, client::Msg};
use tokio::{
    io::{AsyncRead, AsyncWrite, ReadBuf},
    sync::mpsc,
    task::JoinHandle,
};

const READ_BUFFER_MESSAGES: usize = 64;

pub(crate) struct SshChannelStream {
    reader: SshChannelReader,
    writer: SshChannelWriter,
}

impl SshChannelStream {
    pub(crate) fn new(channel: Channel<Msg>) -> Self {
        let (reader, writer) = channel.split();
        Self {
            reader: SshChannelReader::new(reader),
            writer: SshChannelWriter::new(writer),
        }
    }

    pub(crate) fn into_parts(self) -> (SshChannelReader, SshChannelWriter) {
        (self.reader, self.writer)
    }
}

impl AsyncRead for SshChannelStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.reader).poll_read(context, buffer)
    }
}

impl AsyncWrite for SshChannelStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.writer).poll_write(context, buffer)
    }

    fn poll_flush(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.writer).poll_flush(context)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.writer).poll_shutdown(context)
    }
}

pub(crate) struct SshChannelReader {
    receiver: mpsc::Receiver<ChannelMsg>,
    buffered: Option<(ChannelMsg, usize)>,
    pump: JoinHandle<()>,
}

impl SshChannelReader {
    fn new(reader: ChannelReadHalf) -> Self {
        let (sender, receiver) = mpsc::channel(READ_BUFFER_MESSAGES);
        let pump = tokio::spawn(pump_channel_reader(reader, sender));
        Self {
            receiver,
            buffered: None,
            pump,
        }
    }
}

impl AsyncRead for SshChannelReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if buffer.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        loop {
            let (message, mut offset) = match self.buffered.take() {
                Some(buffered) => buffered,
                None => match ready!(self.receiver.poll_recv(context)) {
                    Some(message) => (message, 0),
                    None => return Poll::Ready(Ok(())),
                },
            };
            let ChannelMsg::Data { data } = &message else {
                continue;
            };
            if offset == data.len() {
                continue;
            }
            let readable = buffer.remaining().min(data.len() - offset);
            buffer.put_slice(&data[offset..offset + readable]);
            offset += readable;
            if offset != data.len() {
                self.buffered = Some((message, offset));
            }
            return Poll::Ready(Ok(()));
        }
    }
}

impl Drop for SshChannelReader {
    fn drop(&mut self) {
        self.pump.abort();
    }
}

async fn pump_channel_reader(mut reader: ChannelReadHalf, sender: mpsc::Sender<ChannelMsg>) {
    while let Some(message) = reader.wait().await {
        match message {
            message @ ChannelMsg::Data { .. } => {
                if sender.send(message).await.is_err() {
                    break;
                }
            }
            ChannelMsg::Eof | ChannelMsg::Close => break,
            _ => {}
        }
    }
}

pub(crate) struct SshChannelWriter {
    writer: Pin<Box<dyn AsyncWrite + Send>>,
    control: Option<ChannelWriteHalf<Msg>>,
}

impl SshChannelWriter {
    fn new(control: ChannelWriteHalf<Msg>) -> Self {
        let writer = Box::pin(control.make_writer());
        Self {
            writer,
            control: Some(control),
        }
    }
}

impl AsyncWrite for SshChannelWriter {
    fn poll_write(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &[u8],
    ) -> Poll<io::Result<usize>> {
        self.writer.as_mut().poll_write(context, buffer)
    }

    fn poll_flush(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.writer.as_mut().poll_flush(context)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, context: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.writer.as_mut().poll_shutdown(context)
    }
}

impl Drop for SshChannelWriter {
    fn drop(&mut self) {
        let Some(control) = self.control.take() else {
            return;
        };
        match tokio::runtime::Handle::try_current() {
            Ok(runtime) => {
                runtime.spawn(async move {
                    let _ = control.close().await;
                });
            }
            // Without a runtime the close message cannot be sent; the server sees
            // the channel end when the SSH connection itself is torn down.
            Err(error) => {
                tracing::debug!(%error, "dropped SSH channel writer outside a runtime");
            }
        }
    }
}
