//! One HTTP request under the reader policy (spec/refkv.md §6.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>), as refkv's Python
//! reader sends it (python/src/refkv/reader.py `_send`): a GET that does not follow redirects, its
//! connection opened to an address the policy allows. The host is resolved by the agent's
//! resolver (`Checked`), which drops every address rule 3 refuses, so a connection only ever
//! reaches a checked address, and a name that resolves to refused addresses only is refused. A
//! request that would go through a proxy is refused, since the address the proxy reaches cannot
//! be checked. Redirects and retries are the callers' (`transport.rs`, `store.rs`), which check
//! each redirect target before they request it.

use crate::policy::Policy;
use crate::Fail;
use std::fmt;
use std::sync::Arc;
use std::time::Duration;
use ureq::config::Config;
use ureq::http::{HeaderMap, Uri};
use ureq::unversioned::resolver::{DefaultResolver, ResolvedSocketAddrs, Resolver};
use ureq::unversioned::transport::{DefaultConnector, NextTimeout};

pub const UA: &str = "refkv-virtualize";
pub const REDIRECTS: [u16; 5] = [301, 302, 303, 307, 308];

/// A resolver error: rule 3 refused every address of the host.
#[derive(Debug)]
struct Refused(String);

impl fmt::Display for Refused {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Refused {}

/// The default resolver, keeping the addresses the policy allows.
#[derive(Debug)]
struct Checked {
    policy: Policy,
}

impl Resolver for Checked {
    fn resolve(&self, uri: &Uri, config: &Config, timeout: NextTimeout) -> Result<ResolvedSocketAddrs, ureq::Error> {
        let all = DefaultResolver::default().resolve(uri, config, timeout)?;
        if self.policy.allow_private_hosts {
            return Ok(all);
        }
        let mut kept = self.empty();
        let mut refused = None;
        for &a in all.iter() {
            match self.policy.check_address(&uri.to_string(), a.ip()) {
                Ok(()) => kept.push(a),
                Err(e) => refused = refused.or(Some(e)),
            }
        }
        match refused {
            Some(e) if kept.is_empty() => Err(ureq::Error::Other(Box::new(Refused(e)))),
            _ => Ok(kept),
        }
    }
}

/// A response: its status, its headers, and its body when it was read.
pub struct Response {
    pub status: u16,
    pub headers: HeaderMap,
    pub body: Vec<u8>,
}

impl Response {
    /// The first value of the header `name`, if any.
    pub fn header(&self, name: &str) -> Option<String> {
        self.headers.get(name).map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned())
    }

    pub fn all(&self, name: &str) -> Vec<String> {
        self.headers.get_all(name).iter().map(|v| String::from_utf8_lossy(v.as_bytes()).into_owned()).collect()
    }
}

/// Why a request failed: refused by the policy (a resolution error, never retried), or a network
/// error (retried).
#[derive(Debug)]
pub enum SendError {
    Refused(String),
    Network(String),
}

impl From<SendError> for Fail {
    fn from(e: SendError) -> Fail {
        match e {
            SendError::Refused(m) => Fail::Refused(m),
            SendError::Network(m) => Fail::Other(m),
        }
    }
}

/// The proxy the environment gives for `url` (`http_proxy`, `https_proxy`, in either case), unless
/// `no_proxy` exempts its host, as Python's `urllib.request.getproxies` and `proxy_bypass` read
/// them.
pub fn proxy_for(url: &str) -> Option<String> {
    let scheme = url.split_once("://").map_or("", |(s, _)| s).to_ascii_lowercase();
    let var = |name: &str| {
        std::env::var(name.to_ascii_lowercase()).ok().filter(|v| !v.is_empty()).or_else(|| std::env::var(name.to_ascii_uppercase()).ok().filter(|v| !v.is_empty()))
    };
    let proxy = var(&format!("{scheme}_proxy"))?;
    let host = crate::policy::netloc_host(url).1.unwrap_or_default();
    let exempt = var("no_proxy").unwrap_or_default();
    for e in exempt.split(',').map(|e| e.trim().trim_start_matches('.').to_ascii_lowercase()) {
        let e = e.split(':').next().unwrap_or("").to_string();
        if e == "*" || (!e.is_empty() && (host == e || host.ends_with(&format!(".{e}")))) {
            return None;
        }
    }
    Some(proxy)
}

