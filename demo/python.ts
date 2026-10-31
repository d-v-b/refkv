// The Python script the demo shows for an input: a PEP 723 script
// (https://peps.python.org/pep-0723/) that builds the same Zarr hierarchy with refkv and
// opens it with zarr-python. `uv run` reads its dependencies from the header.

/** The profiles whose arrays the refkv.stream codec decodes, which needs refkv's media extra. */
const MEDIA = ["mp3", "flac", "mp4"];

/**
 * A name for the input's files: the last segment of its URL path that is not `content`
 * (Zenodo's file URLs end in it), without its extension, as a Python-friendly stem.
 */
export function stem(url: string): string {
  const segments = new URL(url).pathname.split("/").filter((s) => s !== "" && s !== "content");
  let last = segments.at(-1) ?? "input";
  try {
    last = decodeURIComponent(last);
  } catch {
    // not percent-encoded UTF-8: keep it as it is
  }
  return last.replace(/\.(vzip|zarr|n5|zip|safe)$/i, "").replace(/\.[^.]*$/, "").replace(/[^\w.-]+/g, "_") || "input";
}

/**
 * The script for `url`, a file or store to zarrify or, when `isArchive`, a vzip archive to
 * open. `profile` is the profile the archive names (its `refkv_zarrified.profile`), which
 * decides the extras; `release` is the earliest refkv release the script needs, or undefined
 * for none.
 */
export function pythonScript(url: string, isArchive: boolean, profile: string | undefined, release: string | undefined): string {
  const extra = MEDIA.includes(profile ?? "") ? "media" : "images";
  const requirement = `refkv[${extra}]${release ? `>=${release}` : ""}`;
  const name = stem(url);
  const header = [
    "# /// script",
    '# requires-python = ">=3.12"',
    `# dependencies = [${JSON.stringify(requirement)}]`,
    "# ///",
    "",
  ];
  const open = isArchive
    ? [
        `"""Open the vzip archive ${name}.vzip with zarr-python."""`,
        "",
        "import zarr",
        "import refkv.zarr",
        "",
        "# The archive holds the hierarchy's metadata and references to the original's bytes,",
        "# which are read only when an array's chunks are.",
        `store = refkv.zarr.open(${JSON.stringify(url)})  # a read-only zarr store`,
      ]
    : [
        `"""Zarrify ${name} and open its Zarr hierarchy with zarr-python."""`,
        "",
        "import zarr",
        "import refkv.zarr",
        "from refkv.zarrify import zarrify",
        "",
        "# Reads the input's structure (its headers and indexes), not its data, which stays where",
        "# it is and is read only when an array's chunks are.",
        `reference = zarrify(${JSON.stringify(url)})`,
        "store = refkv.zarr.store(reference)  # a read-only zarr store",
      ];
  const save = isArchive
    ? []
    : [
        "",
        "# The vzip archive this page downloads: the hierarchy, in one file that refers to the",
        "# input's bytes. Host it, and refkv.zarr.open(url) reads it anywhere.",
        `# reference.save(${JSON.stringify(`${name}.vzip`)})`,
      ];
  return [
    ...header,
    ...open,
    "root = zarr.open(store, mode=\"r\")",
    "print(root.tree() if isinstance(root, zarr.Group) else root.info)",
    ...save,
    "",
  ].join("\n");
}
