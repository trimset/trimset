//! trimset's front door (ADR 0015). It serves the proxy itself, the hot path, and passes
//! every other request to the Python server unchanged.
//!
//! The proxy carries bytes; it doesn't interpret them. It checks the key (asking the
//! Python server, then caching the answer), forwards the request to the project's
//! upstream, returns the upstream's response with one decision id per question, and
//! hands the exchange to the Python server in batches. Wire formats, hashing and storage
//! stay in Python, in one place (docs/03-api/wire-formats.md).

use std::collections::{HashMap, VecDeque};
use std::convert::Infallible;
use std::env;
use std::future::Future;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use bytes::Bytes;
use http::header::{AUTHORIZATION, CONTENT_TYPE, HeaderName, HeaderValue};
use http::{HeaderMap, Method, Request, Response, StatusCode, Uri};
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full, LengthLimitError, Limited};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper_rustls::HttpsConnector;
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::{TokioExecutor, TokioIo};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use tokio::sync::Notify;

type BoxError = Box<dyn std::error::Error + Send + Sync>;
type Body = BoxBody<Bytes, BoxError>;

const MAX_RESPONSE: usize = 16 * 1024 * 1024;
const MAX_STALE: Duration = Duration::from_secs(600); // the oldest key answer used in an outage
const BATCH: usize = 500;
const PROBLEMS: &str = "https://trimset.dev/problems/";
const HOP_BY_HOP: [&str; 8] = [
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
];

// ------------------------------------------------------------------ formats and errors

#[derive(Clone, Copy, Debug, PartialEq)]
enum Format {
    Systemone,
    OpenAi,
}

impl Format {
    fn parse(name: &str) -> Format {
        if name == "openai-decisions" {
            Format::OpenAi
        } else {
            Format::Systemone
        }
    }

    fn name(self) -> &'static str {
        match self {
            Format::Systemone => "systemone",
            Format::OpenAi => "openai-decisions",
        }
    }
}

fn full(body: impl Into<Bytes>) -> Body {
    Full::new(body.into())
        .map_err(|never| match never {})
        .boxed()
}

fn json_response(status: u16, value: &Value, content_type: &'static str) -> Response<Body> {
    let mut response = Response::new(full(value.to_string()));
    *response.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
    response
        .headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
    response
}

/// An error from trimset itself, in the upstream's format (wire-formats.md §6).
fn fail(format: Format, status: u16, message: &str, code: &str) -> Response<Body> {
    let body = match format {
        Format::OpenAi => json!({"error": {
            "message": message, "type": "trimset_error", "code": code, "param": null}}),
        Format::Systemone => json!({"detail": message}),
    };
    json_response(status, &body, "application/json")
}

fn problem(status: u16, slug: &str, title: &str) -> Response<Body> {
    let body = json!({"type": format!("{PROBLEMS}{slug}"), "title": title, "status": status});
    json_response(status, &body, "application/problem+json")
}

// ------------------------------------------------------------------ routes

#[derive(Debug, PartialEq)]
enum Route {
    Proxy {
        key: Option<String>,
        path: &'static str,
        guess: Format,
    },
    Health,
    Internal,
    Api,
}

fn route(method: &Method, path: &str, headers: &HeaderMap) -> Route {
    const PROXIED: [(&Method, &str, Format); 3] = [
        (&Method::POST, "/v1/systemone", Format::Systemone),
        (&Method::POST, "/v1/decisions", Format::OpenAi),
        (&Method::GET, "/v1/models", Format::Systemone),
    ];
    if path == "/health" {
        return Route::Health;
    }
    if path == "/internal" || path.starts_with("/internal/") {
        return Route::Internal;
    }
    for (m, p, guess) in PROXIED {
        if method != m {
            continue;
        }
        if path == p {
            let key = headers.get("x-trimset-key").and_then(|v| v.to_str().ok());
            return Route::Proxy {
                key: key.map(str::to_owned),
                path: p,
                guess,
            };
        }
        // Path mode: /p/<key>/v1/...; the key never reaches a log line.
        if let Some(rest) = path.strip_prefix("/p/")
            && let Some((key, tail)) = rest.split_once('/')
            && !key.is_empty()
            && tail == &p[1..]
        {
            return Route::Proxy {
                key: Some(key.to_owned()),
                path: p,
                guess,
            };
        }
    }
    Route::Api
}

/// A trimset key: four scopes, each a prefix and 32 base62 characters (keys.py).
fn looks_like_key(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.starts_with(b"ts")
        && matches!(bytes[2], b'p' | b'i' | b'r' | b'a')
        && bytes[3] == b'_'
        && bytes[4..].iter().all(u8::is_ascii_alphanumeric)
}

/// The headers sent upstream: end-to-end ones only, never a trimset header or key
/// (ADR 0011). With the operator's key, the app's Authorization is replaced.
fn forward_headers(incoming: &HeaderMap, server_key: Option<&str>) -> HeaderMap {
    let mut out = HeaderMap::with_capacity(incoming.len());
    for (name, value) in incoming {
        let lower = name.as_str();
        if HOP_BY_HOP.contains(&lower)
            || matches!(lower, "host" | "content-length" | "accept-encoding")
            || lower.starts_with("x-trimset-")
        {
            continue;
        }
        if name == AUTHORIZATION {
            if server_key.is_some() {
                continue;
            }
            let text = value.to_str().unwrap_or("").trim();
            let token = text.split_once(' ').map_or(text, |(_, t)| t.trim());
            if looks_like_key(token) {
                continue;
            }
        }
        out.append(name.clone(), value.clone());
    }
    if let Some(key) = server_key
        && let Ok(value) = HeaderValue::from_str(&format!("Bearer {key}"))
    {
        out.insert(AUTHORIZATION, value);
    }
    out
}

fn returned_headers(from: &HeaderMap, to: &mut HeaderMap) {
    for (name, value) in from {
        let lower = name.as_str();
        if HOP_BY_HOP.contains(&lower)
            || matches!(lower, "content-length" | "content-encoding")
            || lower.starts_with("x-trimset-")
        {
            continue;
        }
        to.append(name.clone(), value.clone());
    }
}

// ------------------------------------------------------------------ keys

#[derive(Debug, Deserialize)]
struct Upstream {
    name: String,
    url: String,
    format: String,
    server_key: bool,
    timeout_ms: u64,
}

#[derive(Debug, Deserialize)]
struct Principal {
    project_id: String,
    upstream: Upstream,
    max_body_bytes: usize,
}

#[derive(Clone, Debug, Deserialize)]
struct Denied {
    status: u16,
    code: String,
    message: String,
}

#[derive(Debug, Deserialize)]
struct PrincipalReply {
    principal: Option<Principal>,
    denied: Option<Denied>,
}

#[derive(Clone)]
enum Answer {
    Allowed(Arc<Principal>),
    Denied(Denied),
}

struct Cached {
    at: Instant,
    answer: Answer,
}

// ------------------------------------------------------------------ the exchange queue

