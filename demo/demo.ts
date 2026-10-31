import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import { Archive } from "../js/src/archive.ts";
import {
  archiveDownloadUrl,
  archiveZarrUrl,
  fetchWorker,
  imageZarrUrl,
  registerVzipWorker,
} from "../js/src/client.ts";
import { ERROR_HEADER, WORKER_HEADER } from "../js/src/server.ts";
import { sampleRanges, tooNarrow, usableWindow } from "./contrast.ts";
import { pythonScript, stem } from "./python.ts";

hljs.registerLanguage("python", python);

// The earliest refkv release the Python script needs: the latest release tag of the commit
// the page was built from (js/build.mjs), or "" when the build found none.
declare const __REFKV_RELEASE__: string;

// The examples are the links with a data-url attribute in index.html, where
// their sources and licenses are noted.

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const input = $<HTMLInputElement>("url");
const status = $("status");

const setStatus = (text: string) => {
  status.replaceChildren(text);
  status.classList.remove("error");
};

/** A request to the service worker that failed: its status, its message, and, for a source that
 * got no response, why (its X-Vzip-Error: `cors`, `unreachable` or `mixed-content`). */
class WorkerError extends Error {
  readonly status: number;
  readonly reason: string | undefined;
  constructor(status: number, message: string, reason: string | null) {
    super(message);
    this.status = status;
    this.reason = reason ?? undefined;
  }
}

/** What to do about an error, by its cause or status. */
function helpFor(e: unknown): string {
  if (!(e instanceof WorkerError)) return "";
  switch (e.reason) {
    case "cors":
      return "The file's server does not let other sites read it from a browser. Its owner can allow it " +
        "with CORS headers: Access-Control-Allow-Origin, and Access-Control-Expose-Headers for Content-Range, " +
        "Content-Length and ETag. Python is not restricted by CORS: the script below zarrifies the file on your machine.";
    case "unreachable":
      return "Check the URL: the browser got no answer from its server.";
    case "mixed-content":
      return "Use the file's https:// URL, if its server has one.";
  }
  if (e.status === 422) return "The input was read, but it is not a format, or a form of one, that this page zarrifies.";
  if (e.status === 507) return "The store is larger than this page lists in a browser; the Python package has no such limit.";
  return "";
}

/** Shows `e` in the status area: a title, its message, and what to do about it. */
function showError(e: unknown) {
  const message = String((e as Error).message ?? e);
  const title = document.createElement("strong");
  title.textContent = "Could not zarrify this input";
  const detail = document.createElement("p");
  detail.textContent = message;
  const help = document.createElement("p");
  help.className = "help";
  help.textContent = helpFor(e);
  status.replaceChildren(title, detail, ...(help.textContent ? [help] : []));
  status.classList.add("error");
}

async function getJson(url: string) {
  const r = await fetchWorker(url);
  if (r.headers.get(WORKER_HEADER) === null) {
    throw new Error(
      `the refkv service worker did not answer this request (HTTP ${r.status} from the network); reload the page and try again`,
    );
  }
  if (!r.ok) throw new WorkerError(r.status, await r.text(), r.headers.get(ERROR_HEADER));
  return r.json();
}

const SECONDS: Record<string, number> = { second: 1, millisecond: 1e-3, minute: 60, hour: 3600 };
const METERS: Record<string, number> = {
  meter: 1, millimeter: 1e-3, micrometer: 1e-6, nanometer: 1e-9, picometer: 1e-12, angstrom: 1e-10,
  centimeter: 1e-2, inch: 0.0254, foot: 0.3048,
};

interface OmeroChannel {
  label?: string;
  color?: string;
  window?: { min?: number; max?: number; start: number; end: number };
}

/**
 * One image to show: its URL (in a {@link View}, its Zarr URL under the worker's prefix; given to
 * {@link imageState}, its Neuroglancer data source), a name, and its translation (one number per
 * axis, in the axes' units).
 */
interface ShownImage {
  url: string;
  name: string;
  translation?: number[];
}

/**
 * A Neuroglancer state for OME-Zarr images that share axes, scale and shape:
 * one image, or every stage position of a multi-position file, each placed by
 * its translation.
 *
 * An image of 2 to 16 channels is one layer whose channel axis is a
 * Neuroglancer channel dimension (`c^`): the shader composites the channels,
 * each with a checkbox, a color and a contrast range from the omero metadata
 * (or, for 3 uint8 channels without omero, as red, green and blue), and there
 * is no channel axis to scroll. This holds however the channels are chunked:
 * when a chunk holds one channel (planar TIFF, IMS, CZI), the Neuroglancer
 * fork assembles each of its chunks from the zarr chunks of every channel.
 * An image of more channels is one layer with a channel axis to scroll.
 */
