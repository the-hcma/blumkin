//! Entry point for `blumkin-agent`, spawned by `blumkin.agent.client`.

fn main() {
    server::run();
}

mod paths;
mod protocol;
mod server;
mod version;