#[derive(Serialize)]
struct Exchange {
    project_id: String,
    format: &'static str,
    request: String,
    response: String,
    decision_ids: Vec<String>,
    latency_ms: f64,
    ts_ms: u128,
    external_ref: Option<String>,
    tags: Option<String>,
}

// ------------------------------------------------------------------ the app

struct App {
    api_base: String,
    api: Client<HttpConnector, Body>,
    upstream: Client<HttpsConnector<HttpConnector>, Full<Bytes>>,
    token: HeaderValue,
    upstream_keys: HashMap<String, String>,
    key_ttl: Duration,
    keys: Mutex<HashMap<[u8; 32], Cached>>,
    queue: Mutex<VecDeque<Exchange>>,
    capacity: usize,
    dropped: AtomicU64,
    wake: Notify,
    stopping: AtomicBool,
}

impl App {
    async fn call_api(&self, path: &str, body: Vec<u8>) -> Result<(StatusCode, Bytes), BoxError> {
        let request = Request::post(format!("{}{path}", self.api_base))
            .header(CONTENT_TYPE, "application/json")
            .header("x-trimset-internal", self.token.clone())
            .body(full(body))?;
        let response =
            tokio::time::timeout(Duration::from_secs(5), self.api.request(request)).await??;
        let status = response.status();
        let bytes = response.into_body().collect().await?.to_bytes();
        Ok((status, bytes))
    }

    /// Who a key is, cached for `key_ttl`. When the server can't say, an answer up to
    /// ten minutes old stands (NFR-06); `None` means there is none.
    async fn principal(&self, key: &str) -> Option<Answer> {
        let digest: [u8; 32] = Sha256::digest(key.as_bytes()).into();
        let cached = {
            let keys = self.keys.lock().expect("key cache");
            keys.get(&digest)
                .map(|c| (c.at.elapsed(), c.answer.clone()))
        };
        if let Some((age, answer)) = &cached
            && *age < self.key_ttl
        {
            return Some(answer.clone());
        }
        let fresh = match self
            .call_api(
                "/internal/proxy/principal",
                json!({"key": key}).to_string().into_bytes(),
            )
            .await
        {
            Ok((status, body)) if status.is_success() => {
                match serde_json::from_slice::<PrincipalReply>(&body) {
                    Ok(PrincipalReply {
                        principal: Some(p), ..
                    }) => Some(Answer::Allowed(Arc::new(p))),
                    Ok(PrincipalReply {
                        denied: Some(d), ..
                    }) => Some(Answer::Denied(d)),
                    _ => None,
                }
            }
            _ => None,
        };
        match fresh {
            Some(answer) => {
                let mut keys = self.keys.lock().expect("key cache");
                if keys.len() > 100_000 {
                    keys.clear(); // a flood of made-up keys can't grow this forever
                }
                keys.insert(
                    digest,
                    Cached {
                        at: Instant::now(),
                        answer: answer.clone(),
                    },
                );
                Some(answer)
            }
            None => cached
                .filter(|(age, _)| *age < MAX_STALE)
                .map(|(_, answer)| answer),
        }
    }

    fn enqueue(&self, exchange: Exchange) {
        let mut queue = self.queue.lock().expect("queue");
        if queue.len() >= self.capacity {
            queue.pop_front();
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
        queue.push_back(exchange);
        if queue.len() >= BATCH {
            self.wake.notify_one();
        }
    }

    /// Sends queued exchanges to the server in batches. A failed batch goes back to the
    /// front of the queue and is retried with backoff; one the server refuses is dropped.
    async fn write_batches(self: Arc<Self>) {
        let mut backoff = Duration::from_millis(50);
        loop {
            tokio::select! {
                () = self.wake.notified() => {}
                () = tokio::time::sleep(Duration::from_millis(50)) => {}
            }
            loop {
                let batch: Vec<Exchange> = {
                    let mut queue = self.queue.lock().expect("queue");
                    let n = queue.len().min(BATCH);
                    queue.drain(..n).collect()
                };
                if batch.is_empty() {
                    break;
                }
                let body = serde_json::to_vec(&json!({"exchanges": &batch})).unwrap_or_default();
                match self.call_api("/internal/proxy/exchanges", body).await {
                    Ok((status, _)) if status.is_success() => backoff = Duration::from_millis(50),
                    Ok((status, _)) if status.is_client_error() => {
                        log(
                            "warning",
                            &format!("the server refused {} exchanges ({status})", batch.len()),
                        );
                    }
                    _ => {
                        {
                            let mut queue = self.queue.lock().expect("queue");
                            for exchange in batch.into_iter().rev() {
                                if queue.len() >= self.capacity {
                                    self.dropped.fetch_add(1, Ordering::Relaxed);
                                    continue;
                                }
                                queue.push_front(exchange);
                            }
                        }
                        log(
                            "warning",
                            &format!("the server can't take exchanges; retrying in {backoff:?}"),
                        );
                        tokio::time::sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(5));
                        break;
                    }
                }
            }
            let dropped = self.dropped.swap(0, Ordering::Relaxed);
            if dropped > 0 {
                log(
                    "warning",
                    &format!("the buffer was full: {dropped} exchanges not recorded"),
                );
            }
            if self.stopping.load(Ordering::Relaxed) && self.queue.lock().expect("queue").is_empty()
            {
                return;
            }
        }
    }
}

// ------------------------------------------------------------------ handlers

async fn handle(app: Arc<App>, request: Request<Incoming>) -> Result<Response<Body>, Infallible> {
    let response = match route(request.method(), request.uri().path(), request.headers()) {
        Route::Health => json_response(200, &json!({"status": "ok"}), "application/json"),
        Route::Internal => problem(404, "not-found", "Not found"),
        Route::Proxy { key, path, guess } => proxy(&app, request, key, path, guess).await,
        Route::Api => pass(&app, request).await,
    };
    Ok(response)
}

