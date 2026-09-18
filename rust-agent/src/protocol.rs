//! Wire protocol for talking to the blumkin-agent background process.
//!
//! Mirrors `blumkin.agent.protocol` (Python) byte-for-byte: newline-delimited
//! JSON, one message per call. Kept as a from-scratch Rust implementation
//! (not FFI into the Python module) so the compiled agent has no Python
//! runtime dependency at all - `blumkin.agent.client` only cares that
//! whatever process is listening on the socket speaks this exact framing.

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::time::{Duration, Instant};

use serde_json::Value;

/// Bumped whenever the wire format changes in a way an older/newer agent and
/// client cannot safely interoperate with. See the "Installation & upgrade"
/// section of issue #328: a mismatch tells the client this agent is stale
/// (e.g. still resident from before a `pipx upgrade`/`uv tool upgrade`
/// replaced the binary), so it can wait for this agent to exit and spawn a
/// fresh one.
pub const PROTOCOL_VERSION: u64 = 1;

/// Generous but bounded: every message here is a small control/status
/// object today, and even once secret payloads flow over this protocol in a
/// later layer, MSAL token caches / Google credential JSON are a few KB at
/// most. Bounding rules out a runaway/misbehaving peer exhausting memory.
const MAX_MESSAGE_BYTES: usize = 1_000_000;

#[derive(Debug)]
pub struct ProtocolError(pub String);

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for ProtocolError {}

/// Read one newline-delimited JSON message.
///
/// Reads exactly one message per call, matching the request/response
/// pattern over a short-lived connection that `blumkin.agent.client` (and
/// this daemon's own stale-agent probe) both use.
///
/// `timeout` bounds the *whole* message, not a single `read(2)` call: a
/// deadline is computed once up front and each read is given only the
/// remaining budget (re-set via `set_read_timeout` before every read). A
/// naive "hand the same duration to every read" implementation lets a peer
/// that trickles one byte every few seconds - never quite hitting a single
/// read's timeout, never sending the terminating newline - keep this
/// function (and the caller's single-threaded accept loop) blocked
/// indefinitely, well past the caller's advertised budget (see PR #329
/// review).
pub fn recv_message(
    stream: &mut UnixStream,
    timeout: Option<Duration>,
) -> Result<Value, ProtocolError> {
    let deadline = timeout.map(|d| Instant::now() + d);
    let mut buf: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        let read_timeout = match deadline {
            Some(deadline) => {
                let remaining = deadline.saturating_duration_since(Instant::now());
                if remaining.is_zero() {
                    return Err(ProtocolError("timed out waiting for a message".to_string()));
                }
                Some(remaining)
            }
            None => None,
        };
        stream
            .set_read_timeout(read_timeout)
            .map_err(|e| ProtocolError(format!("could not set read timeout: {e}")))?;
        let read = match stream.read(&mut chunk) {
            Ok(n) => n,
            Err(e) if e.kind() == ErrorKind::WouldBlock => {
                return Err(ProtocolError("timed out waiting for a message".to_string()));
            }
            Err(e) => return Err(ProtocolError(format!("read failed: {e}"))),
        };
        if read == 0 {
            if buf.is_empty() {
                return Err(ProtocolError(
                    "connection closed before any data was received".to_string(),
                ));
            }
            return Err(ProtocolError("connection closed mid-message".to_string()));
        }
        buf.extend_from_slice(&chunk[..read]);
        if buf.len() > MAX_MESSAGE_BYTES {
            return Err(ProtocolError(format!(
                "message too large ({} bytes)",
                buf.len()
            )));
        }
        let Some(newline_index) = buf.iter().position(|&b| b == b'\n') else {
            continue;
        };
        let line = &buf[..newline_index];
        let text = std::str::from_utf8(line)
            .map_err(|e| ProtocolError(format!("malformed message: {e}")))?;
        let parsed: Value = serde_json::from_str(text)
            .map_err(|e| ProtocolError(format!("malformed message: {e}")))?;
        if !parsed.is_object() {
            return Err(ProtocolError("message must be a JSON object".to_string()));
        }
        return Ok(parsed);
    }
}