function imageState(
  images: ShownImage[],
  axes: { name: string; unit?: string }[],
  scale: number[],
  shape: number[],
  dtype: string,
  omero: OmeroChannel[] = [],
  labels: { name: string; url: string }[] = [],
  flipY = false,
  start?: number[],
) {
  // The two displayed axes: x and y, or else the last two.
  let xi = axes.findIndex((a) => a.name === "x");
  let yi = axes.findIndex((a) => a.name === "y");
  if (xi < 0 || yi < 0) [yi, xi] = [axes.length - 2, axes.length - 1];
  // The axis two-finger scroll (the wheel) steps along: the first axis other
  // than x, y and c with more than one element, when there is one (t or z,
  // say). Neuroglancer's wheel moves along the third display dimension, and
  // does nothing without one.
  const scrolled = axes.find((a, i) => i !== xi && i !== yi && a.name !== "c" && shape[i] > 1);
  // The coordinate space, declared up front so that displayDimensions can
  // name x and y before the layers load (otherwise Neuroglancer may display a
  // non-spatial axis such as t).
  const dimensions: Record<string, [number, string]> = {};
  axes.forEach((a, i) => {
    if (a.name === "c") return;
    const unit = a.unit ?? "";
    if (unit in METERS) dimensions[a.name] = [scale[i] * METERS[unit], "m"];
    else if (unit in SECONDS) dimensions[a.name] = [scale[i] * SECONDS[unit], "s"];
    else dimensions[a.name] = [scale[i], ""];
  });
  // Where each image's voxel (0, 0) lands, in full-resolution voxels of the
  // first image (Neuroglancer's coordinates with these dimensions).
  const offset = (img: ShownImage, i: number) => (img.translation?.[i] ?? 0) / scale[i];
  // Start at the first time point (Neuroglancer would pick the middle one),
  // the middle z slice, and the centre of the first image, fitted to the
  // view, each where the image's translation places it (an image translated
  // in z or t would otherwise open outside its data). Zooming out shows the
  // other images where they are; starting with all of them in view would load
  // every one at full resolution.
  const position = axes.flatMap((a, i) => {
    if (a.name === "c") return [];
    const at = offset(images[0], i);
    if (start !== undefined && i !== xi && i !== yi) return [at + start[i]];
    if (a.name === "t" || a.name === "time") return [at];
    return [at + (a.name === "z" ? Math.floor(shape[i] / 2) : shape[i] / 2)];
  });
  const view = {
    dimensions,
    position,
    displayDimensions: [axes[xi].name, axes[yi].name, ...(scrolled ? [scrolled.name] : [])],
    // With the coordinate space declared, this counts full-resolution voxels
    // per screen pixel.
    crossSectionScale: Math.max(shape[yi] / 600, shape[xi] / 850),
    // half a turn about x, which reverses y
    ...(flipY ? { crossSectionOrientation: [1, 0, 0, 0] } : {}),
    layout: "xy",
  };
  const many = images.length > 1;
  const c = axes.findIndex((a) => a.name === "c");
  if (c >= 0 && shape[c] > 1 && shape[c] <= MAX_CHANNELS) {
    const outputDimensions = Object.fromEntries(
      axes.map((a) => (a.name === "c" ? ["c^", [1, ""]] : [a.name, dimensions[a.name]])),
    );
    const rgb = shape[c] === 3 && dtype === "uint8" && omero.length === 0;
    const shader = rgb ? RGB_SHADER : channelShader(shape[c], omero);
    return {
      layers: images.map((img) => ({
        type: "image",
        source: { url: img.url, transform: { outputDimensions } },
        name: many ? img.name : rgb ? "image" : "channels",
        opacity: 1,
        shader,
      })),
      crossSectionBackgroundColor: "#000000", ...view,
    };
  }
  return {
    layers: images.map((img) => ({ type: "image", source: img.url, name: many ? img.name : "image" })),
    ...view,
  };
}

/**
 * The most channels shown as one layer with a control per channel. A
 * Neuroglancer chunk then holds every channel, and the shader has three
 * controls per channel.
 */
const MAX_CHANNELS = 16;

/** {@link imageState}, with a segmentation layer for each OME-Zarr label image. */
function neuroglancerState(...args: Parameters<typeof imageState>) {
  const state = imageState(...args);
  const labels = args[6] ?? [];
  state.layers.push(...labels.map((l) => ({
    type: "segmentation", source: l.url, name: `labels ${l.name}`,
  })) as never[]);
  return state;
}

const RGB_SHADER = `void main() {
  emitRGB(vec3(toNormalized(getDataValue(0)), toNormalized(getDataValue(1)), toNormalized(getDataValue(2))));
}
`;