async fn proxy(
    app: &App,
    request: Request<Incoming>,
    key: Option<String>,
    path: &'static str,
    guess: Format,
) -> Response<Body> {
    let Some(key) = key.filter(|k| !k.is_empty()) else {
        let message = "A trimset key is required: /p/<key> in the base URL, or X-Trimset-Key";
        return fail(guess, 401, message, "missing_key");
    };
    let principal = match app.principal(&key).await {
        Some(Answer::Allowed(p)) => p,
        Some(Answer::Denied(d)) => return fail(guess, d.status, &d.message, &d.code),
        None => {
            return fail(
                guess,
                503,
                "trimset can't check this key right now",
                "unavailable",
            );
        }
    };
    let format = Format::parse(&principal.upstream.format);

    let (parts, body) = request.into_parts();
    let body = match Limited::new(body, principal.max_body_bytes).collect().await {
        Ok(collected) => collected.to_bytes(),
        Err(e) if e.is::<LengthLimitError>() => {
            let message = format!("The body is over {} bytes", principal.max_body_bytes);
            return fail(format, 413, &message, "too_large");
        }
        Err(_) => {
            return fail(
                format,
                400,
                "The request body could not be read",
                "bad_request",
            );
        }
    };
    let query = parts
        .uri
        .query()
        .map(|q| format!("?{q}"))
        .unwrap_or_default();
    let server_key = if principal.upstream.server_key {
        app.upstream_keys
            .get(&principal.upstream.name)
            .map(String::as_str)
    } else {
        None
    };
    let mut outgoing = Request::new(Full::new(body.clone()));
    *outgoing.method_mut() = parts.method.clone();
    *outgoing.headers_mut() = forward_headers(&parts.headers, server_key);
    match format!("{}{path}{query}", principal.upstream.url).parse::<Uri>() {
        Ok(uri) => *outgoing.uri_mut() = uri,
        Err(_) => {
            return fail(
                format,
                502,
                "The upstream could not be reached",
                "upstream_unreachable",
            );
        }
    }

    let started = Instant::now();
    let timeout = Duration::from_millis(principal.upstream.timeout_ms);
    let answered = tokio::time::timeout(timeout, async {
        let response = app.upstream.request(outgoing).await?;
        let (head, body) = response.into_parts();
        let bytes = Limited::new(body, MAX_RESPONSE).collect().await?.to_bytes();
        Ok::<_, BoxError>((head, bytes))
    })
    .await;
    let (head, content) = match answered {
        Err(_) => return fail(format, 504, "The upstream timed out", "upstream_timeout"),
        Ok(Err(e)) if e.is::<LengthLimitError>() => {
            return fail(
                format,
                502,
                "The upstream's response is too large",
                "upstream_too_large",
            );
        }
        Ok(Err(_)) => {
            log(
                "warning",
                &format!("upstream {} unreachable", principal.upstream.name),
            );
            return fail(
                format,
                502,
                "The upstream could not be reached",
                "upstream_unreachable",
            );
        }
        Ok(Ok(answer)) => answer,
    };
    let latency_ms = started.elapsed().as_secs_f64() * 1000.0;

    let mut response = Response::new(full(content.clone()));
    *response.status_mut() = head.status;
    returned_headers(&head.headers, response.headers_mut());
    if parts.method == Method::POST && head.status.is_success() {
        let exchange = capture(
            &principal,
            format,
            &parts.headers,
            &body,
            &content,
            latency_ms,
        );
        if let Some(exchange) = exchange {
            if let Ok(value) = HeaderValue::from_str(&exchange.decision_ids.join(",")) {
                response
                    .headers_mut()
                    .insert(HeaderName::from_static("x-trimset-decision-id"), value);
            }
            app.enqueue(exchange);
        }
    }
    response
}

/// One decision id per question, in question order, and the exchange to record. None
/// when the exchange can't be read; it is forwarded anyway and never logged.
fn capture(
    principal: &Principal,
    format: Format,
    headers: &HeaderMap,
    body: &Bytes,
    content: &Bytes,
    latency_ms: f64,
) -> Option<Exchange> {
    let content_type = headers
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let sent_bytes: &[u8] = if content_type
        .to_ascii_lowercase()
        .starts_with("multipart/form-data")
    {
        request_part(content_type, body)?
    } else {
        body
    };
    let request = std::str::from_utf8(sent_bytes).ok()?;
    let sent: Value = serde_json::from_str(request).ok()?;
    let questions = match format {
        Format::Systemone => sent.get("questions")?.as_object()?.len(),
        Format::OpenAi => sent.get("questions")?.as_array()?.len(),
    };
    let response = std::str::from_utf8(content).ok()?;
    if questions == 0 || !serde_json::from_str::<Value>(response).ok()?.is_object() {
        return None;
    }
    let header = |name: &str| {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .map(str::to_owned)
    };
    Some(Exchange {
        project_id: principal.project_id.clone(),
        format: format.name(),
        request: request.to_owned(),
        response: response.to_owned(),
        decision_ids: (0..questions)
            .map(|_| uuid::Uuid::now_v7().to_string())
            .collect(),
        latency_ms,
        ts_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_millis()),
        external_ref: header("x-trimset-external-ref"),
        tags: header("x-trimset-tags"),
    })
}

fn find(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    haystack
        .get(from..)?
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|i| i + from)
}

/// The `name` parameter of a Content-Disposition line, unquoted.
fn disposition_name(line: &str) -> Option<&str> {
    let (header, params) = line.split_once(':')?;
    if !header.trim().eq_ignore_ascii_case("content-disposition") {
        return None;
    }
    params.split(';').find_map(|param| {
        let (key, value) = param.split_once('=')?;
        key.trim()
            .eq_ignore_ascii_case("name")
            .then(|| value.trim().trim_matches('"'))
    })
}

/// The `request` part of a multipart image request (wire-formats.md §5). The image parts
/// are forwarded but never read here.
fn request_part<'a>(content_type: &str, body: &'a [u8]) -> Option<&'a [u8]> {
    let boundary = content_type.split(';').skip(1).find_map(|param| {
        let (key, value) = param.split_once('=')?;
        key.trim()
            .eq_ignore_ascii_case("boundary")
            .then(|| value.trim().trim_matches('"'))
    })?;
    if boundary.is_empty() {
        return None;
    }
    let delimiter = format!("--{boundary}");
    let delimiter = delimiter.as_bytes();
    let mut at = find(body, delimiter, 0)? + delimiter.len();
    loop {
        if body.get(at..at + 2) == Some(b"--".as_slice()) {
            return None; // the closing delimiter: no request part
        }
        let next = find(body, delimiter, at)?;
        let part = body[at..next].strip_prefix(b"\r\n")?;
        let part = part.strip_suffix(b"\r\n").unwrap_or(part);
        let blank = find(part, b"\r\n\r\n", 0)?;
        let head = std::str::from_utf8(&part[..blank]).ok()?;
        if head
            .split("\r\n")
            .any(|line| disposition_name(line) == Some("request"))
        {
            return Some(&part[blank + 4..]);
        }
        at = next + delimiter.len();
    }
}

/// Everything that isn't the proxy goes to the Python server, unchanged.
async fn pass(app: &App, request: Request<Incoming>) -> Response<Body> {
    let (mut parts, body) = request.into_parts();
    let target = parts.uri.path_and_query().map_or("/", |p| p.as_str());
    match format!("{}{target}", app.api_base).parse::<Uri>() {
        Ok(uri) => parts.uri = uri,
        Err(_) => return problem(400, "invalid-request", "Bad request target"),
    }
    for name in HOP_BY_HOP {
        parts.headers.remove(name);
    }
    let request = Request::from_parts(parts, body.map_err(BoxError::from).boxed());
    match app.api.request(request).await {
        Ok(response) => {
            let (mut head, body) = response.into_parts();
            for name in HOP_BY_HOP {
                head.headers.remove(name);
            }
            Response::from_parts(head, body.map_err(BoxError::from).boxed())
        }
        Err(_) => problem(502, "unavailable", "The API server can't be reached"),
    }
}

// ------------------------------------------------------------------ setup

fn log(level: &str, message: &str) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let line = json!({"ts": rfc3339(now), "level": level.to_uppercase(), "logger": "trimset.proxy", "message": message});
    println!("{line}");
}