/// Send one newline-delimited JSON message.
///
/// Newline-delimited rather than length-prefixed: every message is a single
/// JSON object, and `serde_json` always escapes a raw `\n` inside a string
/// value as the two characters `\` `n` - so the literal newline byte this
/// function appends can never appear earlier in the encoded payload, making
/// it an unambiguous message terminator.
pub fn send_message(stream: &mut UnixStream, payload: &Value) -> Result<(), ProtocolError> {
    let mut line =
        serde_json::to_vec(payload).map_err(|e| ProtocolError(format!("encode failed: {e}")))?;
    // Bound the payload plus the terminating newline `recv_message` will
    // count on the other end (`buf.len()` there is checked after the `\n`
    // is already in the buffer) - checking only the pre-newline length here
    // would let a message exactly at the limit round-trip through
    // `send_message` but bounce off `recv_message`'s stricter check on the
    // receiving side (see PR #329 review).
    if line.len() + 1 > MAX_MESSAGE_BYTES {
        return Err(ProtocolError(format!(
            "message too large ({} bytes)",
            line.len() + 1
        )));
    }
    line.push(b'\n');
    stream
        .write_all(&line)
        .map_err(|e| ProtocolError(format!("write failed: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn recv_message_roundtrips_a_message() {
        let (mut left, mut right) = UnixStream::pair().unwrap();
        send_message(&mut left, &serde_json::json!({"cmd": "ping"})).unwrap();

        let received = recv_message(&mut right, None).unwrap();

        assert_eq!(received["cmd"], "ping");
    }

    #[test]
    fn recv_message_deadline_bounds_the_whole_message_not_each_read() {
        // A peer trickling one byte at a time without ever sending the
        // terminating newline must not wedge `recv_message` past its
        // overall `timeout`, even though every individual `read()` that
        // returns bytes would otherwise reset a per-call `SO_RCVTIMEO`
        // clock (see PR #329 review and the docstring above).
        let (mut left, mut right) = UnixStream::pair().unwrap();
        let release = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let release_writer = release.clone();
        let writer = thread::spawn(move || {
            for byte in b'{'..=b'{' {
                let _ = left.write_all(&[byte]);
            }
            // Keep trickling well past the 200ms deadline below, one byte
            // every 30ms, until the test tells us to stop.
            while !release_writer.load(std::sync::atomic::Ordering::Relaxed) {
                if left.write_all(b"x").is_err() {
                    break;
                }
                thread::sleep(Duration::from_millis(30));
            }
        });

        let started = Instant::now();
        let result = recv_message(&mut right, Some(Duration::from_millis(200)));
        let elapsed = started.elapsed();

        assert!(result.is_err());
        assert!(elapsed < Duration::from_millis(500));
        release.store(true, std::sync::atomic::Ordering::Relaxed);
        writer.join().unwrap();
    }

    #[test]
    fn send_message_rejects_a_payload_that_is_exactly_at_the_limit_once_the_newline_is_counted() {
        // A payload whose JSON encoding is exactly `MAX_MESSAGE_BYTES` bytes
        // long still grows to `MAX_MESSAGE_BYTES + 1` once the terminating
        // newline is appended - `recv_message`'s own bound is checked after
        // that newline is already in its buffer, so `send_message` must
        // reject this case rather than let it through only to have the
        // receiver never see it as a distinct error (see PR #329 review).
        let (mut left, _right) = UnixStream::pair().unwrap();
        // `{"k":"..."}` framing is 8 bytes; pad the string value so the
        // whole encoded line is exactly `MAX_MESSAGE_BYTES` bytes.
        let padding = "x".repeat(MAX_MESSAGE_BYTES - 8);
        let payload = serde_json::json!({ "k": padding });

        let result = send_message(&mut left, &payload);

        assert!(result.is_err());
    }

    #[test]
    fn recv_message_rejects_a_message_larger_than_the_limit() {
        // Pins `recv_message`'s own bound directly (the client-side mirror
        // already has this assertion in
        // `tests/test_agent_protocol_unit.py::test_recv_raises_on_oversized_message`,
        // but nothing here drove `recv_message` itself past its limit -
        // dropping or inverting the `buf.len() > MAX_MESSAGE_BYTES` check
        // would still leave `cargo test` green (see PR #329 review).
        let (mut left, mut right) = UnixStream::pair().unwrap();
        let writer = thread::spawn(move || {
            // No newline: the oversized-buffer check must fire before
            // `recv_message` ever finds a terminator to parse. Deliberately
            // leaks `left` (rather than letting it drop when this thread
            // exits) so the write end never closes mid-read - on macOS,
            // `set_read_timeout` on the still-open `right` end returns
            // `EINVAL` once its peer has disconnected, which would make
            // this test flake on exactly which error `recv_message`
            // surfaces depending on how many 4096-byte chunks it had read
            // before that race won.
            let _ = left.write_all(&vec![b'x'; MAX_MESSAGE_BYTES + 1]);
            std::mem::forget(left);
        });

        let result = recv_message(&mut right, None);

        assert!(matches!(result, Err(ProtocolError(msg)) if msg.contains("too large")));
        writer.join().unwrap();
    }
}