/** A shader compositing `n` channels, with a control per channel. */
function channelShader(n: number, omero: OmeroChannel[]): string {
  // Control names are shown as labels in the layer panel; Neuroglancer
  // accepts identifiers starting with a lowercase letter.
  const used = new Set<string>();
  const ident = (label: string, k: number) => {
    let id = label.toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^(?=[^a-z])/, "c");
    if (id === "" || id.length > 40 || used.has(id)) id = `channel${k}`;
    used.add(id);
    return id;
  };
  const controls: string[] = [];
  const sum: string[] = [];
  for (let k = 0; k < n; k++) {
    const ch = omero[k] ?? {};
    const id = ident(ch.label ?? `channel${k}`, k);
    const range = ch.window ? `, range=[${ch.window.start}, ${ch.window.end}]` : "";
    controls.push(
      `#uicontrol bool ${id}_on checkbox(default=true)`,
      `#uicontrol vec3 ${id}_color color(default="#${(ch.color ?? "FFFFFF").toLowerCase()}")`,
      `#uicontrol invlerp ${id}(channel=${k}${range})`,
    );
    sum.push(`  if (${id}_on) rgb += ${id}_color * ${id}();`);
  }
  return `${controls.join("\n")}
void main() {
  vec3 rgb = vec3(0.0);
${sum.join("\n")}
  emitRGB(rgb);
}
`;
}

/** The translation of a multiscale image's first level, if it has one. */
function translationOf(ms: { datasets?: { coordinateTransformations?: { type: string; translation?: number[] }[] }[] } | undefined) {
  return ms?.datasets?.[0]?.coordinateTransformations?.find((t) => t.type === "translation")?.translation;
}

type ArrayMeta = any;

/**
 * The chunk shape of a Zarr array: that of a regular chunk grid, or for a
 * rectilinear one (clipped CZI levels) the largest chunk length per axis.
 */
function chunkShapeOf(a: ArrayMeta): number[] {
  const { name, configuration } = a.chunk_grid;
  if (name !== "rectilinear") return configuration.chunk_shape;
  return configuration.chunk_shapes.map((lengths: number | (number | [number, number])[]) =>
    typeof lengths === "number" ? lengths : Math.max(...lengths.map((l) => (Array.isArray(l) ? l[0] : l))),
  );
}

/** One level of an image: its path (for the table), URL and metadata. */
interface Level {
  path: string;
  /** The array's URL, ending in "/". */
  url: string;
  meta: ArrayMeta;
}

/**
 * What can be opened in Neuroglancer as one view: an OME-Zarr image, every
 * series of a file together, one series, or a plain array.
 */
interface View {
  label: string;
  name: string;
  /** The URL shown as the Zarr URL. */
  url: string;
  images: ShownImage[];
  axes: { name: string; unit?: string }[];
  scale: number[];
  levels: Level[];
  omero: OmeroChannel[];
  labels: { name: string; url: string }[];
  note?: string;
  /** Whether its rows run south to north (a GRIB grid that scans so), drawn flipped so that
   * north is up. */
  flipY?: boolean;
  /** The index along each axis but the displayed two that the view opens at (a GRIB variable's
   * first field: its levels are the grid's, of which it may have few); otherwise the first time
   * point and the middle elsewhere. */
  start?: number[];
}

type Multiscale = any;

/** The view of the OME-Zarr multiscale image at `url` (a group URL). */
async function omeView(url: string, ms: Multiscale, label: string, withLabels = false): Promise<View> {
  const levels = await Promise.all(
    ms.datasets.map(async (d: { path: string }) => ({
      path: d.path, url: `${url}${d.path}/`, meta: await getJson(`${url}${d.path}/zarr.json`),
    })),
  );
  const scale = ms.datasets[0].coordinateTransformations?.find(
    (t: { type: string }) => t.type === "scale",
  )?.scale ?? levels[0].meta.shape.map(() => 1);
  const group = await getJson(`${url}zarr.json`);
  // An OME-Zarr store's label images, as segmentation layers. (Only an
  // OME-Zarr input has them; looking for them elsewhere would be a 404.)
  let labels: { name: string; url: string }[] = [];
  if (withLabels) {
    try {
      const names: string[] = (await getJson(`${url}labels/zarr.json`)).attributes?.ome?.labels ?? [];
      labels = names.map((n) => ({ name: n, url: `${url}labels/${n}/` }));
    } catch {
      // No labels.
    }
  }
  return {
    label, name: ms.name ?? label, url, images: [{ url, name: ms.name ?? label, translation: translationOf(ms) }],
    axes: ms.axes, scale, levels, omero: group.attributes?.ome?.omero?.channels ?? [], labels,
  };
}