/// A client: one agent (its kept-alive connections shared by the threads), under one policy.
pub struct Client {
    agent: ureq::Agent,
    pub policy: Policy,
}

impl Client {
    /// `timeout`: how long to wait to connect, and then for a response's head.
    pub fn new(policy: Policy, timeout: Duration) -> Arc<Client> {
        // with the policy lifted, a proxy may carry the requests (unchecked), as Python's urllib does
        let proxy = if policy.allow_private_hosts { ureq::Proxy::try_from_env() } else { None };
        let config = ureq::Agent::config_builder()
            .proxy(proxy)
            .max_redirects(0)
            .http_status_as_error(false)
            .user_agent(UA)
            .max_idle_connections(256)
            .max_idle_connections_per_host(64)
            .timeout_connect(Some(timeout))
            .timeout_send_request(Some(timeout))
            .timeout_recv_response(Some(timeout))
            .build();
        Arc::new(Client { agent: ureq::Agent::with_parts(config, DefaultConnector::default(), Checked { policy }), policy })
    }

    /// One GET of `url`, not following redirects. `read(status, headers)` says whether to read the
    /// body: a body not read is never received (its connection is closed).
    pub fn get(&self, url: &str, headers: &[(&str, &str)], read: impl Fn(u16, &HeaderMap) -> bool) -> Result<Response, SendError> {
        if !self.policy.allow_private_hosts
            && let Some(p) = proxy_for(url) {
                return Err(SendError::Refused(format!(
                    "{url}: the request would go through the proxy {p}, and the reader cannot check the address the proxy \
                     connects to (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>); exempt the host with no_proxy"
                )));
            }
        let mut req = self.agent.get(url).header("Accept-Encoding", "identity");
        for (k, v) in headers {
            req = req.header(*k, *v);
        }
        let resp = req.call().map_err(|e| match e {
            ureq::Error::Other(b) if b.is::<Refused>() => SendError::Refused(b.to_string()),
            ureq::Error::BadUri(m) => SendError::Refused(format!("{url}: {m}")),
            e => SendError::Network(format!("{url}: {e}")),
        })?;
        let status = resp.status().as_u16();
        let (parts, body) = resp.into_parts();
        let body = if read(status, &parts.headers) {
            body.into_with_config().limit(u64::MAX).read_to_vec().map_err(|e| SendError::Network(format!("{url}: {e}")))?
        } else {
            Vec::new()
        };
        Ok(Response { status, headers: parts.headers, body })
    }
}

/// spec/refkv.md §5.2 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#52-reading-over-http>:
/// an http(s) URL has no userinfo, a non-empty host, and a port of at most 65535.
pub fn check_http_url(url: &str) -> Result<(), Fail> {
    let (netloc, host) = crate::policy::netloc_host(url);
    if netloc.contains('@') || host.is_none() {
        return Err(Fail::Refused(format!("{url}: userinfo or empty host in an http(s) URL")));
    }
    let after = netloc.rsplit(']').next().unwrap_or("");
    if let Some((_, port)) = after.rsplit_once(':')
        && !port.is_empty() && (!port.bytes().all(|c| c.is_ascii_digit()) || port.parse::<u64>().map_or(true, |p| p > 65535)) {
            return Err(Fail::Refused(format!("{url}: invalid port {port:?}")));
        }
    Ok(())
}

/// The target of a redirect from `url` (its one `Location`, resolved, without a fragment), checked
/// as Python's `follow` checks it: an http(s) URL, and one the policy allows for the input at
/// `base`.
pub fn redirect(base: &str, url: &str, r: &Response, policy: &Policy) -> Result<String, Fail> {
    let locations = r.all("location");
    if locations.len() != 1 {
        return Err(Fail::Refused(format!("{url}: HTTP {} with {} Location fields", r.status, locations.len())));
    }
    let new = refkv_vzip::uri::resolve(url, &locations[0])
        .map_err(|_| Fail::Refused(format!("redirect with an invalid Location: {:?}", locations[0])))?;
    let new = new.split('#').next().unwrap_or("").to_string();
    let scheme = new.split_once(':').map_or("", |(s, _)| s).to_ascii_lowercase();
    if scheme != "http" && scheme != "https" {
        return Err(Fail::Refused(format!("redirect to a non-http URL: {new}")));
    }
    check_http_url(&new)?;
    policy.check(base, &new).map_err(Fail::Refused)?;
    Ok(new)
}

