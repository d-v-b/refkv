"""compare.py on the NDPI corpus, with the caching proxy's upstream fetches limited to
6 at a time (the brief: at most 6 connections to a real host)."""
import hashlib, sys, threading
sys.path.insert(0, "conformance/zarrify")
import proxy
_sem = threading.BoundedSemaphore(6)
_block = proxy.Upstream.block
_size = proxy.Upstream.size
log = open(sys.argv[1] + ".upstream.log", "a")
def block(self, url, i):
    f = self.cache / f"{hashlib.sha256(url.encode()).hexdigest()}.{i}"  # proxy.Upstream.block's cache name
    if f.exists():
        return f.read_bytes()
    with _sem:
        log.write(f"{url} {i}\n"); log.flush()
        return _block(self, url, i)
def size(self, url):
    with _sem:
        return _size(self, url)
proxy.Upstream.block = block
proxy.Upstream.size = size
import compare
sys.exit(compare.main(sys.argv[1:]))
