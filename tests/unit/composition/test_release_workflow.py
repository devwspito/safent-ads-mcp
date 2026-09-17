"""Publishing a version candidate must not silently promote the stable channel."""

from pathlib import Path


def test_release_keeps_sha_and_both_architectures_without_promoting_latest():
    root = Path(__file__).resolve().parents[3]
    workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    merge = workflow.split("docker buildx imagetools create", 1)[1].split(
        "docker buildx imagetools inspect", 1
    )[0]
    assert ":latest" not in workflow
    assert '--tag "${IMAGE}:${RELEASE_TAG}"' in merge
    assert '--tag "${IMAGE}:${GITHUB_SHA}"' in merge
    assert '"${BUILD_PREFIX}-amd64" "${BUILD_PREFIX}-arm64"' in merge
    assert 'BUILD_PREFIX="${IMAGE}:build-${GITHUB_RUN_ID}"' in workflow
    assert "needs: build" in workflow
    assert "platforms: ${{ matrix.platform }}" in workflow