/// A number in [0.5, 1.5), for the jitter of retries.
pub fn jitter() -> f64 {
    use std::hash::{BuildHasher, Hasher};
    let mut h = std::collections::hash_map::RandomState::new().build_hasher();
    h.write_u128(std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_nanos()));
    0.5 + (h.finish() >> 11) as f64 / (1u64 << 53) as f64
}

/// The seconds a `Retry-After` value asks to wait: a number of seconds, or an IMF-fixdate.
pub fn retry_after(v: &str) -> Option<f64> {
    if let Ok(s) = v.trim().parse::<f64>() {
        return Some(s);
    }
    let when = imf_fixdate(v.trim())?;
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).ok()?.as_secs_f64();
    Some(when as f64 - now)
}

/// Seconds since the epoch of an IMF-fixdate, `Sun, 06 Nov 1994 08:49:37 GMT` (RFC 9110 §5.6.7).
pub fn imf_fixdate(v: &str) -> Option<i64> {
    const DAYS: [&str; 7] = ["Thu", "Fri", "Sat", "Sun", "Mon", "Tue", "Wed"]; // 1970-01-01 was a Thursday
    const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    let b = v.as_bytes();
    if b.len() != 29 || &v[3..5] != ", " || b[7] != b' ' || b[11] != b' ' || b[16] != b' ' || b[19] != b':' || b[22] != b':' || &v[25..] != " GMT" {
        return None;
    }
    let num = |s: &str| s.bytes().all(|c| c.is_ascii_digit()).then(|| s.parse::<i64>().ok()).flatten();
    let (day, month, year) = (num(&v[5..7])?, MONTHS.iter().position(|m| *m == &v[8..11])? as i64 + 1, num(&v[12..16])?);
    let (h, m, s) = (num(&v[17..19])?, num(&v[20..22])?, num(&v[23..25])?);
    if !(1..=31).contains(&day) || h > 23 || m > 59 || s > 60 {
        return None;
    }
    // days from the civil date (Howard Hinnant's algorithm)
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    let days = era * 146097 + doe - 719468;
    if DAYS[days.rem_euclid(7) as usize] != &v[..3] {
        return None;
    }
    Some(days * 86400 + h * 3600 + m * 60 + s)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_and_waits() {
        assert_eq!(imf_fixdate("Sun, 06 Nov 1994 08:49:37 GMT"), Some(784111777));
        assert_eq!(imf_fixdate("Thu, 01 Jan 1970 00:00:00 GMT"), Some(0));
        assert_eq!(retry_after(" 2.5 "), Some(2.5));
        assert!(retry_after("Thu, 01 Jan 1970 00:00:00 GMT").unwrap() < 0.0);
        let j = jitter();
        assert!((0.5..1.5).contains(&j));
    }

    #[test]
    fn rejects_a_date_with_the_wrong_day() {
        assert_eq!(imf_fixdate("Mon, 06 Nov 1994 08:49:37 GMT"), None);
    }

    #[test]
    fn rejects_a_date_in_another_form() {
        assert_eq!(imf_fixdate("Sunday, 06-Nov-94 08:49:37 GMT"), None);
    }

    #[test]
    fn rejects_an_unparsable_wait() {
        assert_eq!(retry_after("soon"), None);
    }

    #[test]
    fn http_urls() {
        check_http_url("http://h/x").unwrap();
        check_http_url("http://h:65535/x").unwrap();
        check_http_url("http://[::1]:80/x").unwrap();
        check_http_url("http://h:/x").unwrap();
    }

    #[test]
    fn refuses_userinfo() {
        assert!(check_http_url("http://u@h/x").is_err());
    }

    #[test]
    fn refuses_an_empty_host() {
        assert!(check_http_url("http:///x").is_err());
    }

    #[test]
    fn refuses_a_port_over_65535() {
        assert!(check_http_url("http://h:65536/x").is_err());
    }
}
