"""Hardware probe parsers and recommend/HF API contract tests (no network)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import (
    ModelInfo,
    Profile,
    Provider,
    RecommendRequest,
    SystemInfo,
)
from app.models.catalog import clear_catalog_cache, load_catalog
from app.models.hf_metrics import (
    clear_hf_cache,
    fetch_hf_metrics,
    is_safe_hf_id,
)
from app.models.recommend import (
    model_fits,
    recommend_models,
    score_candidate,
)
from app.obsidian import ObsidianClient
from app.system.hardware import parse_nvidia_smi_csv, probe_ram


@pytest.fixture
def client(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """Boot the app against temp settings; stub Obsidian probes."""

    async def available(_self: ObsidianClient) -> bool:
        """Always report plugin offline in tests."""
        return False

    monkeypatch.setattr(ObsidianClient, "available", available)

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_parse_nvidia_smi_csv() -> None:
    text = "NVIDIA GeForce RTX 4090, 24564, 22000\nTesla T4, 15360, 12000\n"
    gpus = parse_nvidia_smi_csv(text)
    assert len(gpus) == 2
    assert gpus[0].name.startswith("NVIDIA")
    assert gpus[0].vram_total_mb == 24564
    assert gpus[1].vram_free_mb == 12000


def test_probe_ram_does_not_raise() -> None:
    total, avail, errors = probe_ram()
    assert isinstance(errors, list)
    # On a normal developer machine at least one figure is available.
    assert total is None or total > 0
    assert avail is None or avail >= 0


def test_catalog_loads_curated_entries() -> None:
    clear_catalog_cache()
    entries = load_catalog(force=True)
    assert "nomic-embed-text" in entries
    assert "qwen2.5:3b" in entries
    assert entries["nomic-embed-text"].roles["embedding"] >= 80


def test_is_safe_hf_id_rejects_traversal() -> None:
    assert is_safe_hf_id("nomic-ai/nomic-embed-text-v1.5")
    assert not is_safe_hf_id("../etc/passwd")
    assert not is_safe_hf_id("/abs/path")
    assert not is_safe_hf_id("no-slash")


@pytest.mark.asyncio
async def test_hf_metrics_cache_hit_with_mock_transport(
    tmp_settings: Settings,
) -> None:
    clear_hf_cache()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.host == "huggingface.co"
        return httpx.Response(
            200,
            json={
                "downloads": 1_000_000,
                "likes": 500,
                "pipeline_tag": "feature-extraction",
                "lastModified": "2024-01-01T00:00:00.000Z",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=3.0) as client:
        first, degraded1 = await fetch_hf_metrics(
            "nomic-ai/nomic-embed-text-v1.5", client=client
        )
        second, degraded2 = await fetch_hf_metrics(
            "nomic-ai/nomic-embed-text-v1.5", client=client
        )
    assert first is not None and not degraded1
    assert second is not None and not degraded2
    assert second.from_cache is True
    assert calls["n"] == 1
    assert first.downloads == 1_000_000


@pytest.mark.asyncio
async def test_hf_metrics_fail_soft(
    tmp_settings: Settings,
) -> None:
    clear_hf_cache()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=3.0) as client:
        metrics, degraded = await fetch_hf_metrics(
            "Qwen/Qwen2.5-3B-Instruct", client=client
        )
    assert metrics is None
    assert degraded is True


def test_scorer_prefers_fitting_over_oversized() -> None:
    system = SystemInfo(ram_total_mb=16_000, ram_available_mb=8_000, cpu_cores=8, gpus=[])
    small = ModelInfo(
        id="qwen2.5:3b",
        provider=Provider.OLLAMA,
        label="small",
        est_vram_mb=2800,
        role_scores={"rerank": 88, "chat": 62, "chunk_decision": 90, "extraction": 55},
        available=True,
        tier="tiny",
    )
    huge = ModelInfo(
        id="huge:70b",
        provider=Provider.OLLAMA,
        label="huge",
        est_vram_mb=40_000,
        role_scores={"rerank": 99, "chat": 99, "chunk_decision": 90, "extraction": 99},
        available=True,
        tier="large",
    )
    models = {small.id: small, huge.id: huge}
    profile = Profile()
    s_small = score_candidate(
        small,
        "rerank",
        system,
        online=False,
        metrics=None,
        metrics_degraded=False,
        profile=profile,
        models=models,
    )
    s_huge = score_candidate(
        huge,
        "rerank",
        system,
        online=False,
        metrics=None,
        metrics_degraded=False,
        profile=profile,
        models=models,
    )
    assert model_fits(small, system)
    assert not model_fits(huge, system)
    assert s_small.score > s_huge.score


@pytest.mark.asyncio
async def test_recommend_ranks_embedding_role() -> None:
    models = {
        "nomic-embed-text": ModelInfo(
            id="nomic-embed-text",
            provider=Provider.OLLAMA,
            label="nomic",
            is_embedding=True,
            dimensions=768,
            available=True,
            est_vram_mb=500,
            role_scores={"embedding": 92},
            hf_id="nomic-ai/nomic-embed-text-v1.5",
            tier="tiny",
        ),
        "qwen2.5:3b": ModelInfo(
            id="qwen2.5:3b",
            provider=Provider.OLLAMA,
            label="chat",
            available=True,
            est_vram_mb=2800,
            role_scores={"chat": 62, "rerank": 88},
            tier="tiny",
        ),
    }
    system = SystemInfo(ram_total_mb=32_000, ram_available_mb=20_000, cpu_cores=8)
    result = await recommend_models(
        RecommendRequest(roles=["embedding"], online=False),
        models=models,
        profile=Profile(),
        system=system,
    )
    assert len(result.roles) == 1
    assert result.roles[0].role == "embedding"
    assert result.roles[0].recommendations[0].id == "nomic-embed-text"
    assert result.online is False


def test_api_system(client: TestClient) -> None:
    response = client.get("/api/system")
    assert response.status_code == 200
    body = response.json()
    assert "ram_total_mb" in body
    assert "gpus" in body
    assert "probe_errors" in body


def test_api_recommend(client: TestClient) -> None:
    response = client.post(
        "/api/models/recommend",
        json={"roles": ["chat", "embedding"], "online": False, "top_n": 3},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["online"] is False
    roles = {row["role"] for row in body["roles"]}
    assert roles == {"chat", "embedding"}
    for row in body["roles"]:
        assert isinstance(row["recommendations"], list)


@pytest.mark.asyncio
async def test_hf_metrics_rejects_redirect(
    tmp_settings: Settings,
) -> None:
    clear_hf_cache()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/metrics"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, timeout=3.0, follow_redirects=False
    ) as client:
        metrics, degraded = await fetch_hf_metrics(
            "Qwen/Qwen2.5-3B-Instruct", client=client
        )
    assert metrics is None
    assert degraded is True


def test_api_recommend_rejects_apply_true(client: TestClient) -> None:
    before = client.get("/api/profile").json()
    response = client.post(
        "/api/models/recommend",
        json={"roles": ["chat"], "online": False, "apply": True},
    )
    assert response.status_code == 400
    after = client.get("/api/profile").json()
    assert after == before


def test_api_recommend_default_online_off(client: TestClient) -> None:
    response = client.post("/api/models/recommend", json={"roles": ["chat"]})
    assert response.status_code == 200
    assert response.json()["online"] is False


def test_normalize_roles_supported_set() -> None:
    from app.models import RECOMMEND_ROLES
    from app.models.recommend import normalize_roles

    assert normalize_roles(None) == list(RECOMMEND_ROLES)
    assert normalize_roles(["chat", "coding", "chat"]) == ["chat"]
    assert set(normalize_roles(["nope"])) == set(RECOMMEND_ROLES)
    assert "voice" in RECOMMEND_ROLES