/** The view of a plain Zarr array (no OME-Zarr metadata) at `url`. */
function arrayView(url: string, path: string, meta: ArrayMeta): View {
  // Neuroglancer names unnamed zarr v3 dimensions dim_0, dim_1, …
  const axes = meta.shape.map((_: number, i: number) => ({ name: meta.dimension_names?.[i] ?? `dim_${i}` }));
  const label = path === "" ? "the array" : path;
  return {
    label, name: label, url, images: [{ url, name: path || "array" }], axes,
    scale: axes.map(() => 1), levels: [{ path: path || "/", url, meta }], omero: [], labels: [],
  };
}

/** The data types Neuroglancer shows (its DataType): no int64, float64, bool or strings. */
const SHOWN_TYPES = ["uint8", "int8", "uint16", "int16", "uint32", "int32", "uint64", "float32"];

/** The codecs the Neuroglancer fork decodes (src/datasource/zarr/codec/ there). */
const DECODED_CODECS = [
  "bytes", "transpose", "crc32c", "blosc", "zstd", "gzip", "sharding_indexed", "numcodecs.shuffle", "n5_default",
  "imagecodecs_jpeg", "imagecodecs_jpeg2k", "imagecodecs_jpegxr", "imagecodecs_lzw", "refkv.fits_tiles", "refkv.grib",
  "refkv.stream",
];

/**
 * The codecs whose float64 arrays the fork shows, as float32: they decode a
 * chunk to float32 values (a GRIB field's are rounded to nearest).
 */
const FLOAT64_CODECS = ["refkv.grib"];

/** The names of the codecs of `codecs`, and of those a sharding codec among them holds. */
const codecNames = (codecs: { name: string; configuration?: any }[]): string[] =>
  codecs.flatMap((c) => [c.name, ...codecNames(c.configuration?.codecs ?? []), ...codecNames(c.configuration?.index_codecs ?? [])]);

/**
 * Whether Neuroglancer can show the array `meta` as an image: two or more axes, of a type it
 * shows (or float64, of a codec of {@link FLOAT64_CODECS}), with codecs it decodes (not, for
 * example, an HDF5 dataset's `numcodecs.zlib`).
 */
const showable = (meta: ArrayMeta) =>
  meta.shape.length >= 2 &&
  (SHOWN_TYPES.includes(meta.data_type) ||
    (meta.data_type === "float64" && meta.codecs.some((c: { name: string }) => FLOAT64_CODECS.includes(c.name)))) &&
  codecNames(meta.codecs).every((n) => DECODED_CODECS.includes(n));

/** The profiles whose archives hold audio samples, which the page lists but does not show. */
const AUDIO = ["mp3", "flac"];

/** The sample entries of the video tracks the fork's `refkv.stream` codec decodes. */
const DECODED_VIDEO = ["avc1", "hvc1", "hev1"];

/**
 * The views of an MP4 file: each 8-bit H.264 or HEVC track's luma (its Y
 * plane), scrolled over time. Neuroglancer shows one array per layer, and a
 * picture's color needs all three planes, the chroma at half size; the U and
 * V planes are in the archive beside Y. The fork's `refkv.stream` codec
 * decodes HEVC where the browser's WebCodecs has a decoder for it that gives
 * the stream's own pictures, and says why in the layer's error where not. A
 * 10-bit HEVC track (`uint16` planes) is named in a note, not opened: no
 * browser measured gives its pictures unconverted (Chrome's cannot be copied,
 * WebKit's are 8-bit).
 */
async function videoViews(zarrUrl: string, mp4: { tracks?: { path: string | null; handler: string; codec: string }[] }): Promise<View[]> {
  const video = (mp4.tracks ?? []).filter((t) => t.path !== null && t.handler === "vide");
  const planes = await Promise.all(video.map(async (t) => {
    const url = `${zarrUrl}${t.path}/y/`;
    return { t, url, meta: DECODED_VIDEO.includes(t.codec) ? await getJson(`${url}zarr.json`) : undefined };
  }));
  const shown = planes.filter((p) => p.meta?.data_type === "uint8");
  const others = planes.filter((p) => p.meta?.data_type !== "uint8")
    .map(({ t, meta }) => `${t.path} (${t.codec}${meta === undefined ? "" : ", 10-bit"})`);
  if (shown.length === 0 && others.length > 0) {
    throw new Error(`no 8-bit H.264 or HEVC track to show: Neuroglancer does not decode ${others.join(", ")}, which the archive holds`);
  }
  return shown.map(({ t, url, meta }) => {
    const view = arrayView(url, `${t.path}/y`, meta);
    view.label = `${t.path} (luma)`;
    if (others.length > 0) view.note = `Not shown: ${others.join(", ")}, which Neuroglancer does not decode.`;
    return view;
  });
}

