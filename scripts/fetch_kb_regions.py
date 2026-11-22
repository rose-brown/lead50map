#!/usr/bin/env python3
"""Fetch 시/구-level TOP 50 apartment rankings from KB Land API and merge safely with permanent Kakao cache."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import enrich_kakao  # noqa: E402
import import_kb_table  # noqa: E402


CORE18_REGIONS: List[Tuple[str, str]] = [
    ("서울", "강남구"),
    ("서울", "서초구"),
    ("서울", "송파구"),
    ("서울", "강동구"),
    ("서울", "성동구"),
    ("서울", "광진구"),
    ("서울", "동작구"),
    ("서울", "용산구"),
    ("서울", "영등포구"),
    ("서울", "마포구"),
    ("경기", "과천시"),
    ("경기", "성남시 분당구"),
    ("경기", "성남시 수정구"),
    ("경기", "성남시 중원구"),
    ("경기", "하남시"),
    ("경기", "안양시 동안구"),
    ("경기", "광명시"),
    ("경기", "용인시 수지구"),
]

SIDO_CODES = {
    "서울": "1100000000",
    "경기": "4100000000",
}

KB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.kbland.kr/",
    "Accept": "application/json",
}


def kb_get_json(url: str, params: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
    full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
    req = urllib.request.Request(full_url, headers=KB_HEADERS, method="GET")
    last_err: Optional[Exception] = None
    backoffs = (2, 4, 8)
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt <= len(backoffs):
                time.sleep(backoffs[attempt - 1])
    assert last_err is not None
    raise last_err


def fetch_sigungu_catalog() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Fetch official 10-digit 법정동코드 catalog for Seoul and Gyeonggi leaf 시/군/구."""
    catalog: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for region, sido_code in SIDO_CODES.items():
        res = kb_get_json(
            "https://data-api.kbland.kr/bfmavm/map/siGunGuAreaNameList",
            {"법정동코드": sido_code},
        )
        items = (res.get("dataBody") or {}).get("data") or []
        for item in items:
            name = " ".join(str(item.get("시군구명") or "").strip().split())
            if not name:
                continue
            catalog[(region, name)] = {
                "region": region,
                "district": name,
                "code": str(item.get("법정동코드") or "").strip(),
                "hasChildren": str(item.get("하위시군구존재여부") or "0") == "1",
            }
    return catalog


def parse_regions_arg(regions_arg: str) -> List[Tuple[str, str]]:
    """Parse comma-separated region specs like '서울 양천구,경기 의왕시'."""
    parsed: List[Tuple[str, str]] = []
    for raw in regions_arg.split(","):
        cleaned = " ".join(raw.strip().split())
        if not cleaned:
            continue
        parts = cleaned.split(" ", 1)
        if len(parts) != 2 or parts[0] not in ("서울", "경기"):
            raise ValueError(f"지역 형식은 '서울 OO구' 또는 '경기 OO시'여야 합니다: {raw}")
        parsed.append((parts[0], parts[1]))
    return parsed


def parse_kb_api_row(item: Dict[str, Any], region: str, district: str) -> Dict[str, Any]:
    """Convert one item from KB TOP아파트순위정보.데이터리스트 into normalized complex schema."""
    name = str(item.get("아파트명") or "").strip()
    if not name:
        raw_danji = str(item.get("단지명") or "").strip()
        name = raw_danji.split("(")[0].strip()

    location = " ".join(str(item.get("지역명") or item.get("단지지역명") or "").strip().split())
    dong = str(item.get("읍면동명") or "").strip()
    if not dong and location:
        dong = location.split(" ")[-1]

    if not location:
        location = f"{district} {dong}".strip()

    ym_val = str(item.get("준공년월값") or "").strip()
    built_text = str(item.get("준공년월") or "").strip()
    if re_match := (len(ym_val) == 6 and ym_val.isdigit()):
        _ = re_match
        built_year = int(ym_val[:4])
        built_ym = f"{ym_val[:4]}-{ym_val[4:6]}"
    else:
        built_ym, built_year = import_kb_table.parse_built_ym(built_text)

    if item.get("시세총액값") is not None:
        market_cap = round(float(item["시세총액값"]) / 100000000.0, 4)
    else:
        market_cap = import_kb_table.parse_market_cap_trillion(item.get("시세총액"))

    if item.get("평당시세값") is not None:
        price_per_pyeong = int(round(float(item["평당시세값"])))
    else:
        price_per_pyeong = import_kb_table.parse_price_per_pyeong_man(item.get("평당시세"))

    if item.get("세대수값") is not None:
        households = int(item["세대수값"])
    else:
        households = import_kb_table.parse_households(item.get("세대수"))

    rank = int(item.get("순위") or item.get("RN") or 0)
    kb_id = item.get("단지기본일련번호")

    return {
        "id": import_kb_table.make_complex_id(region, dong, name),
        "name": name,
        "region": region,
        "district": district,
        "location": location,
        "dong": dong,
        "sources": ["district_top50"],
        "lead50Rank": None,
        "gyeonggiTopRank": None,
        "districtRank": rank,
        "kbComplexId": int(kb_id) if kb_id is not None and str(kb_id).isdigit() else kb_id,
        "marketCapTrillion": market_cap,
        "pricePerPyeongMan": price_per_pyeong,
        "households": households,
        "builtYM": built_ym,
        "builtYear": built_year,
        "builtText": built_text or built_ym,
    }


