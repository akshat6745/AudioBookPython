"""Private novel registry. Names resolve only through this index, not a guessed slug."""

from __future__ import annotations

import json
import re

from .hashing import sha256_bytes
from .layout import SOURCE_SCHEMA, SOURCE_VERSION, k, novel_index_key, source_manifest_key, titles_key
from .store import NotFound, PreconditionFailed
from .textutil import raw_paragraphs

REMEDY = (
    'Source for this novel is not registered in the private R2 novel index. '
    'Register it with: audiobook-worker publish-novel --name "{name}" --source-dir <directory>'
)


class RegistryError(Exception):
    pass


def remedy(name: str) -> str:
    return REMEDY.format(name=name)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    if not slug or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise RegistryError(f"cannot form a slug from {name!r}")
    return slug


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().casefold()


def empty_index() -> dict:
    return {"schema": "novel-index/v1", "novels": []}


def load_index(store) -> dict:
    try:
        body = store.get_bytes(novel_index_key())
    except NotFound:
        return empty_index()
    return json.loads(body.decode())


def resolve_novel(index: dict, name: str) -> dict:
    raw = (name or "").strip()
    needle = normalize_name(raw)
    try:
        needle_slug = slugify(raw)
    except RegistryError:
        needle_slug = ""

    novels = index.get("novels") or []

    # If the caller supplied an exact lowercase slug (e.g. "dune" or "dune-1")
    # and no other novel claims that string as an alias, resolve directly to
    # that slug even when multiple uploads share the same display title ("Dune").
    if raw and raw == needle_slug:
        exact_slug = [n for n in novels if n.get("slug") == needle_slug]
        alias_elsewhere = [
            n
            for n in novels
            if n.get("slug") != needle_slug
            and any(normalize_name(a) == needle for a in (n.get("aliases") or []) if a)
        ]
        if len(exact_slug) == 1 and not alias_elsewhere:
            return exact_slug[0]

    matches = []
    for novel in novels:
        names = [novel.get("name", ""), novel.get("slug", ""), *(novel.get("aliases") or [])]
        if any(normalize_name(item) == needle for item in names if item):
            matches.append(novel)
            continue
        if needle_slug and (
            novel.get("slug") == needle_slug
            or any(slugify(item) == needle_slug for item in names if item)
        ):
            matches.append(novel)
    if not matches:
        raise RegistryError(remedy(name))
    slugs = {item["slug"] for item in matches}
    if len(slugs) != 1:
        raise RegistryError(f"novel name {name!r} matches multiple registry slugs: {sorted(slugs)}")
    return matches[0]


