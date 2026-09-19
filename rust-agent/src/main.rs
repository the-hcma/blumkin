//! Entry point for `blumkin-agent`, spawned by `blumkin.agent.client`.

fn main() {
    server::run();
}

mod paths;
mod presence;
mod protocol;
mod secret_cache;
mod server;
mod version;
