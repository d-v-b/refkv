//! The reader policy of spec/refkv.md §6.7
//! <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>, as
//! python/src/refkv/policy.py has it for `python -m refkv.virtualize`: rule 1 (the schemes an
//! archive may name), and rule 3 (no loopback, private, link-local or special address unless
//! `allow_private_hosts`, the UNSAFE opt-out `--allow-private-hosts` asks for by name). The input
//! is read under it, every connection's resolved addresses checked (`http.rs`), and the archive's
//! sources are checked against it before anything is written (`output.rs`).

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// The class of an address (rule 3).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    Loopback,
    Private,
    LinkLocal,
    Special,
    Public,
}

impl Class {
    fn name(self) -> &'static str {
        match self {
            Class::Loopback => "loopback",
            Class::Private => "private",
            Class::LinkLocal => "link-local",
            Class::Special => "special",
            Class::Public => "public",
        }
    }
}

const V4: &[(Class, [u8; 4], u8)] = &[
    (Class::Loopback, [127, 0, 0, 0], 8),
    (Class::Private, [10, 0, 0, 0], 8),
    (Class::Private, [172, 16, 0, 0], 12),
    (Class::Private, [192, 168, 0, 0], 16),
    (Class::LinkLocal, [169, 254, 0, 0], 16),
    (Class::Special, [0, 0, 0, 0], 8),
    (Class::Special, [100, 64, 0, 0], 10),
    (Class::Special, [192, 0, 0, 0], 24),
    (Class::Special, [192, 0, 2, 0], 24),
    (Class::Special, [198, 18, 0, 0], 15),
    (Class::Special, [198, 51, 100, 0], 24),
    (Class::Special, [203, 0, 113, 0], 24),
    (Class::Special, [224, 0, 0, 0], 3),
];

const V6: &[(Class, u128, u8)] = &[
    (Class::Loopback, 1, 128),
    (Class::Private, 0xfc00 << 112, 7),
    (Class::LinkLocal, 0xfe80 << 112, 10),
    (Class::Special, 0, 8),
    (Class::Special, 0x0064_ff9b_0001 << 80, 48),
    (Class::Special, 0x0100 << 112, 63),
    (Class::Special, 0x2001 << 112, 23),
    (Class::Special, 0x2001_0db8 << 96, 32),
    (Class::Special, 0x3fff << 112, 20),
    (Class::Special, 0x5f00 << 112, 16),
    (Class::Special, 0xfec0 << 112, 10),
    (Class::Special, 0xff00 << 112, 8),
];

fn within(n: u128, net: u128, bits: u8, width: u32) -> bool {
    bits == 0 || (n ^ net) >> (width - bits as u32) == 0
}

/// The class of an IP address. An IPv6 address that embeds an IPv4 one (IPv4-mapped, NAT64,
/// 6to4) has the IPv4 address's class.
pub fn address_class(ip: IpAddr) -> Class {
    match ip {
        IpAddr::V4(a) => {
            let n = u32::from(a) as u128;
            V4.iter()
                .find(|(_, net, bits)| within(n, u32::from(Ipv4Addr::from(*net)) as u128, *bits, 32))
                .map_or(Class::Public, |c| c.0)
        }
        IpAddr::V6(a) => {
            let n = u128::from(a);
            if within(n, 0xffff << 32, 96, 128) || within(n, 0x0064_ff9b << 96, 96, 128) {
                return address_class(IpAddr::V4(Ipv4Addr::from(n as u32)));
            }
            if within(n, 0x2002 << 112, 16, 128) {
                return address_class(IpAddr::V4(Ipv4Addr::from((n >> 80) as u32)));
            }
            V6.iter().find(|(_, net, bits)| within(n, *net, *bits, 128)).map_or(Class::Public, |c| c.0)
        }
    }
}