def _register_r2_novel_folder(store, slug: str, display_name: str, chapter_keys: list[tuple[int, str]]) -> dict:
    chapter_keys.sort(key=lambda item: item[0])

    def _build_spec(item: tuple[int, str]) -> dict:
        ch, key = item
        body = store.get_bytes(key)
        text = body.decode("utf-8")
        paragraphs = raw_paragraphs(text)
        return {
            "chapter": ch,
            "key": key,
            "sha256": sha256_bytes(body),
            "bytes": len(body),
            "paragraphs": len(paragraphs),
            "title": f"Chapter {ch}",
        }

    if len(chapter_keys) > 1 and store.__class__.__name__ == "BotoStore":
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(16, len(chapter_keys))) as pool:
            specs = list(pool.map(_build_spec, chapter_keys))
    else:
        specs = [_build_spec(item) for item in chapter_keys]
    specs.sort(key=lambda row: row["chapter"])
    chapters = [int(row["chapter"]) for row in specs]
    titles = {str(row["chapter"]): row["title"] for row in specs}
    manifest = {
        "schema": SOURCE_SCHEMA,
        "name": display_name,
        "slug": slug,
        "source_version": SOURCE_VERSION,
        "chapters": specs,
    }
    store.put_bytes(
        source_manifest_key(slug),
        (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode(),
        content_type="application/json",
    )
    store.put_bytes(
        titles_key(slug),
        (json.dumps(titles, indent=1, sort_keys=True) + "\n").encode(),
        content_type="application/json",
    )
    entry = {
        "name": display_name,
        "slug": slug,
        "aliases": [slug],
        "source_version": SOURCE_VERSION,
        "chapters": chapters,
    }
    try:
        updated = upsert_novel(load_index(store), entry)
        save_index(store, updated)
    except RegistryError:
        pass
    print(f"Auto-registered novel {display_name!r} ({slug}, {len(chapters)} chapters) from R2 novels/{slug}/", flush=True)
    return entry


def resolve_or_discover_novel(store, name: str) -> dict:
    """Resolve ``name`` in index.json, or auto-discover matching ``novels/<slug>*/chapter_*.txt*`` on R2.

    Handles multi-user uploads cleanly:
    - If two users uploaded the same book (e.g. ``dune`` and ``dune-1``), both are
      discovered. Passing ``--novel "Dune"`` reports both slugs so the caller can
      pick ``--novel dune`` or ``--novel dune-1``.
    - If a previously registered novel was deleted from R2, its stale index entry
      is ignored so the remaining copy is selected automatically.
    """
    raw = (name or "").strip()
    try:
        target_slug = slugify(raw)
    except RegistryError:
        raise RegistryError(remedy(name)) from None

    base_slug = re.sub(r"-[0-9]+$", "", target_slug)
    is_explicit_slug = raw == target_slug

    # Discover any R2 novels/<base_slug>/ or novels/<base_slug>-<n>/ folders not yet registered
    index = load_index(store)
    known_slugs = {item.get("slug") for item in (index.get("novels") or [])}
    prefix_root = k(f"novels/{base_slug}")
    heads = store.list(prefix_root)
    by_slug: dict[str, list[tuple[int, str]]] = {}
    novels_root = k("novels/")
    for head in heads:
        if not head.key.startswith(novels_root):
            continue
        rest = head.key[len(novels_root) :]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            continue
        cand_slug, rel = parts
        if cand_slug != base_slug and not re.fullmatch(rf"{re.escape(base_slug)}-[0-9]+", cand_slug):
            continue
        match = re.fullmatch(r"chapter_([0-9]+)\.txt(?:\.gz)?", rel)
        if match:
            by_slug.setdefault(cand_slug, []).append((int(match.group(1)), head.key))

    for cand_slug, ch_keys in sorted(by_slug.items()):
        if cand_slug not in known_slugs or store.head(source_manifest_key(cand_slug)) is None:
            display_name = (
                raw
                if raw and not is_explicit_slug
                else base_slug.replace("-", " ").title()
            )
            _register_r2_novel_folder(store, cand_slug, display_name, ch_keys)

    # Filter out any stale index entries whose source manifest was deleted from R2
    index = load_index(store)
    active_novels = [
        item
        for item in (index.get("novels") or [])
        if store.head(source_manifest_key(item["slug"])) is not None
    ]
    return resolve_novel({"schema": "novel-index/v1", "novels": active_novels}, name)


def upsert_novel(index: dict, entry: dict) -> dict:
    novels = [item for item in index.get("novels") or [] if item.get("slug") != entry["slug"]]
    novels.append(entry)
    novels.sort(key=lambda item: item["slug"])
    return {"schema": "novel-index/v1", "novels": novels}


def save_index(store, index: dict) -> None:
    key = novel_index_key()
    body = (json.dumps(index, indent=1, sort_keys=True) + "\n").encode()
    head = store.head(key)
    if head is None:
        try:
            store.put_bytes(key, body, content_type="application/json", if_none_match="*")
        except PreconditionFailed as exc:
            raise RegistryError("novel index was created concurrently; retry") from exc
        return
    try:
        store.put_bytes(key, body, content_type="application/json", if_match=head.etag)
    except PreconditionFailed as exc:
        raise RegistryError("novel index changed concurrently; retry") from exc