/** Whether the index `a` comes before `b`, axis by axis. */
const before = (a: number[], b: number[]) => {
  const i = a.findIndex((x, k) => x !== b[k]);
  return i >= 0 && a[i] < b[i];
};

/** The GRIB variables shown first, when a file has them: temperature 2 m above the ground (and
 * at other heights), at the surface, then on pressure levels. */
const GRIB_FIRST = ["t_heightAboveGround", "t_surface", "t_isobaric"];

/**
 * The views of a GRIB file: each variable of each grid (`grid<g>/<variable>`), a temperature
 * first. A grid's rows run in the file's order, so one that scans south to north (its
 * `spatial:transform`'s row step positive, as HRRR's) is flipped to put north up.
 */
async function gribViews(zarrUrl: string): Promise<View[]> {
  const keys = await archiveKeys(zarrUrl);
  const arrays = (await listArrays(zarrUrl, keys)).filter(({ path, meta }) => path.includes("/") && showable(meta));
  const grids = new Map<string, Promise<any>>();
  const views = await Promise.all(arrays.map(async ({ path, meta }) => {
    const grid = path.slice(0, path.indexOf("/"));
    if (!grids.has(grid)) grids.set(grid, getJson(`${zarrUrl}${grid}/zarr.json`));
    const transform: number[] | undefined = (await grids.get(grid)).attributes?.["spatial:transform"];
    const view = arrayView(`${zarrUrl}${path}/`, path, meta);
    view.flipY = (transform?.[4] ?? 0) > 0;
    // its first field, by its chunk keys (`c/<i>/<j>/…`, a chunk a field)
    const fields = keys.filter((k) => k.startsWith(`${path}/c/`))
      .map((k) => k.slice(path.length + 3).split("/").map(Number));
    view.start = fields.reduce((a, b) => (before(b, a) ? b : a), fields[0]);
    return view;
  }));
  const rank = (v: View) => {
    const k = GRIB_FIRST.indexOf(v.label.slice(v.label.indexOf("/") + 1));
    return k < 0 ? GRIB_FIRST.length : k;
  };
  return views.sort((a, b) => rank(a) - rank(b));
}

/** The views of a bioformats2raw layout: each series, and all of them together when they can be. */
async function seriesViews(zarrUrl: string): Promise<View[]> {
  const series: string[] = (await getJson(`${zarrUrl}OME/zarr.json`)).attributes?.ome?.series ?? [];
  const groups = await Promise.all(series.map((s) => getJson(`${zarrUrl}${s}/zarr.json`)));
  const views = await Promise.all(series.map((s, i) => {
    const ms = groups[i].attributes?.ome?.multiscales?.[0];
    return omeView(`${zarrUrl}${s}/`, ms, `series ${s}${ms?.name ? ` (${ms.name})` : ""}`);
  }));
  if (views.length < 2) return views;
  // Series are shown together, each placed by its translation, when they have
  // the same axes, data type and channels (the stage positions of an ND2 or a
  // CZI); otherwise (CZI scenes of other types or axes) one at a time.
  const key = (v: View) => {
    const m = v.levels[0].meta;
    const c = v.axes.findIndex((a) => a.name === "c");
    return JSON.stringify([v.axes.map((a) => a.name), m.data_type, c < 0 ? 0 : m.shape[c]]);
  };
  if (views.some((v) => key(v) !== key(views[0]))) {
    views[0].note = `${views.length} series of different axes, data types or channels: pick one.`;
    return views;
  }
  const placed = views.every((v) => v.images[0].translation !== undefined);
  const all: View = {
    ...views[0],
    label: `all ${views.length} series`,
    url: views[0].url,
    images: views.map((v) => v.images[0]),
    labels: [],
    note: `${views.length} images (series ${series[0]}–${series[series.length - 1]}), ` +
      (placed ? "placed at their stage positions; zoom out in Neuroglancer to see them all." :
        "without stage positions, so they overlap."),
  };
  return [all, ...views];
}

/** The keys of the archive of the hierarchy at `zarrUrl` (which the worker holds). */
async function archiveKeys(zarrUrl: string): Promise<string[]> {
  const r = await fetchWorker(archiveDownloadUrl(zarrUrl));
  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
  const archive = await Archive.open(new Uint8Array(await r.arrayBuffer()), zarrUrl, () => {
    throw new Error("not read");
  });
  return archive.keys();
}

/**
 * The arrays of the hierarchy at `zarrUrl`, outside `refkv_source`, from the
 * keys of its archive (`keys`, else {@link archiveKeys}): a Zarr hierarchy
 * cannot otherwise be listed.
 */