/// UTC, to the millisecond, like the Python logs.
fn rfc3339(since_epoch: Duration) -> String {
    let secs = since_epoch.as_secs();
    let (days, rem) = (secs / 86_400, secs % 86_400);
    // Howard Hinnant's civil_from_days.
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}+00:00",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60,
        since_epoch.subsec_millis()
    )
}

/// The token for the server's internal endpoints: HMAC-SHA-256 of a fixed context under
/// TRIMSET_SECRET (internal.py).
fn internal_token(secret: &[u8]) -> String {
    use hmac::{KeyInit, Mac};
    let mut mac = hmac::Hmac::<Sha256>::new_from_slice(secret).expect("HMAC takes any key");
    mac.update(b"trimset-internal/1");
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

fn upstream_keys() -> HashMap<String, String> {
    env::vars()
        .filter_map(|(name, value)| {
            let upstream = name
                .strip_prefix("TRIMSET_UPSTREAM_")?
                .strip_suffix("_KEY")?;
            let value = value.trim();
            (!value.is_empty()).then(|| (upstream.to_ascii_lowercase(), value.to_owned()))
        })
        .collect()
}

fn setting<T: std::str::FromStr>(name: &str, default: T) -> T {
    env::var(name)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

struct Config {
    api_base: String,
    secret: String,
    key_ttl: Duration,
    capacity: usize,
    upstream_keys: HashMap<String, String>,
}

fn build_app(config: Config) -> Result<Arc<App>, BoxError> {
    let tls = rustls::crypto::ring::default_provider();
    let https = hyper_rustls::HttpsConnectorBuilder::new()
        .with_provider_and_webpki_roots(tls)?
        .https_or_http()
        .enable_http1()
        .build();
    Ok(Arc::new(App {
        api_base: config.api_base.trim_end_matches('/').to_owned(),
        // No idle connections to the server: each request opens its own, so the kernel
        // spreads them over the server's processes. Kept-alive ones funnelled everything
        // into one process, and ingest's p99 went from 17 ms to 24 ms.
        api: Client::builder(TokioExecutor::new())
            .pool_max_idle_per_host(0)
            .build_http(),
        upstream: Client::builder(TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(60))
            .build(https),
        token: HeaderValue::from_str(&internal_token(config.secret.as_bytes()))?,
        upstream_keys: config.upstream_keys,
        key_ttl: config.key_ttl,
        keys: Mutex::new(HashMap::new()),
        queue: Mutex::new(VecDeque::new()),
        capacity: config.capacity.max(BATCH),
        dropped: AtomicU64::new(0),
        wake: Notify::new(),
        stopping: AtomicBool::new(false),
    }))
}

async fn serve(listener: TcpListener, app: Arc<App>, shutdown: impl Future<Output = ()>) {
    tokio::pin!(shutdown);
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((stream, _)) = accepted else { continue };
                let _ = stream.set_nodelay(true);
                let app = Arc::clone(&app);
                tokio::spawn(async move {
                    let service = service_fn(move |request| handle(Arc::clone(&app), request));
                    let _ = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), service)
                        .await;
                });
            }
            () = &mut shutdown => break,
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), BoxError> {
    let secret = env::var("TRIMSET_SECRET").unwrap_or_default();
    if secret.len() < 32 {
        log(
            "error",
            "TRIMSET_SECRET must be at least 32 characters (openssl rand -hex 32)",
        );
        std::process::exit(1);
    }
    let host = setting("TRIMSET_PROXY_HOST", "0.0.0.0".to_owned());
    let port: u16 = setting("TRIMSET_PROXY_PORT", 8800);
    let app = build_app(Config {
        api_base: setting("TRIMSET_API_URL", "http://127.0.0.1:8810".to_owned()),
        secret,
        key_ttl: Duration::from_secs_f64(
            setting("TRIMSET_KEY_CACHE_TTL_S", 30.0_f64).clamp(0.0, 60.0),
        ),
        capacity: setting("TRIMSET_PROXY_BUFFER", 50_000),
        upstream_keys: upstream_keys(),
    })?;
    let writer = tokio::spawn(Arc::clone(&app).write_batches());

    let address: SocketAddr = format!("{host}:{port}").parse()?;
    let listener = TcpListener::bind(address).await?;
    log(
        "info",
        &format!("trimset proxy on {address}, API at {}", app.api_base),
    );
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    serve(listener, Arc::clone(&app), async move {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {}
            _ = term.recv() => {}
        }
    })
    .await;

    // Stop taking requests; give the writer a few seconds to hand over what it holds.
    app.stopping.store(true, Ordering::Relaxed);
    app.wake.notify_one();
    let _ = tokio::time::timeout(Duration::from_secs(5), writer).await;
    log("info", "trimset proxy stopped");
    Ok(())
}

