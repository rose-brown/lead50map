#!/usr/bin/env python3
"""Unit, cache-guarantee, and HTTP smoke tests for apt-commute-map."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import enrich_kakao  # noqa: E402
import fetch_kb_regions  # noqa: E402
import import_kb_table  # noqa: E402
import server  # noqa: E402


CORE18_DISTRICTS = {
    "강남구",
    "서초구",
    "송파구",
    "강동구",
    "성동구",
    "광진구",
    "동작구",
    "용산구",
    "영등포구",
    "마포구",
    "과천시",
    "성남시 분당구",
    "성남시 수정구",
    "성남시 중원구",
    "하남시",
    "안양시 동안구",
    "광명시",
    "용인시 수지구",
}


def test_import_parsers() -> None:
    assert import_kb_table.parse_market_cap_trillion("23.46조") == 23.46
    assert import_kb_table.parse_market_cap_trillion("9,800억") == 0.98
    assert import_kb_table.parse_price_per_pyeong_man("8,920만원") == 8920
    assert import_kb_table.parse_households("8,109세대") == 8109

    assert import_kb_table.parse_built_ym("71년 12월 (56년차)") == ("1971-12", 1971)
    assert import_kb_table.parse_built_ym("91년 09월 (36년차)") == ("1991-09", 1991)
    assert import_kb_table.parse_built_ym("18년 12월 (9년차)") == ("2018-12", 2018)
    assert import_kb_table.parse_built_ym("25년 12월 (2년차)") == ("2025-12", 2025)

    merged = import_kb_table.merge_and_deduplicate(
        lead50_rows=[
            {
                "rank": 38,
                "name": "래미안슈르",
                "location": "경기 원문동",
                "marketCap": "6.18조",
                "pricePerPyeong": "6,353만원",
                "households": "2,899세대",
                "builtYM": "08년 08월 (19년차)",
            }
        ],
        gyeonggi_rows=[
            {
                "rank": 1,
                "name": "래미안슈르",
                "location": "과천시 원문동",
                "marketCap": "6.18조",
                "pricePerPyeong": "6,353만원",
                "households": "2,899세대",
                "builtYM": "08년 08월 (19년차)",
            }
        ],
    )
    assert len(merged) == 1
    item = merged[0]
    assert item["location"] == "과천시 원문동"
    assert item["district"] == "과천시"
    assert item["sources"] == ["kb_lead50", "gyeonggi_top50"]
    assert item["lead50Rank"] == 38
    assert item["gyeonggiTopRank"] == 1


def test_fetch_regions_merge() -> None:
    base = [
        {
            "id": "seoul-개포동-디에이치퍼스티어아이파크",
            "name": "디에이치퍼스티어아이파크",
            "region": "서울",
            "district": "",
            "location": "서울 개포동",
            "dong": "개포동",
            "sources": ["kb_lead50"],
            "lead50Rank": 2,
            "gyeonggiTopRank": None,
            "marketCapTrillion": 21.62,
            "pricePerPyeongMan": 10725,
            "households": 6089,
            "builtYM": "2023-11",
            "builtYear": 2023,
            "builtText": "23년 11월 (4년차)",
        }
    ]
    batches = {
        "서울 강남구": {
            "region": "서울",
            "district": "강남구",
            "rows": [
                {
                    "단지기본일련번호": 50176,
                    "아파트명": "디에이치퍼스티어아이파크",
                    "지역명": "강남구 개포동",
                    "읍면동명": "개포동",
                    "순위": 1,
                    "시세총액": "21.62조",
                    "평당시세값": 10725,
                    "세대수값": 6089,
                    "준공년월값": 202311,
                    "준공년월": "23년 11월 (4년차)",
                }
            ],
        }
    }
    merged = fetch_kb_regions.merge_complex_lists(base, batches)
    assert len(merged) == 1
    assert merged[0]["id"] == "seoul-개포동-디에이치퍼스티어아이파크"
    assert merged[0]["district"] == "강남구"
    assert merged[0]["location"] == "강남구 개포동"
    assert merged[0]["districtRank"] == 1
    assert merged[0]["kbComplexId"] == 50176
    assert set(merged[0]["sources"]) == {"kb_lead50", "district_top50"}


def test_kakao_parsers_and_once_only_cache() -> None:
    dist = enrich_kakao.haversine_km(37.4977583, 127.1070101, 37.5073809, 127.0568213)
    assert 4.0 <= dist <= 5.5

    sample_transit = {
        "status": "OK",
        "properties": {"landingURL": "https://map.kakao.com/?rt=sample"},
        "routes": [
            {
                "properties": {
                    "type": "SUBWAY",
                    "totalDistance": 12450,
                    "totalTime": 2450,
                    "transfers": 1,
                    "fare": {"value": 1500},
                },
                "steps": [
                    {"properties": {"type": "WALKING", "time": 180, "guidance": "역까지 도보"}},
                    {
                        "properties": {"type": "SUBWAY", "time": 900},
                        "vehicles": [{"name": "8호선"}],
                        "stops": [{"name": "송파"}, {"name": "석촌"}, {"name": "잠실"}],
                    },
                    {
                        "properties": {"type": "SUBWAY", "time": 700},
                        "vehicles": [{"name": "2호선"}],
                        "stops": [{"name": "잠실"}, {"name": "삼성"}],
                    },
                ],
            }
        ],
    }
    parsed = enrich_kakao.parse_publictraffic_response(sample_transit)
    assert parsed["minutes"] == 41
    assert parsed["transfers"] == 1
    assert parsed["fareWon"] == 1500
    assert parsed["landingUrl"] == "https://map.kakao.com/?rt=sample"
    assert "8호선(송파→잠실)" in parsed["summary"]
    assert "2호선(잠실→삼성)" in parsed["summary"]
    assert parsed["verified"] is True

    apt_doc = {
        "place_name": "헬리오시티아파트",
        "address_name": "서울 송파구 가락동 913",
        "road_address_name": "서울 송파구 송파대로 345",
        "category_name": "부동산 > 주거시설 > 아파트",
    }
    broker_doc = {
        "place_name": "헬리오시티 공인중개사사무소",
        "address_name": "서울 송파구 가락동 913",
        "road_address_name": "서울 송파구 송파대로 345",
        "category_name": "부동산 > 공인중개사",
    }
    assert enrich_kakao.score_kakao_place(apt_doc, "서울", "가락동", "헬리오시티") > enrich_kakao.score_kakao_place(
        broker_doc, "서울", "가락동", "헬리오시티"
    )

    # Strict once-only cache guarantee test: cached entries must NEVER call fetcher!
    dest = {"name": "테헤란로 501", "lat": 37.5073809, "lng": 127.0568213}
    cache = enrich_kakao.empty_cache(dest)
    ckey = enrich_kakao.make_coord_key(37.4977583, 127.1070101, 37.5073809, 127.0568213)
    cache["transitByComplexKey"]["id-1"] = dict(parsed)
    cache["transitByCoordKey"][ckey] = dict(parsed)

    apts = [
        {
            "id": "id-1",
            "name": "헬리오시티",
            "location": "송파구 가락동",
            "lat": 37.4977583,
            "lng": 127.1070101,
            "coordsVerified": True,
            "commute": {"verified": False, "minutes": None},
        },
        {
            "id": "id-2-same-coords",
            "name": "헬리오시티복제",
            "location": "송파구 가락동",
            "lat": 37.4977583,
            "lng": 127.1070101,
            "coordsVerified": True,
            "commute": {"verified": False, "minutes": None},
        },
    ]

    def fail_if_called(url: str, params: dict, key: str) -> dict:
        raise AssertionError(f"Transit API should NOT be called when cached! url={url} params={params}")

    stats = enrich_kakao.run_transit_enrichment(
        apartments=apts,
        dest=dest,
        cache=cache,
        rest_key="dummy",
        fetcher=fail_if_called,
        sleep_sec=0.0,
    )
    assert stats["api_calls"] == 0
    assert stats["cache_hits"] == 2
    assert apts[0]["commute"]["minutes"] == 41
    assert apts[1]["commute"]["minutes"] == 41


def test_generated_datasets() -> None:
    raw_path = PROJECT_ROOT / "data" / "kb_raw.json"
    app_path = PROJECT_ROOT / "docs" / "data" / "apartments.json"
    cache_path = PROJECT_ROOT / "data" / "kakao_transit_cache.json"

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    app = json.loads(app_path.read_text(encoding="utf-8"))
    cache = json.loads(cache_path.read_text(encoding="utf-8"))

    assert len(raw["complexes"]) >= 500
    assert len(app["apartments"]) == len(raw["complexes"])
    assert app["meta"]["totalCount"] == len(app["apartments"])

    present_districts = {apt.get("district") for apt in app["apartments"]}
    assert "" not in present_districts and None not in present_districts
    assert CORE18_DISTRICTS.issubset(present_districts)

    assert len(cache.get("transitByComplexKey") or {}) >= 97
    assert len(cache.get("coordsByComplexKey") or {}) >= 97

    for apt in app["apartments"]:
        assert apt["name"]
        assert apt["region"] in ("서울", "경기")
        assert apt["district"]
        assert apt["location"]
        assert apt["dong"]
        assert apt["pricePerPyeongMan"] > 0
        assert apt["households"] > 0
        assert len(apt["builtYM"]) == 7
        assert 1960 <= apt["builtYear"] <= 2028


def http_get(url: str) -> tuple:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", errors="replace")


def test_http_server() -> None:
    srv = server.ReusableThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(PROJECT_ROOT))
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, html = http_get(f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "테헤란로 501 출근권" in html

        status, cfg_txt = http_get(f"http://127.0.0.1:{port}/api/config")
        assert status == 200
        cfg = json.loads(cfg_txt)
        assert "hasKakaoJsKey" in cfg
        assert "hasRestKey" in cfg

        status, static_cfg_txt = http_get(f"http://127.0.0.1:{port}/config.json")
        assert status == 200
        static_cfg = json.loads(static_cfg_txt)
        assert static_cfg.get("hasRestKey") is False
        assert "hasKakaoJsKey" in static_cfg

        status, data_txt = http_get(f"http://127.0.0.1:{port}/data/apartments.json")
        assert status == 200
        data = json.loads(data_txt)
        assert len(data["apartments"]) >= 500

        status_env, _ = http_get(f"http://127.0.0.1:{port}/.env")
        assert status_env == 404
        status_trav, _ = http_get(f"http://127.0.0.1:{port}/../.env")
        assert status_trav == 404
    finally:
        srv.shutdown()
        srv.server_close()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        (tmp_root / "docs").mkdir()
        (tmp_root / "docs" / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
        (tmp_root / ".env").write_text(
            'VITE_KAKAO_JS_KEY="test-js-key-abc"\nKAKAO_REST_API_KEY="super-secret-rest-key-999"\n',
            encoding="utf-8",
        )
        srv2 = server.ReusableThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(tmp_root))
        port2 = srv2.server_address[1]
        thread2 = threading.Thread(target=srv2.serve_forever, daemon=True)
        thread2.start()
        try:
            status2, cfg2_txt = http_get(f"http://127.0.0.1:{port2}/api/config")
            assert status2 == 200
            assert "super-secret-rest-key-999" not in cfg2_txt
            cfg2 = json.loads(cfg2_txt)
            assert cfg2["kakaoJsKey"] == "test-js-key-abc"
            assert cfg2["hasKakaoJsKey"] is True
            assert cfg2["hasRestKey"] is True

            status2_env, _ = http_get(f"http://127.0.0.1:{port2}/.env")
            assert status2_env == 404
        finally:
            srv2.shutdown()
            srv2.server_close()


def main() -> int:
    test_import_parsers()
    test_fetch_regions_merge()
    test_kakao_parsers_and_once_only_cache()
    test_generated_datasets()
    test_http_server()
    print("[test_smoke] All assertions passed (KB region merge, once-only cache, datasets, HTTP security).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