async function listArrays(zarrUrl: string, keys?: string[]): Promise<{ path: string; meta: ArrayMeta }[]> {
  keys ??= await archiveKeys(zarrUrl);
  const paths = keys
    .filter((k) => (k === "zarr.json" || k.endsWith("/zarr.json")) && !k.startsWith("refkv_source/"))
    .map((k) => k.slice(0, -"zarr.json".length).replace(/\/$/, ""));
  const nodes = await Promise.all(paths.map(async (path) => ({
    path, meta: await getJson(`${zarrUrl}${path === "" ? "" : `${path}/`}zarr.json`),
  })));
  return nodes.filter((n) => n.meta.node_type === "array");
}

let prefix: Promise<string> | undefined;
/** An input: its URL, its Zarr URL under the worker's prefix, and whether it is a .vzip archive. */
interface Input {
  url: string;
  zarrUrl: string;
  isArchive: boolean;
}

/** The views of the input shown, each with that input. */
let views: (View & { input: Input })[] = [];

/**
 * The Neuroglancer data source of the node at `url`, a Zarr URL under `shown.zarrUrl`. For a
 * .vzip input it names the archive itself, `<archive url>|vzip:<path>|zarr3:`, which any
 * deployment of the Neuroglancer fork opens, wherever the link is shared. Otherwise it is the
 * worker's URL, `<url>|zarr3:`, which opens only on this site, in a browser where the page has
 * zarrified the input: a link to share names an archive of it, downloaded and hosted.
 */
function neuroglancerSource(shown: Input, url: string): string {
  if (!shown.isArchive) return `${url}|zarr3:`;
  if (!url.startsWith(shown.zarrUrl)) throw new Error(`${url} is not under ${shown.zarrUrl}`);
  const path = url.slice(shown.zarrUrl.length).split("/").map(encodeURIComponent).join("/");
  return `${shown.url}|vzip:${path}|zarr3:`;
}

function showLevels(rows: Omit<Level, "url">[]) {
  $("levels").replaceChildren(
    ...rows.map(({ path, meta: a }) => {
      const tr = document.createElement("tr");
      for (const v of [
        path,
        a.shape.join(" × "),
        chunkShapeOf(a).join(" × ") + (a.chunk_grid.name === "rectilinear" ? " (at most; rectilinear)" : ""),
        a.data_type,
        a.codecs.map((c: { name: string }) => c.name).join(", "),
      ]) {
        const td = document.createElement("td");
        td.textContent = v;
        tr.append(td);
      }
      return tr;
    }),
  );
}

let shown: View | undefined;

/** Shows `view`: its levels, and a Neuroglancer link. */
async function showView(view: View & { input: Input }) {
  shown = view;
  const source = (url: string) => neuroglancerSource(view.input, url);
  $("share-local").hidden = view.input.isArchive;
  $("share-archive").hidden = !view.input.isArchive;
  $("name").textContent = view.name;
  $("zarr-url").textContent = source(view.url);
  $("view-note").textContent = view.note ?? "";
  showLevels(view.levels);
  const level0 = view.levels[0].meta;
  const dataType: string = level0.data_type;
  const coarsest = view.levels[view.levels.length - 1];
  const sample = (channels?: number[]) =>
    sampleRanges(coarsest.url, coarsest.meta, view.axes, chunkShapeOf(coarsest.meta), channels, view.start);
  // Each channel's contrast: the file's window when it is usable, else one
  // sampled from the data (contrast.ts). 8-bit data keeps its full range.
  let omero = view.omero;
  const c = view.axes.findIndex((a) => a.name === "c");
  const nc = c >= 0 ? level0.shape[c] : 0;
  if (dataType !== "uint8" && nc > 1 && nc <= MAX_CHANNELS) {
    const need = Array.from({ length: nc }, (_, k) => k).filter((k) => !usableWindow(omero[k]?.window, dataType));
    const ranges = need.length > 0 ? await sample(need) : [];
    omero = Array.from({ length: nc }, (_, k) => {
      const ch = omero[k] ?? {};
      if (usableWindow(ch.window, dataType)) return ch;
      // A sampled range, or else (the chunks hold every channel, or their
      // codec is not decoded here) the file's window unless it is too narrow.
      const r = ranges[need.indexOf(k)];
      if (r !== undefined) return { ...ch, window: { start: r[0], end: r[1] } };
      return { ...ch, window: ch.window && !tooNarrow(ch.window, dataType) ? ch.window : undefined };
    });
  }
  const state = neuroglancerState(
    view.images.map((img) => ({ ...img, url: source(img.url) })), view.axes, view.scale, level0.shape, dataType, omero,
    view.labels.map((l) => ({ ...l, url: source(l.url) })), view.flipY, view.start,
  );
  // An image shown with Neuroglancer's default shader gets a sampled range too.
  const plain = state.layers.filter((l) => l.type === "image" && !("shader" in l));
  if (plain.length > 0 && dataType !== "uint8") {
    const [range] = await sample();
    if (range !== undefined) {
      for (const l of plain) {
        Object.assign(l, { shader: `#uicontrol invlerp normalized(range=[${range[0]}, ${range[1]}])\nvoid main() {\n  emitGrayscale(normalized());\n}\n` });
      }
    }
  }
  if (shown !== view) return;
  const ng = $<HTMLAnchorElement>("open-ng");
  ng.hidden = false;
  ng.href = new URL(`neuroglancer/#!${encodeURIComponent(JSON.stringify(state))}`, location.href).href;
}

