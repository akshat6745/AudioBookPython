#!/usr/bin/env python3
"""Queue one audiobook render task on the deployed Beam task queue (used by the GitHub Actions workflow).

  BEAM_TOKEN=... python queue_beam_task.py deploy.json render_request.json
deploy.json is the output of `beam deploy ... --json` (the task queue URL is taken from it); render_request.json
holds the task arguments: novels (empty = all tagged novels), chapters (e.g. "1-3"), retry_held.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request


def deploy_url(text: str) -> str:
    urls = re.findall(r"https://[\w.-]+\.beam\.cloud[\w/.-]*", text)
    if not urls:
        sys.exit("no Beam URL in the deploy output")
    return urls[0]


def payload(request: dict) -> dict:
    body: dict = {}
    if request.get("novels"):
        body["novels"] = list(request["novels"])
    if str(request.get("chapters") or "").strip():
        body["chapters"] = str(request["chapters"]).strip()
    if request.get("retry_held") is True:
        body["retry_held"] = True
    return body


def main() -> None:
    url = deploy_url(open(sys.argv[1]).read())
    body = payload(json.load(open(sys.argv[2])))
    for scheme in ("Bearer", "Basic"):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"{scheme} {os.environ['BEAM_TOKEN']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                print(f"queued {json.dumps(body)} -> {r.read().decode()}")
                return
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
    sys.exit("Beam rejected the token (401)")


if __name__ == "__main__":
    main()
