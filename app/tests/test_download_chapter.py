"""
Integration tests for the /download/chapter endpoint.

Uses Oathbringer chapter 82 as the primary test fixture.
Requires a running server at http://localhost:8000.

Run with:
    pytest app/tests/test_download_chapter.py -v
"""

import asyncio
import time
import pytest
import httpx
import os

BASE_URL = os.environ.get("TEST_BASE_URL", "http://localhost:8000")
OATHBRINGER_NOVEL = "oathbringer-the-stormlight-archive-book-3"
OATHBRINGER_CHAPTER = 82
NARRATOR_VOICE = "en-US-ChristopherNeural"
DIALOGUE_VOICE = "en-US-JennyNeural"

# How long the full chapter download must complete in (seconds)
PERFORMANCE_DEADLINE_S = 30.0
# Poll interval when waiting for completion
POLL_INTERVAL_S = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def start_download(client: httpx.Client) -> dict:
    """POST /download/chapter and return the parsed JSON response."""
    resp = client.post(
        f"{BASE_URL}/download/chapter",
        json={
            "novel_name": OATHBRINGER_NOVEL,
            "chapter_number": OATHBRINGER_CHAPTER,
            "narrator_voice": NARRATOR_VOICE,
            "dialogue_voice": DIALOGUE_VOICE,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def poll_until_done(client: httpx.Client, download_id: str, timeout_s: float) -> dict:
    """Poll GET /download/status/{id} until status is 'completed' or 'error'."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"{BASE_URL}/download/status/{download_id}", timeout=10)
        resp.raise_for_status()
        status = resp.json()
        if status["status"] in ("completed", "error"):
            return status
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"Download {download_id} did not complete within {timeout_s}s. "
        f"Last status: {status}"
    )


def cleanup_download(client: httpx.Client, download_id: str):
    try:
        client.delete(f"{BASE_URL}/download/{download_id}", timeout=10)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def chapter_content():
    """Fetch chapter content once for the whole module."""
    with httpx.Client() as client:
        resp = client.get(
            f"{BASE_URL}/chapter",
            params={"chapterNumber": OATHBRINGER_CHAPTER, "novelName": OATHBRINGER_NOVEL},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Tests: chapter content endpoint
# ---------------------------------------------------------------------------

class TestChapterContent:
    def test_chapter_returns_200(self, chapter_content):
        assert chapter_content is not None

    def test_chapter_has_expected_fields(self, chapter_content):
        assert "chapterNumber" in chapter_content
        assert "chapterTitle" in chapter_content
        assert "content" in chapter_content

    def test_chapter_number_matches(self, chapter_content):
        assert chapter_content["chapterNumber"] == OATHBRINGER_CHAPTER

    def test_chapter_has_paragraphs(self, chapter_content):
        assert len(chapter_content["content"]) > 0, "Chapter must have at least one paragraph"

    def test_chapter_82_has_many_paragraphs(self, chapter_content):
        # Oathbringer ch82 is a full chapter — expect > 50 paragraphs
        assert len(chapter_content["content"]) > 50, (
            f"Expected >50 paragraphs, got {len(chapter_content['content'])}"
        )

    def test_chapter_title_is_nonempty(self, chapter_content):
        assert chapter_content["chapterTitle"].strip() != ""

    def test_paragraphs_are_nonempty_strings(self, chapter_content):
        for i, para in enumerate(chapter_content["content"]):
            assert isinstance(para, str), f"Paragraph {i} is not a string"
            assert para.strip() != "", f"Paragraph {i} is empty"


# ---------------------------------------------------------------------------
# Tests: download/chapter endpoint — lifecycle & structure
# ---------------------------------------------------------------------------

class TestDownloadChapterLifecycle:
    def test_start_download_returns_download_id(self):
        with httpx.Client() as client:
            result = start_download(client)
            assert "download_id" in result, "Response must include download_id"
            assert result["download_id"], "download_id must not be empty"
            cleanup_download(client, result["download_id"])

    def test_start_download_status_is_pending(self):
        with httpx.Client() as client:
            result = start_download(client)
            assert result["status"] == "pending"
            cleanup_download(client, result["download_id"])

    def test_status_endpoint_returns_data(self):
        with httpx.Client() as client:
            result = start_download(client)
            download_id = result["download_id"]
            try:
                resp = client.get(f"{BASE_URL}/download/status/{download_id}", timeout=10)
                assert resp.status_code == 200
                status = resp.json()
                assert "status" in status
                assert "progress" in status
            finally:
                cleanup_download(client, download_id)

    def test_status_unknown_id_returns_404(self):
        with httpx.Client() as client:
            resp = client.get(
                f"{BASE_URL}/download/status/nonexistent-id-00000000",
                timeout=10,
            )
            assert resp.status_code == 404

    def test_cleanup_removes_download(self):
        with httpx.Client() as client:
            result = start_download(client)
            download_id = result["download_id"]
            # Allow it to start
            time.sleep(0.5)
            del_resp = client.delete(f"{BASE_URL}/download/{download_id}", timeout=10)
            assert del_resp.status_code == 200
            # After cleanup, status must 404
            status_resp = client.get(
                f"{BASE_URL}/download/status/{download_id}", timeout=10
            )
            assert status_resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests: download/chapter endpoint — correctness
# ---------------------------------------------------------------------------

class TestDownloadChapterCorrectness:
    @pytest.fixture(scope="class")
    def completed_download(self):
        """Start a download and wait for it to complete (or fail)."""
        with httpx.Client() as client:
            result = start_download(client)
            download_id = result["download_id"]
            try:
                status = poll_until_done(client, download_id, timeout_s=120)
                yield client, download_id, status
            finally:
                cleanup_download(client, download_id)

    def test_download_completes_successfully(self, completed_download):
        _, _, status = completed_download
        assert status["status"] == "completed", (
            f"Download did not complete: {status}"
        )

    def test_progress_reaches_100(self, completed_download):
        _, _, status = completed_download
        assert status["progress"] == 100

    def test_files_manifest_present(self, completed_download):
        _, _, status = completed_download
        assert status.get("files") is not None, "files manifest must be present"

    def test_manifest_has_content_json(self, completed_download):
        _, _, status = completed_download
        assert "content" in status["files"]

    def test_manifest_has_title_audio(self, completed_download):
        _, _, status = completed_download
        assert "title" in status["files"]["audio"]

    def test_manifest_has_paragraph_audios(self, completed_download, chapter_content):
        _, _, status = completed_download
        expected = len(chapter_content["content"])
        actual = len(status["files"]["audio"]["paragraphs"])
        assert actual == expected, (
            f"Expected {expected} paragraph audio files, got {actual}"
        )

    def test_content_json_is_downloadable(self, completed_download):
        client, download_id, status = completed_download
        url = f"{BASE_URL}{status['files']['content']}"
        resp = client.get(url, timeout=10)
        assert resp.status_code == 200
        data = resp.json()
        assert "paragraphs" in data
        assert data["chapter_number"] == OATHBRINGER_CHAPTER

    def test_title_audio_is_downloadable(self, completed_download):
        client, download_id, status = completed_download
        url = f"{BASE_URL}{status['files']['audio']['title']}"
        resp = client.get(url, timeout=10)
        assert resp.status_code == 200
        assert len(resp.content) > 0, "title.mp3 must not be empty"

    def test_first_paragraph_audio_is_downloadable(self, completed_download):
        client, download_id, status = completed_download
        url = f"{BASE_URL}{status['files']['audio']['paragraphs'][0]}"
        resp = client.get(url, timeout=10)
        assert resp.status_code == 200
        assert len(resp.content) > 0, "0.mp3 must not be empty"


# ---------------------------------------------------------------------------
# Tests: download/chapter endpoint — PERFORMANCE
# ---------------------------------------------------------------------------

class TestDownloadChapterPerformance:
    def test_chapter_82_completes_within_30_seconds(self):
        """
        Core performance requirement: Oathbringer ch82 (147 paragraphs, ~23k chars)
        must complete in under 30 seconds.
        """
        with httpx.Client() as client:
            t_start = time.monotonic()
            result = start_download(client)
            download_id = result["download_id"]
            try:
                status = poll_until_done(
                    client, download_id, timeout_s=PERFORMANCE_DEADLINE_S
                )
                elapsed = time.monotonic() - t_start
                assert status["status"] == "completed", (
                    f"Download failed after {elapsed:.1f}s: {status}"
                )
                assert elapsed <= PERFORMANCE_DEADLINE_S, (
                    f"Download took {elapsed:.1f}s — exceeds {PERFORMANCE_DEADLINE_S}s limit"
                )
            finally:
                cleanup_download(client, download_id)

    def test_progress_increases_monotonically(self):
        """Progress values should never go backwards."""
        with httpx.Client() as client:
            result = start_download(client)
            download_id = result["download_id"]
            try:
                last_progress = -1
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    resp = client.get(
                        f"{BASE_URL}/download/status/{download_id}", timeout=10
                    )
                    resp.raise_for_status()
                    status = resp.json()
                    current = status["progress"]
                    assert current >= last_progress, (
                        f"Progress went backwards: {last_progress} → {current}"
                    )
                    last_progress = current
                    if status["status"] in ("completed", "error"):
                        break
                    time.sleep(POLL_INTERVAL_S)
            finally:
                cleanup_download(client, download_id)


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------

class TestDownloadChapterErrorHandling:
    def test_invalid_novel_name_returns_error(self):
        with httpx.Client() as client:
            resp = client.post(
                f"{BASE_URL}/download/chapter",
                json={
                    "novel_name": "this-novel-does-not-exist-xyz-abc-123",
                    "chapter_number": 1,
                    "narrator_voice": NARRATOR_VOICE,
                    "dialogue_voice": DIALOGUE_VOICE,
                },
                timeout=30,
            )
            # POST itself succeeds (task queued)
            assert resp.status_code == 200
            download_id = resp.json()["download_id"]
            try:
                status = poll_until_done(client, download_id, timeout_s=30)
                assert status["status"] == "error", (
                    f"Expected error status for invalid novel, got: {status}"
                )
            finally:
                cleanup_download(client, download_id)

    def test_invalid_chapter_number_returns_error(self):
        with httpx.Client() as client:
            resp = client.post(
                f"{BASE_URL}/download/chapter",
                json={
                    "novel_name": OATHBRINGER_NOVEL,
                    "chapter_number": 999999,
                    "narrator_voice": NARRATOR_VOICE,
                    "dialogue_voice": DIALOGUE_VOICE,
                },
                timeout=30,
            )
            assert resp.status_code == 200
            download_id = resp.json()["download_id"]
            try:
                status = poll_until_done(client, download_id, timeout_s=30)
                assert status["status"] == "error", (
                    f"Expected error for nonexistent chapter, got: {status}"
                )
            finally:
                cleanup_download(client, download_id)

    def test_missing_required_fields_returns_422(self):
        with httpx.Client() as client:
            resp = client.post(
                f"{BASE_URL}/download/chapter",
                json={"novel_name": OATHBRINGER_NOVEL},  # missing required fields
                timeout=10,
            )
            assert resp.status_code == 422

    def test_file_not_found_returns_404(self):
        with httpx.Client() as client:
            result = start_download(client)
            download_id = result["download_id"]
            try:
                resp = client.get(
                    f"{BASE_URL}/download/file/{download_id}/nonexistent.mp3",
                    timeout=10,
                )
                assert resp.status_code == 404
            finally:
                cleanup_download(client, download_id)