let pythonUrl: string | undefined;

/** The media profile a URL's extension suggests (whose script needs refkv's media extra), for a
 * script shown before the input was read. */
function mediaProfile(url: string): string | undefined {
  const ext = new URL(url).pathname.toLowerCase().match(/\.(\w+)$/)?.[1];
  return ext === "mp3" ? "mp3" : ext === "flac" ? "flac" : ext && ["mp4", "mov", "m4a", "m4v"].includes(ext) ? "mp4" : undefined;
}

/** Shows the Python script that builds the same hierarchy as `input`, and links it as a file. */
function showPython(input: Input, profile: string | undefined) {
  const script = pythonScript(input.url, input.isArchive, profile, __REFKV_RELEASE__ || undefined);
  const file = `${input.isArchive ? "open" : "zarrify"}_${stem(input.url)}.py`;
  $("python-code").innerHTML = hljs.highlight(script, { language: "python" }).value; // escaped by highlight.js
  $("python-run").textContent = `uv run ${file}`;
  if (pythonUrl) URL.revokeObjectURL(pythonUrl);
  pythonUrl = URL.createObjectURL(new Blob([script], { type: "text/x-python" }));
  const save = $<HTMLAnchorElement>("python-save");
  save.href = pythonUrl;
  save.download = file;
}

