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
    needle = normalize_name(name)
    try:
        needle_slug = slugify(name)
    except RegistryError:
        needle_slug = ""
    matches = []
    for novel in index.get("novels") or []:
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


def resolve_or_discover_novel(store, name: str) -> dict:
    """Resolve ``name`` in index.json, or auto-register it if ``novels/<slug>/chapter_*.txt*`` exists on R2."""
    index = load_index(store)
    try:
        return resolve_novel(index, name)
    except RegistryError:
        pass
    try:
        slug = slugify(name)
    except RegistryError:
        raise RegistryError(remedy(name)) from None
    prefix = k(f"novels/{slug}/")
    heads = store.list(prefix)
    chapter_keys: list[tuple[int, str]] = []
    for head in heads:
        rel = head.key[len(prefix) :] if head.key.startswith(prefix) else ""
        match = re.fullmatch(r"chapter_([0-9]+)\.txt(?:\.gz)?", rel)
        if match:
            chapter_keys.append((int(match.group(1)), head.key))
    if not chapter_keys:
        raise RegistryError(remedy(name))
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
    display_name = name.strip() if name.strip() and name.strip() != slug else slug.replace("-", " ").title()
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


def upsert_novel(index: dict, entry: dict) -> dict:
    novels = [item for item in index.get("novels") or [] if item.get("slug") != entry["slug"]]
    for item in novels:
        if normalize_name(item.get("name", "")) == normalize_name(entry["name"]):
            raise RegistryError(f"name {entry['name']!r} is already registered as {item['slug']}")
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