/// The class of a URL's host as it is written: an IP literal's class, loopback for `localhost`
/// and names under `.localhost` (RFC 6761), and None for any other name, whose class depends on
/// what it resolves to.
pub fn host_class(host: &str) -> Option<Class> {
    let h = host.to_ascii_lowercase();
    let mut h = h.trim_end_matches('.');
    if h.starts_with('[') && h.ends_with(']') {
        h = &h[1..h.len() - 1];
    }
    if h == "localhost" || h.ends_with(".localhost") {
        return Some(Class::Loopback);
    }
    let h = h.split('%').next().unwrap_or(""); // an IPv6 zone
    if let Ok(a) = h.parse::<Ipv4Addr>() {
        return Some(address_class(IpAddr::V4(a)));
    }
    h.parse::<Ipv6Addr>().ok().map(|a| address_class(IpAddr::V6(a)))
}

/// The scheme of an absolute URL, lowercase, with https counted as http (rule 1).
fn scheme(url: &str) -> String {
    let s = url.split_once(':').map_or("", |(s, _)| s).to_ascii_lowercase();
    if s == "https" { "http".into() } else { s }
}

/// The parts of an http(s) URL `urllib.parse.urlsplit` gives: (netloc, hostname), the hostname
/// lowercase, without its brackets, its userinfo and its port.
pub fn netloc_host(url: &str) -> (String, Option<String>) {
    let p = refkv_vzip::uri::split(url);
    let netloc = p.authority.unwrap_or("").to_string();
    let hp = netloc.rsplit_once('@').map_or(netloc.as_str(), |(_, h)| h);
    let host = if let Some(r) = hp.strip_prefix('[') {
        r.split(']').next().unwrap_or("")
    } else {
        hp.split(':').next().unwrap_or("")
    };
    let host = (!host.is_empty()).then(|| host.to_ascii_lowercase());
    (netloc, host)
}

/// What the reader may resolve. The defaults of spec/refkv.md §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>, with rule 3 lifted by
/// `allow_private_hosts` (UNSAFE).
#[derive(Clone, Copy, Debug, Default)]
pub struct Policy {
    pub allow_private_hosts: bool,
}

impl Policy {
    /// Refuses `url` unless rule 1 allows it for an archive at `base` (http and https always, `file`
    /// for a local archive and with no host), and rule 3 its host as written.
    pub fn check(&self, base: &str, url: &str) -> Result<(), String> {
        let s = scheme(url);
        if s == "file" {
            if scheme(base) != "file" {
                return Err(format!("{url}: an archive opened from a URL may not read local files (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)"));
            }
            let (netloc, _) = netloc_host(url);
            if !netloc.is_empty() && !netloc.eq_ignore_ascii_case("localhost") {
                return Err(format!(
                    "{url}: a file: URL with the host {netloc:?} (a UNC path) reads over the network, which the reader \
                     does not do without allow_file_hosts=True (spec §5 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#5-source-table>)"
                ));
            }
        } else if s != "http" {
            return Err(format!("{url}: the scheme {s:?} is not allowed (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)"));
        }
        self.check_host(url)
    }

    /// Refuses an http(s) `url` whose host as written rule 3 refuses: an IP literal by its class,
    /// `localhost` as loopback. A name passes here; each address it resolves to is checked when
    /// a connection is made (`check_address`).
    pub fn check_host(&self, url: &str) -> Result<(), String> {
        if scheme(url) == "http"
            && let Some(c) = netloc_host(url).1.as_deref().and_then(host_class) {
                return self.check_class(url, c, None);
            }
        Ok(())
    }

    /// Refuses a connection to `ip` for `url` that rule 3 refuses.
    pub fn check_address(&self, url: &str, ip: IpAddr) -> Result<(), String> {
        self.check_class(url, address_class(ip), Some(ip))
    }

