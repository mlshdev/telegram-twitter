"""Tests for the Video Download API."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Set auth token before importing app
os.environ["AUTH_TOKENS"] = "test-token-123,another-token"

from main import app, get_auth_tokens, is_twitter_url, needs_quicktime_fix, verify_token


@pytest.fixture
def client():
    """Create a test client."""
    return TestClient(app)


class TestHealthEndpoint:
    """Tests for the /health endpoint."""

    def test_health_check(self, client):
        """Health endpoint returns correct status."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data


class TestAuthentication:
    """Tests for authentication."""

    def test_get_auth_tokens(self):
        """Auth tokens are parsed correctly from environment."""
        tokens = get_auth_tokens()
        assert "test-token-123" in tokens
        assert "another-token" in tokens

    def test_download_without_token(self, client):
        """Download endpoint requires authentication."""
        response = client.post(
            "/download",
            json={"url": "https://example.com/video"},
        )
        assert response.status_code == 401

    def test_download_with_invalid_token(self, client):
        """Download endpoint rejects invalid tokens."""
        response = client.post(
            "/download",
            json={"url": "https://example.com/video"},
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401

    def test_download_with_valid_token(self, client):
        """Download endpoint accepts valid tokens."""
        # This will fail at download stage, but auth should pass
        with patch("main.download_video") as mock_download:
            mock_download.side_effect = Exception("Test - not actually downloading")
            response = client.post(
                "/download",
                json={"url": "https://example.com/video"},
                headers={"Authorization": "Bearer test-token-123"},
            )
            # Should get 500 from the mock exception, not 401
            assert response.status_code == 500

    def test_bearer_prefix_handling(self, client):
        """Authorization header with Bearer prefix works."""
        with patch("main.download_video") as mock_download:
            mock_download.side_effect = Exception("Test")
            response = client.post(
                "/download",
                json={"url": "https://example.com/video"},
                headers={"Authorization": "Bearer test-token-123"},
            )
            assert response.status_code != 401


class TestTwitterUrlDetection:
    """Tests for Twitter/X URL detection."""

    def test_twitter_com(self):
        """Detects twitter.com URLs."""
        assert is_twitter_url("https://twitter.com/user/status/123")
        assert is_twitter_url("https://www.twitter.com/user/status/123")

    def test_x_com(self):
        """Detects x.com URLs."""
        assert is_twitter_url("https://x.com/user/status/123")
        assert is_twitter_url("https://www.x.com/user/status/123")

    def test_mobile_twitter(self):
        """Detects mobile Twitter URLs."""
        assert is_twitter_url("https://mobile.twitter.com/user/status/123")
        assert is_twitter_url("https://mobile.x.com/user/status/123")

    def test_non_twitter_urls(self):
        """Non-Twitter URLs are not detected as Twitter."""
        assert not is_twitter_url("https://youtube.com/watch?v=123")
        assert not is_twitter_url("https://tiktok.com/@user/video/123")
        assert not is_twitter_url("https://instagram.com/p/123")


class TestQuickTimeCompatibility:
    """Tests for QuickTime compatibility detection."""

    def test_h264_compatible(self):
        """H.264 codec is compatible."""
        video_info = {"codec_name": "h264", "sample_aspect_ratio": "1:1"}
        needs_fix, reason = needs_quicktime_fix(video_info)
        assert not needs_fix

    def test_hevc_compatible(self):
        """HEVC codec is compatible."""
        video_info = {"codec_name": "hevc", "sample_aspect_ratio": "1:1"}
        needs_fix, reason = needs_quicktime_fix(video_info)
        assert not needs_fix

    def test_vp9_incompatible(self):
        """VP9 codec requires conversion."""
        video_info = {"codec_name": "vp9", "sample_aspect_ratio": "1:1"}
        needs_fix, reason = needs_quicktime_fix(video_info)
        assert needs_fix
        assert "codec" in reason.lower()

    def test_non_square_sar(self):
        """Non-square SAR requires processing."""
        video_info = {"codec_name": "h264", "sample_aspect_ratio": "4:3"}
        needs_fix, reason = needs_quicktime_fix(video_info)
        assert needs_fix
        assert "SAR" in reason


class TestRequestValidation:
    """Tests for request validation."""

    def test_invalid_url_format(self, client):
        """Invalid URL format is rejected."""
        response = client.post(
            "/download",
            json={"url": "not-a-valid-url"},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert response.status_code == 422  # Validation error

    def test_missing_url(self, client):
        """Missing URL is rejected."""
        response = client.post(
            "/download",
            json={},
            headers={"Authorization": "Bearer test-token-123"},
        )
        assert response.status_code == 422

    def test_valid_url_accepted(self, client):
        """Valid URL passes validation."""
        with patch("main.download_video") as mock_download:
            mock_download.side_effect = Exception("Test")
            response = client.post(
                "/download",
                json={"url": "https://youtube.com/watch?v=dQw4w9WgXcQ"},
                headers={"Authorization": "Bearer test-token-123"},
            )
            # Should fail at download, not validation
            assert response.status_code == 500
