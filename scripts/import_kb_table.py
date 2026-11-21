#!/usr/bin/env python3
"""Import and merge KB Lead 50 and Gyeonggi Top 50 tables into app datasets."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_DESTINATION = {
    "name": "테헤란로 501",
    "address": "서울 강남구 테헤란로 501",
    "lat": 37.5073809,
    "lng": 127.0568213,
    "verified": False,
}

SEOUL_DONG_TO_GU = {
    "가락동": "송파구",
    "문정동": "송파구",
    "방이동": "송파구",
    "신천동": "송파구",
    "잠실동": "송파구",
    "개포동": "강남구",
    "대치동": "강남구",
    "도곡동": "강남구",
    "압구정동": "강남구",
    "일원동": "강남구",
    "고덕동": "강동구",
    "상일동": "강동구",
    "암사동": "강동구",
    "반포동": "서초구",
    "서초동": "서초구",
    "잠원동": "서초구",
    "목동": "양천구",
    "신정동": "양천구",
    "아현동": "마포구",
    "서빙고동": "용산구",
    "이촌동": "용산구",
    "여의도동": "영등포구",
}


def derive_district(region: str, location: str, dong: str, explicit_district: str = "") -> str:
    """Derive canonical 시/구 name such as '강남구' or '안양시 동안구'."""
    if explicit_district and explicit_district not in ("서울", "경기"):
        return explicit_district
    cleaned = " ".join(str(location or "").strip().split())
    tokens = cleaned.split(" ")
    if len(tokens) >= 2 and tokens[0] not in ("서울", "경기"):
        return " ".join(tokens[:-1])
    if len(tokens) >= 2 and tokens[0] in ("서울", "경기"):
        # e.g. '서울 강남구 개포동' or '강남구 개포동'
        mid = " ".join(tokens[1:-1])
        if mid:
            return mid
    if region == "서울" and dong in SEOUL_DONG_TO_GU:
        return SEOUL_DONG_TO_GU[dong]
    return ""


def parse_market_cap_trillion(text: Any) -> float:
    """Parse market cap string such as '23.46조' or '9,800억' into trillion KRW."""
    if isinstance(text, (int, float)):
        return round(float(text), 4)
    cleaned = str(text or "").strip().replace(",", "")
    if not cleaned:
        raise ValueError("Empty marketCap value")
    if "조" in cleaned:
        num = re.search(r"[\d.]+", cleaned)
        if not num:
            raise ValueError(f"Cannot parse marketCap: {text}")
        return round(float(num.group(0)), 4)
    if "억" in cleaned:
        num = re.search(r"[\d.]+", cleaned)
        if not num:
            raise ValueError(f"Cannot parse marketCap: {text}")
        return round(float(num.group(0)) / 10000.0, 4)
    return round(float(cleaned), 4)


def parse_price_per_pyeong_man(text: Any) -> int:
    """Parse price per 3.3m2 string such as '8,920만원' into integer man-won."""
    if isinstance(text, int):
        return text
    if isinstance(text, float):
        return int(round(text))
    cleaned = str(text or "").strip().replace(",", "")
    num = re.search(r"[\d]+", cleaned)
    if not num:
        raise ValueError(f"Cannot parse pricePerPyeong: {text}")
    return int(num.group(0))


def parse_households(text: Any) -> int:
    """Parse households string such as '8,109세대' into integer."""
    if isinstance(text, int):
        return text
    if isinstance(text, float):
        return int(round(text))
    cleaned = str(text or "").strip().replace(",", "")
    num = re.search(r"[\d]+", cleaned)
    if not num:
        raise ValueError(f"Cannot parse households: {text}")
    return int(num.group(0))


def parse_built_ym(text: Any) -> Tuple[str, int]:
    """Parse completion year/month such as '18년 12월 (9년차)' or '2018-12' into ('YYYY-MM', year)."""
    raw = str(text or "").strip()
    iso_match = re.match(r"^(\d{4})[-./](\d{1,2})", raw)
    if iso_match:
        year = int(iso_match.group(1))
        month = int(iso_match.group(2))
        return f"{year:04d}-{month:02d}", year

    kr_match = re.search(r"(\d{2,4})\s*년\s*(\d{1,2})\s*월", raw)
    if kr_match:
        yy = int(kr_match.group(1))
        month = int(kr_match.group(2))
        if yy < 100:
            year = 1900 + yy if yy >= 50 else 2000 + yy
        else:
            year = yy
        return f"{year:04d}-{month:02d}", year

    raise ValueError(f"Cannot parse builtYM: {text}")


def extract_region_and_dong(location: str, fallback_region: str = "경기") -> Tuple[str, str]:
    """Extract canonical region ('서울' or '경기') and trailing dong/eup/myeon token."""
    cleaned = " ".join(str(location or "").strip().split())
    if not cleaned:
        return fallback_region, ""
    tokens = cleaned.split(" ")
    first = tokens[0]
    if first.startswith("서울"):
        region = "서울"
    elif first.startswith("경기"):
        region = "경기"
    else:
        region = fallback_region or "경기"
    dong = tokens[-1]
    return region, dong


def normalize_complex_name(name: str) -> str:
    """Normalize complex name for deduplication key generation."""
    return re.sub(r"\s+", "", str(name or "").strip())


def make_complex_id(region: str, dong: str, name: str) -> str:
    """Create stable ID matching region, dong, and normalized complex name."""
    region_slug = "seoul" if region == "서울" else "gyeonggi"
    return f"{region_slug}-{dong}-{normalize_complex_name(name)}"


def normalize_row(row: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Normalize a single extracted row from KB Lead 50 or Gyeonggi Top 50."""
    fallback_region = "경기" if source == "gyeonggi_top50" else "서울"
    location = " ".join(str(row.get("location", "")).strip().split())
    region, dong = extract_region_and_dong(location, fallback_region=fallback_region)
    name = str(row.get("name", "")).strip()
    rank = int(row["rank"])
    built_raw = str(row.get("builtText") or row.get("builtYM") or "").strip()
    built_ym, built_year = parse_built_ym(built_raw)

    district = derive_district(region, location, dong, str(row.get("district") or ""))
    return {
        "id": make_complex_id(region, dong, name),
        "name": name,
        "region": region,
        "district": district,
        "location": location,
        "dong": dong,
        "sources": [source],
        "lead50Rank": rank if source == "kb_lead50" else None,
        "gyeonggiTopRank": rank if source == "gyeonggi_top50" else None,
        "districtRank": row.get("districtRank"),
        "kbComplexId": row.get("kbComplexId"),
        "marketCapTrillion": parse_market_cap_trillion(row["marketCap"]),
        "pricePerPyeongMan": parse_price_per_pyeong_man(row["pricePerPyeong"]),
        "households": parse_households(row["households"]),
        "builtYM": built_ym,
        "builtYear": built_year,
        "builtText": built_raw,
    }