async function zarrify(url: string) {
  $("result").hidden = true;
  $("python").hidden = true;
  // Record the URL in the address bar, so the page can be shared or reloaded.
  const here = new URL(location.href);
  here.searchParams.set("url", url);
  history.replaceState(null, "", here);
  setStatus("Starting the service worker…");
  // the status and the result are above the examples: bring them into view
  status.scrollIntoView({ block: "nearest", behavior: "smooth" });
  prefix ??= registerVzipWorker(new URL("vzip-sw.js", location.href));
  const p = await prefix;
  const isArchive = /\.vzip(?:[?#]|$)/i.test(url);
  const zarrUrl = isArchive ? archiveZarrUrl(p, url) : imageZarrUrl(p, url);
  const opened: Input = { url, zarrUrl, isArchive };
  setStatus(isArchive ? "Opening the archive…" : "Reading the file's structure…");
  const t0 = performance.now();
  const root = await getJson(`${zarrUrl}zarr.json`);
  const ms = Math.round(performance.now() - t0);
  const attrs = root.attributes ?? {};
  const map = $<HTMLAnchorElement>("open-map");
  const picker = $<HTMLSelectElement>("view");
  map.hidden = true;
  $("view-row").hidden = true;
  if (attrs.refkv_zarrified?.profile === "safe") {
    // A Sentinel-2 product: GeoZarr groups of JPEG 2000 bands, for a map
    // viewer (map.html) rather than Neuroglancer.
    const safe = attrs.refkv_zarrified.safe ?? {};
    views = [];
    $("name").textContent = `${safe.PRODUCT_URI ?? url.split("/").pop()} (${safe.PROCESSING_LEVEL ?? "Sentinel-2"})`;
    $("zarr-url").textContent = neuroglancerSource(opened, zarrUrl);
    $("view-note").textContent = `${safe.SENSING_TIME ?? ""} ${attrs["proj:code"] ?? ""}`;
    showLevels(await listArrays(zarrUrl));
    $("open-ng").hidden = true;
    $("share-local").hidden = true;
    $("share-archive").hidden = true;
    map.hidden = false;
    map.href = new URL(`map.html?url=${encodeURIComponent(url)}`, location.href).href;
  } else if (AUDIO.includes(attrs.refkv_zarrified?.profile)) {
    // Audio: an array of samples, decoded from the file's frames when a chunk
    // is read (the refkv.stream codec). Neuroglancer shows images, so the page
    // lists the arrays and offers the archive.
    const info = attrs.refkv_zarrified[attrs.refkv_zarrified.profile] ?? {};
    views = [];
    const file = url.split("?")[0].split("/").pop()!;
    try {
      $("name").textContent = decodeURIComponent(file);
    } catch {
      $("name").textContent = file;
    }
    $("zarr-url").textContent = neuroglancerSource(opened, zarrUrl);
    const rate = info.sample_rate;
    $("view-note").textContent = `Audio${rate ? ` at ${rate} Hz` : ""}: not shown here. ` +
      "zarr-python reads the samples from the downloaded archive, with refkv's media extra.";
    showLevels(await listArrays(zarrUrl));
    $("open-ng").hidden = true;
    $("share-local").hidden = true;
    $("share-archive").hidden = true;
  } else {
    const ms0 = attrs.ome?.multiscales?.[0];
    let found: View[];
    if (root.node_type === "array") {
      found = [arrayView(zarrUrl, "", root)];
    } else if (attrs.refkv_zarrified?.profile === "mp4") {
      found = await videoViews(zarrUrl, attrs.refkv_zarrified.mp4 ?? {});
      if (found.length === 0) throw new Error("no video track to show (audio is not displayed)");
    } else if (ms0 !== undefined) {
      found = [await omeView(zarrUrl, ms0, ms0.name ?? "image", attrs.refkv_zarrified?.profile === "ome-zarr")];
    } else if (attrs.ome?.["bioformats2raw.layout"] !== undefined) {
      found = await seriesViews(zarrUrl);
    } else if (attrs.refkv_zarrified?.profile === "grib") {
      found = await gribViews(zarrUrl);
      if (found.length === 0) throw new Error("no GRIB variable to show");
    } else {
      // No OME-Zarr image (a Zarr v2 or N5 hierarchy without one, or an
      // HDF5 file's datasets): its arrays that Neuroglancer can show. An HDF5
      // file has many, most of them small or long and narrow (an NWB file's
      // traces, time by cell), so its images come first (the last two axes
      // each 64 or more, neither 8 times the other), then the rest, each the
      // largest first.
      const arrays = (await listArrays(zarrUrl)).filter(({ meta }) => showable(meta));
      if (attrs.refkv_zarrified?.profile === "hdf5") {
        const size = (meta: ArrayMeta) => meta.shape.reduce((a: number, b: number) => a * b, 1);
        const image = (meta: ArrayMeta) => {
          const [h, w] = meta.shape.slice(-2);
          return Math.min(h, w) >= 64 && Math.max(h, w) <= 8 * Math.min(h, w);
        };
        arrays.sort((a, b) => Number(image(b.meta)) - Number(image(a.meta)) || size(b.meta) - size(a.meta));
      }
      // at most 50 in the picker (an NWB file of stimulus frames has thousands)
      found = arrays.slice(0, 50).map(({ path, meta }) => arrayView(`${zarrUrl}${path}/`, path, meta));
      if (found.length === 0) throw new Error("no arrays to show (Neuroglancer shows arrays of two or more axes)");
      if (arrays.length > found.length) {
        for (const v of found) v.note = `${found.length} of the ${arrays.length} arrays Neuroglancer can show are listed.`;
      }
    }
    views = found.map((v) => ({ ...v, input: opened }));
    if (views.length > 1) {
      picker.replaceChildren(...views.map((v, i) => new Option(v.label, String(i))));
      $("view-row").hidden = false;
    }
    await showView(views[0]);
  }
  $<HTMLAnchorElement>("download").href = archiveDownloadUrl(zarrUrl);
  showPython(opened, attrs.refkv_zarrified?.profile);
  $("result").hidden = false;
  $("python").hidden = false;
  setStatus(`Ready in ${ms} ms.`);
}

$("python-copy").addEventListener("click", () => {
  navigator.clipboard.writeText($("python-code").textContent ?? "").then(
    () => ($("python-copy").textContent = "Copied"),
    () => ($("python-copy").textContent = "Copy failed"),
  );
  setTimeout(() => ($("python-copy").textContent = "Copy"), 2000);
});
$("view").addEventListener("change", () => {
  showView(views[Number($<HTMLSelectElement>("view").value)]).catch(showError);
});
$("form").addEventListener("submit", (event) => {
  event.preventDefault();
  const url = input.value.trim();
  zarrify(url).catch((e) => {
    showError(e);
    // Python reads a file whatever its server's CORS headers say
    if (e instanceof WorkerError && e.reason === "cors") {
      showPython({ url, zarrUrl: "", isArchive: /\.vzip(?:[?#]|$)/i.test(url) }, mediaProfile(url));
      $("python").hidden = false;
    }
  });
});
for (const link of document.querySelectorAll<HTMLAnchorElement>("a[data-url]")) {
  link.addEventListener("click", (event) => {
    event.preventDefault();
    input.value = link.dataset.url!;
    $<HTMLFormElement>("form").requestSubmit();
  });
}
const fromQuery = new URLSearchParams(location.search).get("url");
if (fromQuery) {
  input.value = fromQuery;
  $<HTMLFormElement>("form").requestSubmit();
}