def _dedup_key(c: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(c.get("region") or ""),
        str(c.get("dong") or ""),
        import_kb_table.normalize_complex_name(str(c.get("name") or "")),
    )


def merge_complex_lists(
    base_complexes: List[Dict[str, Any]],
    district_batches: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge existing KB Lead50/GyeonggiTop50 complexes with fetched 시/구 TOP50 batches safely."""
    by_key: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    ordered: List[Dict[str, Any]] = []

    def add_or_merge(incoming: Dict[str, Any]) -> None:
        in_copy = dict(incoming)
        in_copy["sources"] = list(in_copy.get("sources") or [])
        in_district = str(
            in_copy.get("district")
            or import_kb_table.derive_district(in_copy["region"], in_copy["location"], in_copy["dong"])
        )
        in_copy["district"] = in_district

        key = _dedup_key(in_copy)
        candidates = by_key.get(key, [])
        match: Optional[Dict[str, Any]] = None
        for cand in candidates:
            c_dist = str(cand.get("district") or "")
            if not c_dist or not in_district or c_dist == in_district:
                match = cand
                break

        if match is not None:
            for src in in_copy["sources"]:
                if src not in match["sources"]:
                    match["sources"].append(src)
            if in_copy.get("lead50Rank") is not None and match.get("lead50Rank") is None:
                match["lead50Rank"] = in_copy["lead50Rank"]
            if in_copy.get("gyeonggiTopRank") is not None and match.get("gyeonggiTopRank") is None:
                match["gyeonggiTopRank"] = in_copy["gyeonggiTopRank"]
            if in_copy.get("districtRank") is not None:
                match["districtRank"] = in_copy["districtRank"]
            if in_copy.get("kbComplexId") is not None:
                match["kbComplexId"] = in_copy["kbComplexId"]
            if in_district and (not match.get("district") or len(in_district) >= len(str(match.get("district")))):
                match["district"] = in_district
            # Prefer specific municipal location like '강남구 개포동' over '서울 개포동'
            existing_loc = str(match.get("location") or "")
            new_loc = str(in_copy.get("location") or "")
            if new_loc and (existing_loc.startswith("서울 ") or existing_loc.startswith("경기 ")):
                match["location"] = new_loc
            match["marketCapTrillion"] = in_copy["marketCapTrillion"]
            match["pricePerPyeongMan"] = in_copy["pricePerPyeongMan"]
            match["households"] = in_copy["households"]
            match["builtYM"] = in_copy["builtYM"]
            match["builtYear"] = in_copy["builtYear"]
            match["builtText"] = in_copy["builtText"]
            return

        # Prevent ID collision if two distinct districts in the same region share dong+name
        existing_ids = {item["id"] for item in ordered}
        if in_copy["id"] in existing_ids and in_district:
            reg_slug = "seoul" if in_copy["region"] == "서울" else "gyeonggi"
            dist_slug = in_district.replace(" ", "")
            in_copy["id"] = f"{reg_slug}-{dist_slug}-{in_copy['dong']}-{import_kb_table.normalize_complex_name(in_copy['name'])}"

        by_key.setdefault(key, []).append(in_copy)
        ordered.append(in_copy)

    for base in base_complexes:
        add_or_merge(base)

    for batch_key in sorted(district_batches.keys()):
        batch = district_batches[batch_key]
        region = batch["region"]
        district = batch["district"]
        for row in batch.get("rows") or []:
            parsed = parse_kb_api_row(row, region=region, district=district)
            add_or_merge(parsed)

    def sort_key(c: Dict[str, Any]) -> Tuple[int, int, int, int, str]:
        has_lead = 0 if c.get("lead50Rank") is not None else 1
        lead_r = c["lead50Rank"] if c.get("lead50Rank") is not None else 9999
        gg_r = c["gyeonggiTopRank"] if c.get("gyeonggiTopRank") is not None else 9999
        dist_r = c["districtRank"] if c.get("districtRank") is not None else 9999
        return (has_lead, lead_r, gg_r, dist_r, str(c.get("name") or ""))

    return sorted(ordered, key=sort_key)


def fetch_district_top50(region: str, district: str, code: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Fetch 50 ranking rows for a single 시/구 from KB Land API."""
    res = kb_get_json(
        "https://data-api.kbland.kr/bfmstat/kbleadapt50/areaIndxAndAptPrcIndxAndRankgList",
        {
            "TOP코드": "T20",
            "TOP코드명": "TOP20",
            "기간코드": "24",
            "기준조건코드": "A01",
            "법정동코드": code,
            "변동률여부": "0",
            "지역명": f"{region} {district}",
        },
    )
    top_info = ((res.get("dataBody") or {}).get("data") or {}).get("TOP아파트순위정보") or {}
    as_of = str((top_info.get("기준정보") or {}).get("기준년월") or "2026-10")
    rows = top_info.get("데이터리스트") or []
    return as_of, rows


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Fetch 시/구 TOP 50 apartments from KB Land API.")
    parser.add_argument(
        "--preset",
        choices=["core18", "all"],
        default="core18",
        help="Preset region list to fetch (default: core18).",
    )
    parser.add_argument(
        "--regions",
        default="",
        help="Comma-separated 시/구 override, e.g. '서울 양천구,경기 의왕시'.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Resolve codes and print target 시/구 without fetching rankings.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay in seconds between KB API calls.")
    args = parser.parse_args()

    catalog = fetch_sigungu_catalog()
    if args.regions.strip():
        requested = parse_regions_arg(args.regions)
    elif args.preset == "all":
        requested = [
            (meta["region"], meta["district"])
            for meta in catalog.values()
            if not meta["hasChildren"]
        ]
        requested.sort(key=lambda item: (0 if item[0] == "서울" else 1, item[1]))
    else:
        requested = list(CORE18_REGIONS)

    resolved: List[Dict[str, Any]] = []
    for reg, dist in requested:
        entry = catalog.get((reg, dist))
        if not entry:
            print(f"경고: '{reg} {dist}' 지역 코드를 KB 목록에서 찾지 못했습니다.", file=sys.stderr)
            continue
        resolved.append(entry)

    if args.dry_run:
        print(f"[dry-run] 대상 시/구 ({len(resolved)}개):")
        for item in resolved:
            print(f"  - {item['region']} {item['district']} (법정동코드={item['code']})")
        return 0

    app_path = project_root / "docs" / "data" / "apartments.json"
    raw_path = project_root / "data" / "kb_raw.json"
    regions_raw_path = project_root / "data" / "kb_regions_raw.json"
    cache_path = project_root / "data" / "kakao_transit_cache.json"

    # Always ensure existing verified Kakao results are safely persisted in cache first.
    existing_app_data: Optional[Dict[str, Any]] = None
    if app_path.exists():
        existing_app_data = json.loads(app_path.read_text(encoding="utf-8"))
        cache = enrich_kakao.load_transit_cache(cache_path, destination=(existing_app_data.get("meta") or {}).get("destination"))
        seeded = enrich_kakao.seed_cache_from_apartments(
            existing_app_data,
            cache,
            record_date_if_missing=enrich_kakao.now_kst_date(),
        )
        if seeded > 0 or not cache_path.exists():
            enrich_kakao.save_transit_cache(cache_path, cache)
    else:
        cache = enrich_kakao.load_transit_cache(cache_path)

    district_batches: Dict[str, Dict[str, Any]] = {}
    if regions_raw_path.exists():
        try:
            prev_regions = json.loads(regions_raw_path.read_text(encoding="utf-8"))
            district_batches = dict(prev_regions.get("districts") or {})
        except (OSError, json.JSONDecodeError):
            district_batches = {}

    as_of = "2026-10"
    print(f"[fetch_kb_regions] {len(resolved)}개 시/구 TOP50 조회 시작...")
    for idx, target in enumerate(resolved, start=1):
        reg = target["region"]
        dist = target["district"]
        code = target["code"]
        batch_as_of, rows = fetch_district_top50(reg, dist, code)
        as_of = batch_as_of or as_of
        key = f"{reg} {dist}"
        district_batches[key] = {
            "region": reg,
            "district": dist,
            "code": code,
            "asOf": as_of,
            "count": len(rows),
            "rows": rows,
        }
        print(f"  [{idx}/{len(resolved)}] {key} ({code}) -> {len(rows)}개")
        if idx < len(resolved) and args.sleep > 0:
            time.sleep(args.sleep)

    regions_raw_path.parent.mkdir(parents=True, exist_ok=True)
    regions_raw_path.write_text(
        json.dumps({"asOf": as_of, "districts": district_batches}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    base_complexes: List[Dict[str, Any]] = []
    if raw_path.exists():
        raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
        base_complexes = raw_data.get("complexes") or []

    merged = merge_complex_lists(base_complexes, district_batches)
    raw_path.write_text(
        json.dumps({"asOf": as_of, "datasetNote": f"merged_{len(merged)}", "complexes": merged}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    app_payload = import_kb_table.build_apartments_payload(
        merged,
        as_of=as_of,
        existing_data=existing_app_data,
        cache_data=cache,
    )
    app_path.parent.mkdir(parents=True, exist_ok=True)
    app_path.write_text(json.dumps(app_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    meta = app_payload["meta"]
    print(
        f"[완료] 총 고유 단지: {meta['totalCount']}개 "
        f"(서울 {meta['seoulCount']}, 경기 {meta['gyeonggiCount']}, 시/구 {len(meta['districts'])}개) | "
        f"기존 실측 유지: {meta['enrichedCount']}/{meta['totalCount']}개"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
