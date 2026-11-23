// The XML subset of spec/zarrify.md §1.5
// <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store> as a tree
// (`parseXml`, `XmlElement`), which the frozen SAFE profile reads its documents with, and the URL
// of a store's object (`objectUrl`), and whether a URL is a store input (`isStoreUrl`): the TypeScript the package had before the Rust core took
// over the store rules (d-v-b/refkv#459), copied here so that the frozen reference keeps them as
// they were and needs no WebAssembly instance.

import { StoreError } from "../../../../js/src/zarrify/store.ts";

const MAX_DEPTH = 256;

const PATH_SAFE = /[A-Za-z0-9\-._~!$&'()*+,;=:@/]/;
const utf8 = new TextEncoder();

function pct(s: string, safe: RegExp): string {
  let out = "";
  for (const b of utf8.encode(s)) {
    const c = String.fromCharCode(b);
    out += b < 128 && safe.test(c) ? c : `%${b.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return out;
}

/**
 * The URL of the object with relative key `key` (§1.4
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#14-store-inputs>).
 */
export function objectUrl(storeUrl: string, key: string): string {
  return storeUrl + pct(key, PATH_SAFE);
}

function isChar(c: number): boolean {
  return c === 0x9 || c === 0xa || c === 0xd || (c >= 0x20 && c <= 0xd7ff) || (c >= 0xe000 && c <= 0xfffd) ||
    (c >= 0x10000 && c <= 0x10ffff);
}

const NAME_START = /[A-Za-z_:]/;
const NAME_CHAR = /[A-Za-z0-9_:.-]/;
const WS = new Set([" ", "\t", "\r", "\n"]);
const ENTITIES: Record<string, string> = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };

export interface XmlElement {
  name: string;
  children: XmlElement[];
  text: string[];
  /** The attributes, [name, value] in document order, values with references replaced. */
  attributes: [string, string][];
}

/** The XML subset of §1.5 <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store> (the same algorithm as the Python reference); `what` names the
 * document in rejections, and `fail` makes the error. */
class Xml {
  pos = 0;
  depth = 0; // of the element being read
  readonly s: string;
  readonly what: string;
  readonly error: (m: string) => Error;
  constructor(s: string, what = "listing", error: (m: string) => Error = (m) => new StoreError(m)) {
    this.s = s;
    this.what = what;
    this.error = error;
  }

  fail(why: string): never {
    throw this.error(`${this.what} is not well formed: ${why} at ${this.pos}`);
  }
  ws() {
    while (this.pos < this.s.length && WS.has(this.s[this.pos])) this.pos++;
  }
  at(t: string): boolean {
    return this.s.startsWith(t, this.pos);
  }
  chars(t: string) {
    for (const ch of t) {
      const c = ch.codePointAt(0)!;
      if (!isChar(c)) this.fail(`character U+${c.toString(16).toUpperCase().padStart(4, "0")}`);
    }
  }
  comment() {
    const end = this.s.indexOf("--", this.pos + 4);
    if (end < 0 || !this.s.startsWith("-->", end)) this.fail("bad comment");
    this.chars(this.s.slice(this.pos + 4, end));
    this.pos = end + 3;
  }
  misc() {
    for (;;) {
      this.ws();
      if (this.at("<!--")) this.comment();
      else return;
    }
  }
  name(): string {
    const start = this.pos;
    if (start >= this.s.length || !NAME_START.test(this.s[start])) this.fail("expected a name");
    this.pos++;
    while (this.pos < this.s.length && NAME_CHAR.test(this.s[this.pos])) this.pos++;
    return this.s.slice(start, this.pos);
  }
  reference(): string {
    const end = this.s.indexOf(";", this.pos);
    if (end < 0) this.fail("unterminated reference");
    const body = this.s.slice(this.pos + 1, end);
    this.pos = end + 1;
    if (Object.hasOwn(ENTITIES, body)) return ENTITIES[body];
    let v: number;
    if (/^#x[0-9a-fA-F]+$/.test(body)) v = body.length < 12 ? parseInt(body.slice(2), 16) : -1;
    else if (/^#[0-9]+$/.test(body)) v = body.length < 12 ? parseInt(body.slice(1), 10) : -1;
    else this.fail(`unknown reference &${body};`);
    if (!isChar(v!)) this.fail(`reference &${body}; is not a character`);
    return String.fromCodePoint(v!);
  }
  chardata(stop: string): string {
    const start = this.pos;
    while (this.pos < this.s.length && this.s[this.pos] !== "<" && this.s[this.pos] !== "&" &&
      this.s[this.pos] !== stop) this.pos++;
    const t = this.s.slice(start, this.pos);
    this.chars(t);
    return t;
  }
  element(): XmlElement {
    if (!this.at("<")) this.fail("expected an element");
    if (++this.depth > MAX_DEPTH) this.fail(`elements nest more than ${MAX_DEPTH} deep`);
    const el = this.elementBody();
    this.depth--;
    return el;
  }
  elementBody(): XmlElement {
    this.pos++;
    const el: XmlElement = { name: this.name(), children: [], text: [], attributes: [] };
    const s = this.s;
    for (;;) {
      if (this.pos < s.length && WS.has(s[this.pos])) {
        this.ws();
        if (this.pos < s.length && NAME_START.test(s[this.pos])) {
          const name = this.name();
          this.ws();
          if (!this.at("=")) this.fail("expected =");
          this.pos++;
          this.ws();
          const q = this.pos < s.length ? s[this.pos] : "";
          if (q !== "'" && q !== '"') this.fail("expected a quoted value");
          this.pos++;
          const value: string[] = [];
          for (;;) {
            value.push(this.chardata(q));
            if (this.at("&")) value.push(this.reference());
            else if (this.at(q)) {
              this.pos++;
              break;
            } else this.fail("bad attribute value");
          }
          el.attributes.push([name, value.join("")]);
          continue;
        }
      }
      break;
    }
    if (this.at("/>")) {
      this.pos += 2;
      return el;
    }
    if (!this.at(">")) this.fail("expected >");
    this.pos++;
    for (;;) {
      if (this.pos >= s.length) this.fail("unterminated element");
      if (this.at("</")) {
        this.pos += 2;
        if (this.name() !== el.name) this.fail("mismatched end tag");
        this.ws();
        if (!this.at(">")) this.fail("expected >");
        this.pos++;
        return el;
      }
      if (this.at("<!--")) this.comment();
      else if (this.at("<!") || this.at("<?")) this.fail("unsupported markup");
      else if (this.at("<")) el.children.push(this.element());
      else if (this.at("&")) el.text.push(this.reference());
      else {
        const t = this.chardata("<");
        if (t.includes("]]>")) this.fail("]]> in character data");
        el.text.push(t);
      }
    }
  }
  document(): XmlElement {
    this.ws();
    if (this.at("<?xml") && this.pos + 5 < this.s.length && WS.has(this.s[this.pos + 5])) {
      const end = this.s.indexOf("?>", this.pos + 5);
      if (end < 0) this.fail("unterminated XML declaration");
      this.chars(this.s.slice(this.pos, end));
      this.pos = end + 2;
    }
    this.misc();
    const root = this.element();
    this.misc();
    if (this.pos !== this.s.length) this.fail("content after the root element");
    return root;
  }
}

/**
 * The root element of a document in the XML subset of §1.5
 * <https://github.com/d-v-b/refkv/blob/main/spec/zarrify.md#15-listing-a-store>, or a rejection
 * (naming `what`).
 */
export function parseXml(s: string, what: string, error: (m: string) => Error): XmlElement {
  return new Xml(s, what, error).document();
}

/** Is `url` a store input: a URL whose path ends in `/`? */
export function isStoreUrl(url: string): boolean {
  return url.split(/[?#]/, 1)[0].endsWith("/");
}
