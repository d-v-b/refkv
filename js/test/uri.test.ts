// Strict RFC 3986 references (uri.ts): what the Python host (python/src/refkv/uri.py) and the
// Rust core (rust/refkv-uri) give for the same input, which python/tests/test_uri_core.py
// compares over a generated corpus.

import assert from "node:assert/strict";
import { test } from "node:test";

import { isUriReference, resolveReference } from "../src/uri.ts";

test("isUriReference and resolveReference take any string, a newline in it too", () => {
  for (const [ref, ok] of [
    ["a/b?q#f", true],
    ["//[::1]:8000/x", true],
    ["#a\nb", false],
    ["?a\nb", false],
    ["//h\n", false],
    ["a\n", false],
  ] as const) {
    assert.equal(isUriReference(ref), ok, JSON.stringify(ref));
  }
  assert.equal(resolveReference("http://h/?a\nb", ""), "http://h/?a\nb");
  assert.equal(resolveReference("http://h/#a\nb", "x"), "http://h/x");
  assert.equal(resolveReference("http://a/b/c/d;p?q", "../g"), "http://a/b/g");
});

test("resolveReference throws for a string that is not a reference", () => {
  assert.throws(() => resolveReference("http://h/", "#a\nb"), /not a valid URI reference/);
});