def merge_and_deduplicate(
    lead50_rows: List[Dict[str, Any]],
    gyeonggi_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge KB Lead 50 and Gyeonggi Top 50 rows, deduplicating overlapping complexes."""
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    for raw in lead50_rows:
        item = normalize_row(raw, "kb_lead50")
        key = (item["region"], item["dong"], normalize_complex_name(item["name"]))
        merged[key] = item

    for raw in gyeonggi_rows:
        item = normalize_row(raw, "gyeonggi_top50")
        key = (item["region"], item["dong"], normalize_complex_name(item["name"]))
        if key in merged:
            existing = merged[key]
            # Prefer specific municipal location like '과천시 원문동' over '경기 원문동'.
            if existing["location"].startswith("경기 ") and not item["location"].startswith("경기 "):
                existing["location"] = item["location"]
            if not existing.get("district") and item.get("district"):
                existing["district"] = item["district"]
            if "gyeonggi_top50" not in existing["sources"]:
                existing["sources"].append("gyeonggi_top50")
            existing["gyeonggiTopRank"] = item["gyeonggiTopRank"]
            existing["marketCapTrillion"] = item["marketCapTrillion"]
            existing["pricePerPyeongMan"] = item["pricePerPyeongMan"]
            existing["households"] = item["households"]
            existing["builtYM"] = item["builtYM"]
            existing["builtYear"] = item["builtYear"]
            existing["builtText"] = item["builtText"]
        else:
            merged[key] = item

    def sort_key(c: Dict[str, Any]) -> Tuple[int, int, int]:
        has_lead = 0 if c["lead50Rank"] is not None else 1
        lead_rank = c["lead50Rank"] if c["lead50Rank"] is not None else 9999
        gg_rank = c["gyeonggiTopRank"] if c["gyeonggiTopRank"] is not None else 9999
        return (has_lead, lead_rank, gg_rank)

    return sorted(merged.values(), key=sort_key)


def parse_snapshot_text(text: str) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Parse pipe-delimited snapshot text containing GYEONGGI_TOP_50 and KB_LEAD_50 sections."""
    as_of = "2026-10"
    gyeonggi_rows: List[Dict[str, Any]] = []
    lead50_rows: List[Dict[str, Any]] = []
    section: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# asOf:"):
            as_of_raw = line.split(":", 1)[1].strip()
            m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월", as_of_raw)
            if m:
                as_of = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
            else:
                as_of = as_of_raw
            continue
        if line == "### GYEONGGI_TOP_50":
            section = "gyeonggi"
            continue
        if line == "### KB_LEAD_50":
            section = "lead50"
            continue
        if line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 7:
            raise ValueError(f"Expected 7 pipe-separated columns, got {len(parts)}: {line}")
        row = {
            "rank": int(parts[0]),
            "name": parts[1],
            "location": parts[2],
            "marketCap": parts[3],
            "pricePerPyeong": parts[4],
            "households": parts[5],
            "builtYM": parts[6],
        }
        if section == "gyeonggi":
            gyeonggi_rows.append(row)
        elif section == "lead50":
            lead50_rows.append(row)
        else:
            raise ValueError(f"Row found outside section header: {line}")

    return as_of, lead50_rows, gyeonggi_rows


def empty_commute() -> Dict[str, Any]:
    return {
        "minutes": None,
        "transfers": None,
        "routeType": None,
        "distanceMeters": None,
        "fareWon": None,
        "summary": None,
        "landingUrl": None,
        "verified": False,
    }


def _summarize_districts(apartments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[Tuple[str, str], int] = {}
    for apt in apartments:
        d = str(apt.get("district") or "").strip()
        if not d:
            continue
        key = (str(apt.get("region") or ""), d)
        counts[key] = counts.get(key, 0) + 1
    return [
        {"region": reg, "district": dist, "count": cnt}
        for (reg, dist), cnt in sorted(counts.items(), key=lambda item: (0 if item[0][0] == "서울" else 1, item[0][1]))
    ]


def build_apartments_payload(
    complexes: List[Dict[str, Any]],
    as_of: str,
    existing_data: Optional[Dict[str, Any]] = None,
    cache_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build public/data/apartments.json while preserving existing & cached verified coords and commute."""
    existing_by_id: Dict[str, Dict[str, Any]] = {}
    existing_meta = (existing_data or {}).get("meta") or {}
    for item in (existing_data or {}).get("apartments") or []:
        if isinstance(item, dict) and item.get("id"):
            existing_by_id[str(item["id"])] = item

    coords_cache = (cache_data or {}).get("coordsByComplexKey") or {}
    transit_id_cache = (cache_data or {}).get("transitByComplexKey") or {}
    transit_coord_cache = (cache_data or {}).get("transitByCoordKey") or {}

    destination = existing_meta.get("destination") or (cache_data or {}).get("destination") or dict(DEFAULT_DESTINATION)
    dest_lat = destination.get("lat")
    dest_lng = destination.get("lng")

    apartments: List[Dict[str, Any]] = []
    coords_count = 0
    enriched_count = 0

    for c in complexes:
        prev = existing_by_id.get(c["id"], {})
        if prev.get("coordsVerified") and prev.get("lat") is not None and prev.get("lng") is not None:
            lat = prev.get("lat")
            lng = prev.get("lng")
            coords_verified = True
        elif c["id"] in coords_cache and coords_cache[c["id"]].get("lat") is not None:
            lat = coords_cache[c["id"]]["lat"]
            lng = coords_cache[c["id"]]["lng"]
            coords_verified = True
        else:
            lat = None
            lng = None
            coords_verified = False

        prev_commute = prev.get("commute") if isinstance(prev.get("commute"), dict) else {}
        cached_commute = transit_id_cache.get(c["id"])
        if not cached_commute and lat is not None and lng is not None and dest_lat is not None and dest_lng is not None:
            ckey = f"{float(lat):.5f},{float(lng):.5f}->{float(dest_lat):.5f},{float(dest_lng):.5f}"
            cached_commute = transit_coord_cache.get(ckey)

        if prev_commute.get("verified") and prev_commute.get("minutes") is not None:
            commute = dict(prev_commute)
        elif isinstance(cached_commute, dict) and cached_commute.get("verified") and cached_commute.get("minutes") is not None:
            commute = dict(cached_commute)
        else:
            commute = empty_commute()

        if coords_verified:
            coords_count += 1
        if coords_verified and commute.get("verified"):
            enriched_count += 1

        straight_distance_km = prev.get("straightDistanceKm")
        if straight_distance_km is None and lat is not None and lng is not None and dest_lat is not None and dest_lng is not None:
            import math
            p1, p2 = math.radians(float(lat)), math.radians(float(dest_lat))
            dp, dl = math.radians(float(dest_lat) - float(lat)), math.radians(float(dest_lng) - float(lng))
            a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
            straight_distance_km = round(6371.0 * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a)), 2)

        units = prev.get("units") if isinstance(prev.get("units"), list) else []
        district = str(c.get("district") or prev.get("district") or derive_district(c["region"], c["location"], c["dong"]))

        apartments.append(
            {
                "id": c["id"],
                "name": c["name"],
                "region": c["region"],
                "district": district,
                "location": c["location"],
                "dong": c["dong"],
                "sources": list(c["sources"]),
                "lead50Rank": c.get("lead50Rank"),
                "gyeonggiTopRank": c.get("gyeonggiTopRank"),
                "districtRank": c.get("districtRank") if c.get("districtRank") is not None else prev.get("districtRank"),
                "kbComplexId": c.get("kbComplexId") if c.get("kbComplexId") is not None else prev.get("kbComplexId"),
                "marketCapTrillion": c["marketCapTrillion"],
                "pricePerPyeongMan": c["pricePerPyeongMan"],
                "households": c["households"],
                "builtYM": c["builtYM"],
                "builtYear": c["builtYear"],
                "builtText": c["builtText"],
                "lat": lat,
                "lng": lng,
                "coordsVerified": coords_verified,
                "straightDistanceKm": straight_distance_km,
                "commute": commute,
                "units": units,
            }
        )

    total_count = len(apartments)
    seoul_count = sum(1 for a in apartments if a["region"] == "서울")
    gyeonggi_count = sum(1 for a in apartments if a["region"] == "경기")
    overlap_count = sum(1 for a in apartments if len(a["sources"]) > 1)
    districts = _summarize_districts(apartments)

    enriched_at = existing_meta.get("enrichedAt")
    if any("district_top50" in a["sources"] for a in apartments):
        dataset_status = f"districts_{len(districts)}_{total_count}"
    elif total_count >= 90:
        dataset_status = f"full_{total_count}"
    else:
        dataset_status = f"partial_{total_count}"

    return {
        "meta": {
            "asOf": as_of,
            "datasetStatus": dataset_status,
            "totalCount": total_count,
            "seoulCount": seoul_count,
            "gyeonggiCount": gyeonggi_count,
            "overlapCount": overlap_count,
            "coordsCount": coords_count,
            "enrichedCount": enriched_count,
            "districts": districts,
            "destination": destination,
            "enrichedAt": enriched_at,
        },
        "apartments": apartments,
    }


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Import KB Lead 50 and Gyeonggi Top 50 data.")
    parser.add_argument("--snapshot", type=Path, help="Pipe-delimited snapshot text file.")
    parser.add_argument("--lead50", type=Path, help="JSON array file exported for KB선도아파트50.")
    parser.add_argument("--gyeonggi", type=Path, help="JSON array file exported for 경기 시세총액 TOP.")
    parser.add_argument("--input", type=Path, help="Combined JSON file with kbLead50 and gyeonggiTop50.")
    parser.add_argument("--as-of", default="2026-10", help="Reference month (YYYY-MM).")
    parser.add_argument(
        "--raw-out",
        type=Path,
        default=project_root / "data" / "kb_raw.json",
        help="Output path for normalized raw KB JSON.",
    )
    parser.add_argument(
        "--app-out",
        type=Path,
        default=project_root / "docs" / "data" / "apartments.json",
        help="Output path for frontend apartments.json.",
    )
    args = parser.parse_args()

    as_of = args.as_of
    lead50_rows: List[Dict[str, Any]] = []
    gyeonggi_rows: List[Dict[str, Any]] = []

    if args.snapshot:
        text = args.snapshot.read_text(encoding="utf-8")
        as_of, lead50_rows, gyeonggi_rows = parse_snapshot_text(text)
    elif args.input:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        as_of = str(payload.get("asOf") or as_of)
        lead50_rows = payload.get("kbLead50") or []
        gyeonggi_rows = payload.get("gyeonggiTop50") or []
    elif args.lead50 or args.gyeonggi:
        if args.lead50:
            lead_payload = json.loads(args.lead50.read_text(encoding="utf-8"))
            lead50_rows = lead_payload if isinstance(lead_payload, list) else lead_payload.get("rows", [])
        if args.gyeonggi:
            gg_payload = json.loads(args.gyeonggi.read_text(encoding="utf-8"))
            gyeonggi_rows = gg_payload if isinstance(gg_payload, list) else gg_payload.get("rows", [])
    else:
        parser.error("Provide --snapshot, --input, or --lead50/--gyeonggi.")

    merged = merge_and_deduplicate(lead50_rows, gyeonggi_rows)

    args.raw_out.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = {
        "asOf": as_of,
        "datasetNote": f"merged_{len(merged)}",
        "complexes": merged,
    }
    args.raw_out.write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    existing_app_data: Optional[Dict[str, Any]] = None
    if args.app_out.exists():
        try:
            existing_app_data = json.loads(args.app_out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_app_data = None

    cache_path = project_root / "data" / "kakao_transit_cache.json"
    cache_data: Optional[Dict[str, Any]] = None
    if cache_path.exists():
        try:
            cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache_data = None

    app_payload = build_apartments_payload(
        merged,
        as_of=as_of,
        existing_data=existing_app_data,
        cache_data=cache_data,
    )
    args.app_out.parent.mkdir(parents=True, exist_ok=True)
    args.app_out.write_text(json.dumps(app_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    meta = app_payload["meta"]
    print(
        f"[import_kb_table] asOf={meta['asOf']} total={meta['totalCount']} "
        f"(서울 {meta['seoulCount']}, 경기 {meta['gyeonggiCount']}, 중복병합 {meta['overlapCount']}, "
        f"실측유지 {meta['enrichedCount']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
