"""Private novel registry. Names resolve only through this index, not a guessed slug."""

from __future__ import annotations

import json
import re

from .layout import novel_index_key
from .store import NotFound, PreconditionFailed

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