    fn check_class(&self, url: &str, c: Class, ip: Option<IpAddr>) -> Result<(), String> {
        if c == Class::Public || self.allow_private_hosts {
            return Ok(());
        }
        let at = ip.map_or(String::new(), |a| format!(" (at {a})"));
        Err(format!(
            "{url}: the host{at} is a {} address, which the reader does not reach without --allow-private-hosts (spec §6.7 <https://github.com/d-v-b/refkv/blob/main/spec/refkv.md#67-reader-policy>)",
            c.name()
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes() {
        for (a, c) in [
            ("127.0.0.1", Class::Loopback),
            ("::1", Class::Loopback),
            ("10.1.2.3", Class::Private),
            ("172.31.255.255", Class::Private),
            ("172.32.0.0", Class::Public),
            ("192.168.1.1", Class::Private),
            ("fd00::1", Class::Private),
            ("169.254.169.254", Class::LinkLocal),
            ("fe80::1", Class::LinkLocal),
            ("0.0.0.0", Class::Special),
            ("100.64.0.1", Class::Special),
            ("192.0.2.1", Class::Special),
            ("198.19.0.1", Class::Special),
            ("224.0.0.1", Class::Special),
            ("255.255.255.255", Class::Special),
            ("2001:db8::1", Class::Special),
            ("ff02::1", Class::Special),
            ("::", Class::Special),
            ("::ffff:127.0.0.1", Class::Loopback),
            ("64:ff9b::a00:1", Class::Private),
            ("2002:a00:1::", Class::Private),
            ("2002:808:808::", Class::Public),
            ("8.8.8.8", Class::Public),
            ("2606:4700::1111", Class::Public),
        ] {
            assert_eq!(address_class(a.parse().unwrap()), c, "{a}");
        }
        for (h, c) in [
            ("LocalHost.", Some(Class::Loopback)),
            ("a.localhost", Some(Class::Loopback)),
            ("[::1]", Some(Class::Loopback)),
            ("[fe80::1%25eth0]", Some(Class::LinkLocal)),
            ("127.0.0.1", Some(Class::Loopback)),
            ("example.org", None),
        ] {
            assert_eq!(host_class(h), c, "{h}");
        }
    }

    #[test]
    fn allowed() {
        let p = Policy::default();
        for (base, url) in [
            ("file:///x/a.vzip", "http://example.org/a"),
            ("file:///x/a.vzip", "HTTPS://example.org/a"),
            ("file:///x/a.vzip", "file:///x/b"),
            ("file:///x/a.vzip", "file://localhost/x/b"),
            ("http://example.org/a.vzip", "http://8.8.8.8/b"),
        ] {
            assert_eq!(p.check(base, url), Ok(()), "{url}");
        }
        let unsafe_ = Policy { allow_private_hosts: true };
        unsafe_.check("file:///a", "http://127.0.0.1:8000/x").unwrap();
        unsafe_.check_address("http://h/", "10.0.0.1".parse().unwrap()).unwrap();
        p.check_address("http://h/", "8.8.4.4".parse().unwrap()).unwrap();
        assert_eq!(netloc_host("http://u:p@[::1]:80/x"), ("u:p@[::1]:80".into(), Some("::1".into())));
    }

    #[test]
    fn refuses_a_private_literal_host() {
        assert!(Policy::default().check("file:///a", "http://127.0.0.1:8000/x").is_err());
    }

    #[test]
    fn refuses_localhost() {
        assert!(Policy::default().check_host("http://localhost/x").is_err());
    }

    #[test]
    fn refuses_a_private_address() {
        assert!(Policy::default().check_address("http://h/", "192.168.0.1".parse().unwrap()).is_err());
    }

    #[test]
    fn refuses_local_files_for_a_remote_archive() {
        assert!(Policy::default().check("http://h/a.vzip", "file:///etc/passwd").is_err());
    }

    #[test]
    fn refuses_a_file_host() {
        assert!(Policy::default().check("file:///a", "file://server/share/x").is_err());
    }

    #[test]
    fn refuses_another_scheme() {
        assert!(Policy::default().check("file:///a", "s3://bucket/x").is_err());
    }
}