// ------------------------------------------------------------------ tests

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(pairs: &[(&'static str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.append(*name, HeaderValue::from_str(value).unwrap());
        }
        map
    }

    const KEY: &str = "tsp_0123456789abcdefghijABCDEFGHIJ01";

    #[test]
    fn routes() {
        let none = HeaderMap::new();
        let with_key = headers(&[("x-trimset-key", KEY)]);
        assert_eq!(
            route(&Method::POST, "/v1/systemone", &with_key),
            Route::Proxy {
                key: Some(KEY.into()),
                path: "/v1/systemone",
                guess: Format::Systemone
            }
        );
        assert_eq!(
            route(&Method::POST, &format!("/p/{KEY}/v1/decisions"), &none),
            Route::Proxy {
                key: Some(KEY.into()),
                path: "/v1/decisions",
                guess: Format::OpenAi
            }
        );
        assert_eq!(
            route(&Method::GET, &format!("/p/{KEY}/v1/models"), &none),
            Route::Proxy {
                key: Some(KEY.into()),
                path: "/v1/models",
                guess: Format::Systemone
            }
        );
        assert_eq!(
            route(&Method::POST, "/v1/decisions", &none),
            Route::Proxy {
                key: None,
                path: "/v1/decisions",
                guess: Format::OpenAi
            }
        );
        assert_eq!(route(&Method::GET, "/v1/systemone", &none), Route::Api);
        assert_eq!(route(&Method::POST, "/p//v1/systemone", &none), Route::Api);
        assert_eq!(
            route(&Method::POST, &format!("/p/{KEY}/v1/other"), &none),
            Route::Api
        );
        assert_eq!(route(&Method::POST, "/v1/decisions:log", &none), Route::Api);
        assert_eq!(
            route(&Method::GET, "/internal/proxy/principal", &none),
            Route::Internal
        );
        assert_eq!(route(&Method::GET, "/internal", &none), Route::Internal);
        assert_eq!(route(&Method::GET, "/health", &none), Route::Health);
    }

    #[test]
    fn keys_look_like_keys() {
        assert!(looks_like_key(KEY));
        assert!(looks_like_key("tsa_0123456789abcdefghijABCDEFGHIJ01"));
        assert!(!looks_like_key("tsx_0123456789abcdefghijABCDEFGHIJ01"));
        assert!(!looks_like_key("tsp_0123456789abcdefghijABCDEFGHIJ0"));
        assert!(!looks_like_key("tsp_0123456789abcdefghijABCDEFGHIJ0!"));
        assert!(!looks_like_key("sk-live-0123456789abcdefghijABCDEFGHI"));
    }

    #[test]
    fn forwarded_headers_drop_trimset_and_hop_by_hop() {
        let incoming = headers(&[
            ("authorization", "Bearer sk-app-own"),
            ("x-trimset-key", KEY),
            ("x-trimset-tags", "a=b"),
            ("connection", "keep-alive"),
            ("host", "trimset.example.com"),
            ("accept-encoding", "gzip"),
            ("content-type", "application/json"),
            ("typesafe-danger-zone-x", "1"),
        ]);
        let out = forward_headers(&incoming, None);
        assert_eq!(out.get("authorization").unwrap(), "Bearer sk-app-own");
        assert_eq!(out.get("content-type").unwrap(), "application/json");
        assert_eq!(out.get("typesafe-danger-zone-x").unwrap(), "1");
        for gone in [
            "x-trimset-key",
            "x-trimset-tags",
            "connection",
            "host",
            "accept-encoding",
        ] {
            assert!(out.get(gone).is_none(), "{gone} was forwarded");
        }
    }

    #[test]
    fn a_trimset_key_never_reaches_the_upstream() {
        let incoming = headers(&[("authorization", &format!("Bearer {KEY}"))]);
        assert!(
            forward_headers(&incoming, None)
                .get("authorization")
                .is_none()
        );
        let bare = headers(&[("authorization", KEY)]);
        assert!(forward_headers(&bare, None).get("authorization").is_none());
    }

    #[test]
    fn the_operators_key_replaces_the_apps() {
        let incoming = headers(&[("authorization", "Bearer sk-app-own")]);
        let out = forward_headers(&incoming, Some("sk-operator"));
        assert_eq!(out.get_all("authorization").iter().count(), 1);
        assert_eq!(out.get("authorization").unwrap(), "Bearer sk-operator");
    }

    #[test]
    fn returned_headers_drop_trimset_and_lengths() {
        let from = headers(&[
            ("content-type", "application/json"),
            ("content-length", "12"),
            ("x-trimset-decision-id", "spoofed"),
            ("x-ollaya-load-duration", "3ms"),
        ]);
        let mut to = HeaderMap::new();
        returned_headers(&from, &mut to);
        assert_eq!(to.len(), 2);
        assert!(to.get("x-trimset-decision-id").is_none());
    }

    #[tokio::test]
    async fn errors_use_the_upstreams_format() {
        let body = |r: Response<Body>| async move {
            let bytes = r.into_body().collect().await.unwrap().to_bytes();
            serde_json::from_slice::<Value>(&bytes).unwrap()
        };
        let openai = fail(Format::OpenAi, 401, "no key", "missing_key");
        assert_eq!(openai.status(), 401);
        assert_eq!(
            body(openai).await,
            json!({"error": {"message": "no key", "type": "trimset_error", "code": "missing_key", "param": null}})
        );
        assert_eq!(
            body(fail(Format::Systemone, 502, "down", "x")).await,
            json!({"detail": "down"})
        );
    }

    fn principal() -> Principal {
        Principal {
            project_id: "p".into(),
            upstream: Upstream {
                name: "mock".into(),
                url: "http://mock".into(),
                format: "systemone".into(),
                server_key: false,
                timeout_ms: 1000,
            },
            max_body_bytes: 1024,
        }
    }

    #[test]
    fn one_decision_id_per_question() {
        let p = principal();
        let sent =
            Bytes::from(r#"{"model":"m","questions":{"a":{"type":"noul"},"b":{"type":"noul"}}}"#);
        let answer = Bytes::from(r#"{"model":"m","answers":{}}"#);
        let meta = headers(&[
            ("x-trimset-external-ref", "rf-1"),
            ("x-trimset-tags", "a=b"),
        ]);
        let ex = capture(&p, Format::Systemone, &meta, &sent, &answer, 3.0).unwrap();
        assert_eq!(ex.decision_ids.len(), 2);
        assert_ne!(ex.decision_ids[0], ex.decision_ids[1]);
        assert_eq!(ex.external_ref.as_deref(), Some("rf-1"));
        assert_eq!(ex.tags.as_deref(), Some("a=b"));

        let openai = Bytes::from(r#"{"model":"m","questions":[{"type":"predicate"}]}"#);
        assert_eq!(
            capture(&p, Format::OpenAi, &meta, &openai, &answer, 1.0)
                .unwrap()
                .decision_ids
                .len(),
            1
        );
    }

    #[test]
    fn unreadable_exchanges_are_forwarded_but_not_recorded() {
        let p = principal();
        let none = HeaderMap::new();
        let ok = Bytes::from(r#"{"model":"m","questions":{"a":{}}}"#);
        let answer = Bytes::from(r#"{"model":"m"}"#);
        assert!(
            capture(
                &p,
                Format::Systemone,
                &none,
                &Bytes::from("not json"),
                &answer,
                1.0
            )
            .is_none()
        );
        assert!(capture(&p, Format::Systemone, &none, &ok, &Bytes::from("[]"), 1.0).is_none());
        assert!(capture(&p, Format::OpenAi, &none, &ok, &answer, 1.0).is_none()); // wrong shape
        let empty = Bytes::from(r#"{"model":"m","questions":{}}"#);
        assert!(capture(&p, Format::Systemone, &none, &empty, &answer, 1.0).is_none());
        let multipart = headers(&[("content-type", "multipart/form-data; boundary=x")]);
        assert!(capture(&p, Format::Systemone, &multipart, &ok, &answer, 1.0).is_none());
    }

    #[test]
    fn the_internal_token_matches_the_python_server() {
        // hmac.new(b"test-secret-0123456789abcdef0123456789", b"trimset-internal/1", sha256)
        assert_eq!(
            internal_token(b"test-secret-0123456789abcdef0123456789"),
            "fe6fd7c4ee6ef149bec655e14c46ca72d7a24fa078f563ff1344f8a68d1fa17b"
        );
    }

    #[test]
    fn timestamps() {
        assert_eq!(
            rfc3339(Duration::from_millis(0)),
            "1970-01-01T00:00:00.000+00:00"
        );
        assert_eq!(
            rfc3339(Duration::from_millis(1_762_867_200_123)),
            "2025-11-11T13:20:00.123+00:00"
        );
    }

    // -------------------------------------------------------------- end to end
    //
    // The real proxy on a local port, between a fake upstream and a fake Python server.

    use std::sync::atomic::AtomicU64;

    const SECRET: &str = "end-to-end-secret-0123456789abcdef";
    const PROXY_KEY: &str = "tsp_0123456789abcdefghijABCDEFGHIJ01";
    const REVIEW_KEY: &str = "tsr_0123456789abcdefghijABCDEFGHIJ01";
    const LOST_KEY: &str = "tsp_upstreamIsDownabcdefghijABCDEFG1";
    const SYSTEMONE: &str = r#"{"model":"m-latest","state":{"x":1},"questions":{"a":{"type":"noul"},"b":{"type":"noul"}}}"#;
    const ANSWER: &str = r#"{"model":"m-1.0.0","answers":{"a":{"type":"noul","noul":0.8}}}"#;

    type Seen = Arc<Mutex<Vec<(Method, String, HeaderMap, Bytes)>>>;

    struct World {
        proxy: SocketAddr,
        upstream_seen: Seen,
        api_seen: Seen,
        exchanges: Arc<Mutex<Vec<Value>>>,
        principal_calls: Arc<AtomicU64>,
        api_down: Arc<AtomicBool>,
    }

    async fn fake<F, Fut>(handler: F) -> SocketAddr
    where
        F: Fn(Request<Incoming>) -> Fut + Clone + Send + Sync + 'static,
        Fut: Future<Output = Response<Full<Bytes>>> + Send + 'static,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let handler = handler.clone();
                tokio::spawn(async move {
                    let service = service_fn(move |request| {
                        let handler = handler.clone();
                        async move { Ok::<_, Infallible>(handler(request).await) }
                    });
                    let _ = http1::Builder::new()
                        .serve_connection(TokioIo::new(stream), service)
                        .await;
                });
            }
        });
        address
    }

    fn reply(status: u16, body: &str) -> Response<Full<Bytes>> {
        let mut response = Response::new(Full::new(Bytes::from(body.to_owned())));
        *response.status_mut() = StatusCode::from_u16(status).unwrap();
        response
            .headers_mut()
            .insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        response
    }

    async fn remember(
        seen: &Seen,
        request: Request<Incoming>,
    ) -> (Method, String, HeaderMap, Bytes) {
        let (parts, body) = request.into_parts();
        let bytes = body.collect().await.unwrap().to_bytes();
        let entry = (parts.method, parts.uri.to_string(), parts.headers, bytes);
        seen.lock().unwrap().push(entry.clone());
        entry
    }

    async fn world(format: &'static str, server_key: bool, key_ttl: Duration) -> World {
        let upstream_seen: Seen = Arc::default();
        let seen = Arc::clone(&upstream_seen);
        let upstream = fake(move |request: Request<Incoming>| {
            let seen = Arc::clone(&seen);
            async move {
                let (_, path, headers, _) = remember(&seen, request).await;
                match headers.get("x-test").and_then(|v| v.to_str().ok()) {
                    Some("slow") => {
                        tokio::time::sleep(Duration::from_millis(800)).await;
                        reply(200, ANSWER)
                    }
                    Some("error") => reply(500, r#"{"detail":"the model failed"}"#),
                    Some("redirect") => {
                        let mut r = reply(302, "{}");
                        r.headers_mut().insert(
                            "location",
                            HeaderValue::from_static("http://elsewhere.test/"),
                        );
                        r
                    }
                    _ if path.ends_with("/v1/models") => reply(200, r#"{"data":[]}"#),
                    _ => {
                        let mut r = reply(200, ANSWER);
                        r.headers_mut()
                            .insert("x-trimset-decision-id", HeaderValue::from_static("spoofed"));
                        r.headers_mut()
                            .insert("x-upstream", HeaderValue::from_static("yes"));
                        r
                    }
                }
            }
        })
        .await;

        let api_seen: Seen = Arc::default();
        let exchanges: Arc<Mutex<Vec<Value>>> = Arc::default();
        let principal_calls = Arc::new(AtomicU64::new(0));
        let api_down = Arc::new(AtomicBool::new(false));
        let (seen, got, calls, down) = (
            Arc::clone(&api_seen),
            Arc::clone(&exchanges),
            Arc::clone(&principal_calls),
            Arc::clone(&api_down),
        );
        let api = fake(move |request: Request<Incoming>| {
            let (seen, got, calls, down) = (
                Arc::clone(&seen),
                Arc::clone(&got),
                Arc::clone(&calls),
                Arc::clone(&down),
            );
            async move {
                let (_, path, headers, body) = remember(&seen, request).await;
                if !path.starts_with("/internal/") {
                    return reply(200, &json!({"passed": path}).to_string());
                }
                let token = headers
                    .get("x-trimset-internal")
                    .and_then(|v| v.to_str().ok());
                if token != Some(internal_token(SECRET.as_bytes()).as_str()) {
                    return reply(404, "{}");
                }
                if down.load(Ordering::Relaxed) {
                    return reply(503, "{}");
                }
                let sent: Value = serde_json::from_slice(&body).unwrap();
                if path == "/internal/proxy/exchanges" {
                    got.lock()
                        .unwrap()
                        .extend(sent["exchanges"].as_array().unwrap().iter().cloned());
                    return reply(202, r#"{"queued":1}"#);
                }
                calls.fetch_add(1, Ordering::Relaxed);
                let url = match sent["key"].as_str() {
                    Some(PROXY_KEY) => format!("http://{upstream}"),
                    Some(LOST_KEY) => "http://127.0.0.1:1".to_owned(),
                    Some(REVIEW_KEY) => {
                        let denied = json!({"denied": {"status": 403, "code": "forbidden",
                            "message": "This trimset key can't use the proxy"}});
                        return reply(200, &denied.to_string());
                    }
                    _ => {
                        let denied = json!({"denied": {"status": 401, "code": "invalid_key",
                            "message": "Unknown or revoked trimset key"}});
                        return reply(200, &denied.to_string());
                    }
                };
                let principal = json!({"principal": {
                    "key_id": "k", "project_id": "p1", "max_body_bytes": 4096,
                    "upstream": {"name": "up", "url": url, "format": format,
                                 "server_key": server_key, "timeout_ms": 300}}});
                reply(200, &principal.to_string())
            }
        })
        .await;

        let app = build_app(Config {
            api_base: format!("http://{api}"),
            secret: SECRET.to_owned(),
            key_ttl,
            capacity: 50_000,
            upstream_keys: HashMap::from([("up".to_owned(), "sk-operator".to_owned())]),
        })
        .unwrap();
        tokio::spawn(Arc::clone(&app).write_batches());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let proxy = listener.local_addr().unwrap();
        tokio::spawn(serve(listener, Arc::clone(&app), std::future::pending()));
        World {
            proxy,
            upstream_seen,
            api_seen,
            exchanges,
            principal_calls,
            api_down,
        }
    }

    async fn send(
        world: &World,
        method: Method,
        path: &str,
        headers: &[(&'static str, &str)],
        body: &str,
    ) -> (StatusCode, HeaderMap, Bytes) {
        let client: Client<HttpConnector, Full<Bytes>> =
            Client::builder(TokioExecutor::new()).build_http();
        let mut request = Request::new(Full::new(Bytes::from(body.to_owned())));
        *request.method_mut() = method;
        *request.uri_mut() = format!("http://{}{path}", world.proxy).parse().unwrap();
        for (name, value) in headers {
            request
                .headers_mut()
                .insert(*name, HeaderValue::from_str(value).unwrap());
        }
        let response = client.request(request).await.unwrap();
        let (head, body) = response.into_parts();
        (
            head.status,
            head.headers,
            body.collect().await.unwrap().to_bytes(),
        )
    }

    async fn recorded(world: &World, n: usize) -> Vec<Value> {
        for _ in 0..100 {
            if world.exchanges.lock().unwrap().len() >= n {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        world.exchanges.lock().unwrap().clone()
    }

    fn json_of(body: &Bytes) -> Value {
        serde_json::from_slice(body).unwrap()
    }

    #[tokio::test]
    async fn path_mode_forwards_unchanged_and_records_the_exchange() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let path = format!("/p/{PROXY_KEY}/v1/systemone?trace=1");
        let headers = [
            ("authorization", "Bearer sk-app-own"),
            ("x-trimset-external-ref", "rf-1"),
            ("x-trimset-tags", "channel=web"),
            ("content-type", "application/json"),
        ];
        let (status, out, body) = send(&w, Method::POST, &path, &headers, SYSTEMONE).await;
        assert_eq!(status, 200);
        assert_eq!(
            body,
            Bytes::from(ANSWER),
            "the upstream's body comes back unchanged"
        );
        assert_eq!(out.get("x-upstream").unwrap(), "yes");
        let ids: Vec<&str> = out
            .get("x-trimset-decision-id")
            .unwrap()
            .to_str()
            .unwrap()
            .split(',')
            .collect();
        assert_eq!(ids.len(), 2, "one id per question, never the upstream's");

        let seen = w.upstream_seen.lock().unwrap().clone();
        let (method, uri, sent_headers, sent_body) = &seen[0];
        assert_eq!(
            (method, uri.as_str()),
            (&Method::POST, "/v1/systemone?trace=1")
        );
        assert_eq!(sent_body, &Bytes::from(SYSTEMONE));
        assert_eq!(
            sent_headers.get("authorization").unwrap(),
            "Bearer sk-app-own"
        );
        assert!(
            sent_headers
                .keys()
                .all(|k| !k.as_str().starts_with("x-trimset-"))
        );

        let exchanges = recorded(&w, 1).await;
        assert_eq!(exchanges.len(), 1);
        let ex = &exchanges[0];
        assert_eq!(ex["project_id"], "p1");
        assert_eq!(ex["format"], "systemone");
        assert_eq!(ex["request"], SYSTEMONE);
        assert_eq!(ex["response"], ANSWER);
        assert_eq!(ex["decision_ids"], json!(ids));
        assert_eq!(
            (ex["external_ref"].as_str(), ex["tags"].as_str()),
            (Some("rf-1"), Some("channel=web"))
        );
    }

    #[tokio::test]
    async fn header_mode_works_and_a_trimset_key_never_leaves() {
        let w = world("openai-decisions", false, Duration::from_secs(30)).await;
        let headers = [("x-trimset-key", PROXY_KEY), ("authorization", PROXY_KEY)];
        let body = r#"{"model":"gpt","questions":[{"type":"predicate"}]}"#;
        let (status, out, _) = send(&w, Method::POST, "/v1/decisions", &headers, body).await;
        assert_eq!(status, 200);
        assert_eq!(
            out.get("x-trimset-decision-id")
                .unwrap()
                .to_str()
                .unwrap()
                .split(',')
                .count(),
            1
        );
        let seen = w.upstream_seen.lock().unwrap().clone();
        assert!(seen[0].2.get("authorization").is_none());
        assert!(seen[0].2.get("x-trimset-key").is_none());
    }

    #[tokio::test]
    async fn the_operators_key_replaces_the_apps_end_to_end() {
        let w = world("systemone", true, Duration::from_secs(30)).await;
        let headers = [
            ("x-trimset-key", PROXY_KEY),
            ("authorization", "Bearer sk-app-own"),
        ];
        send(&w, Method::POST, "/v1/systemone", &headers, SYSTEMONE).await;
        let seen = w.upstream_seen.lock().unwrap().clone();
        assert_eq!(
            seen[0].2.get("authorization").unwrap(),
            "Bearer sk-operator"
        );
    }

    #[tokio::test]
    async fn trimsets_own_errors_use_the_upstreams_format() {
        let w = world("openai-decisions", false, Duration::from_secs(30)).await;
        let (status, _, body) = send(&w, Method::POST, "/v1/decisions", &[], "{}").await;
        assert_eq!(status, 401);
        assert_eq!(json_of(&body)["error"]["code"], "missing_key");
        let (status, _, body) = send(&w, Method::POST, "/v1/systemone", &[], "{}").await;
        assert_eq!(
            (status, json_of(&body)),
            (
                StatusCode::UNAUTHORIZED,
                json!({
            "detail": "A trimset key is required: /p/<key> in the base URL, or X-Trimset-Key"})
            )
        );

        let unknown = "tsp_unknownunknownunknownunknownun1";
        let (status, _, body) = send(
            &w,
            Method::POST,
            &format!("/p/{unknown}/v1/decisions"),
            &[],
            "{}",
        )
        .await;
        assert_eq!(
            (status, json_of(&body)["error"]["code"].clone()),
            (StatusCode::UNAUTHORIZED, json!("invalid_key"))
        );
        let (status, _, _) = send(
            &w,
            Method::POST,
            &format!("/p/{REVIEW_KEY}/v1/decisions"),
            &[],
            "{}",
        )
        .await;
        assert_eq!(status, 403);
        assert!(
            w.upstream_seen.lock().unwrap().is_empty(),
            "nothing reached the upstream"
        );
    }

    #[tokio::test]
    async fn an_unreachable_or_slow_upstream_gets_502_or_504() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let (status, _, body) = send(
            &w,
            Method::POST,
            &format!("/p/{LOST_KEY}/v1/systemone"),
            &[],
            SYSTEMONE,
        )
        .await;
        assert_eq!(
            (status, json_of(&body)),
            (
                StatusCode::BAD_GATEWAY,
                json!({"detail": "The upstream could not be reached"})
            )
        );
        let slow = [("x-test", "slow")];
        let (status, _, body) = send(
            &w,
            Method::POST,
            &format!("/p/{PROXY_KEY}/v1/systemone"),
            &slow,
            SYSTEMONE,
        )
        .await;
        assert_eq!(
            (status, json_of(&body)),
            (
                StatusCode::GATEWAY_TIMEOUT,
                json!({"detail": "The upstream timed out"})
            )
        );
    }

    #[tokio::test]
    async fn upstream_errors_and_redirects_pass_through_unrecorded() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let path = format!("/p/{PROXY_KEY}/v1/systemone");
        let (status, out, body) =
            send(&w, Method::POST, &path, &[("x-test", "error")], SYSTEMONE).await;
        assert_eq!(
            (status, body),
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                Bytes::from(r#"{"detail":"the model failed"}"#)
            )
        );
        assert!(out.get("x-trimset-decision-id").is_none());
        let (status, out, _) = send(
            &w,
            Method::POST,
            &path,
            &[("x-test", "redirect")],
            SYSTEMONE,
        )
        .await;
        assert_eq!(status, 302, "redirects are never followed (ADR 0011)");
        assert_eq!(out.get("location").unwrap(), "http://elsewhere.test/");
        assert_eq!(w.upstream_seen.lock().unwrap().len(), 2);
        tokio::time::sleep(Duration::from_millis(150)).await;
        assert!(w.exchanges.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn a_body_over_the_projects_limit_is_refused_before_forwarding() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let big = "x".repeat(5000);
        let (status, _, body) = send(
            &w,
            Method::POST,
            &format!("/p/{PROXY_KEY}/v1/systemone"),
            &[],
            &big,
        )
        .await;
        assert_eq!(
            (status, json_of(&body)),
            (
                StatusCode::PAYLOAD_TOO_LARGE,
                json!({"detail": "The body is over 4096 bytes"})
            )
        );
        assert!(w.upstream_seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn models_pass_through_unrecorded() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let (status, out, body) = send(
            &w,
            Method::GET,
            &format!("/p/{PROXY_KEY}/v1/models"),
            &[],
            "",
        )
        .await;
        assert_eq!(
            (status, body),
            (StatusCode::OK, Bytes::from(r#"{"data":[]}"#))
        );
        assert!(out.get("x-trimset-decision-id").is_none());
    }

    #[tokio::test]
    async fn a_multipart_request_records_its_request_part_and_forwards_the_image() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let image = "\u{1}\u{2}--not-the-boundary\r\n\r\nbinary";
        let body = format!(
            "--XyZ\r\nContent-Disposition: form-data; name=\"image\"; filename=\"a.png\"\r\nContent-Type: image/png\r\n\r\n{image}\r\n--XyZ\r\nContent-Disposition: form-data; name=\"request\"\r\nContent-Type: application/json\r\n\r\n{SYSTEMONE}\r\n--XyZ--\r\n"
        );
        let headers = [("content-type", "multipart/form-data; boundary=\"XyZ\"")];
        let (status, out, _) = send(
            &w,
            Method::POST,
            &format!("/p/{PROXY_KEY}/v1/systemone"),
            &headers,
            &body,
        )
        .await;
        assert_eq!(status, 200);
        assert!(out.get("x-trimset-decision-id").is_some());
        assert_eq!(
            w.upstream_seen.lock().unwrap()[0].3,
            Bytes::from(body),
            "forwarded byte for byte"
        );
        assert_eq!(recorded(&w, 1).await[0]["request"], SYSTEMONE);
    }

    #[test]
    fn multipart_without_a_request_part_records_nothing() {
        let body = b"--b\r\nContent-Disposition: form-data; name=\"image\"\r\n\r\nxx\r\n--b--\r\n";
        assert!(request_part("multipart/form-data; boundary=b", body).is_none());
        assert!(request_part("multipart/form-data", body).is_none());
        let named = b"--b\r\nContent-Disposition: form-data; filename=\"request\"; name=\"x\"\r\n\r\n{}\r\n--b--\r\n";
        assert!(request_part("multipart/form-data; boundary=b", named).is_none());
    }

    #[tokio::test]
    async fn everything_else_goes_to_the_server_and_internal_paths_never_do() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let headers = [(
            "authorization",
            "Bearer tsa_0123456789abcdefghijABCDEFGHIJ01",
        )];
        let (status, _, body) = send(&w, Method::GET, "/v1/admin/keys?limit=5", &headers, "").await;
        assert_eq!(
            (status, json_of(&body)),
            (StatusCode::OK, json!({"passed": "/v1/admin/keys?limit=5"}))
        );
        let api = w.api_seen.lock().unwrap().clone();
        assert_eq!(
            api.last().unwrap().2.get("authorization").unwrap(),
            headers[0].1
        );

        let before = w.api_seen.lock().unwrap().len();
        let (status, _, _) = send(
            &w,
            Method::POST,
            "/internal/proxy/principal",
            &[],
            r#"{"key":"x"}"#,
        )
        .await;
        assert_eq!(status, 404);
        assert_eq!(w.api_seen.lock().unwrap().len(), before, "never forwarded");
        let (status, _, body) = send(&w, Method::GET, "/health", &[], "").await;
        assert_eq!(
            (status, json_of(&body)),
            (StatusCode::OK, json!({"status": "ok"}))
        );
    }

    #[tokio::test]
    async fn key_answers_are_cached_and_stand_while_the_server_is_down() {
        let w = world("systemone", false, Duration::ZERO).await;
        let path = format!("/p/{PROXY_KEY}/v1/systemone");
        send(&w, Method::POST, &path, &[], SYSTEMONE).await;
        w.api_down.store(true, Ordering::Relaxed);
        let (status, _, _) = send(&w, Method::POST, &path, &[], SYSTEMONE).await;
        assert_eq!(
            status, 200,
            "the last answer stands for up to ten minutes (NFR-06)"
        );
        let unknown = format!("/p/{LOST_KEY}/v1/systemone");
        let (status, _, body) = send(&w, Method::POST, &unknown, &[], SYSTEMONE).await;
        assert_eq!(
            (status, json_of(&body)["detail"].clone()),
            (
                StatusCode::SERVICE_UNAVAILABLE,
                json!("trimset can't check this key right now")
            )
        );

        let cached = world("systemone", false, Duration::from_secs(30)).await;
        for _ in 0..3 {
            send(&cached, Method::POST, &path, &[], SYSTEMONE).await;
        }
        assert_eq!(cached.principal_calls.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn exchanges_wait_while_the_server_is_down_and_a_full_buffer_drops_the_oldest() {
        let w = world("systemone", false, Duration::from_secs(30)).await;
        let path = format!("/p/{PROXY_KEY}/v1/systemone");
        send(&w, Method::POST, &path, &[], SYSTEMONE).await; // caches the key
        w.api_down.store(true, Ordering::Relaxed);
        send(&w, Method::POST, &path, &[], SYSTEMONE).await;
        tokio::time::sleep(Duration::from_millis(200)).await;
        let held = w.exchanges.lock().unwrap().len();
        w.api_down.store(false, Ordering::Relaxed);
        assert_eq!(
            recorded(&w, 2).await.len(),
            2,
            "held ({held} went in), then sent"
        );

        let exchange = |n: u32| Exchange {
            project_id: format!("p{n}"),
            format: "systemone",
            request: String::new(),
            response: String::new(),
            decision_ids: vec![],
            latency_ms: 0.0,
            ts_ms: 0,
            external_ref: None,
            tags: None,
        };
        let small = build_app(Config {
            api_base: "http://127.0.0.1:1".into(),
            secret: SECRET.into(),
            key_ttl: Duration::ZERO,
            capacity: 0, // raised to one batch
            upstream_keys: HashMap::new(),
        })
        .unwrap();
        for n in 0..=(BATCH as u32) {
            small.enqueue(exchange(n));
        }
        let queue = small.queue.lock().unwrap();
        assert_eq!(
            (queue.len(), small.dropped.load(Ordering::Relaxed)),
            (BATCH, 1)
        );
        assert_eq!(queue.front().unwrap().project_id, "p1", "the oldest went");
    }
}
