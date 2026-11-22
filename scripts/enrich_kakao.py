#!/usr/bin/env python3
"""Enrich apartments.json with Kakao Local coords & once-only cached public transit to Teheran-ro 501."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


KST = timezone(timedelta(hours=9))

NON_RESIDENTIAL_KEYWORDS = (
    "부동산",
    "공인중개사",
    "상가",
    "주차장",
    "어린이집",
    "관리사무소",
    "경비실",
    "편의점",
    "카페",
    "병원",
    "약국",
    "학원",
    "은행",
    "미용실",
    "세탁소",
)


def now_kst_date() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_dotenv(env_path: Path) -> Dict[str, str]:
    """Parse simple KEY=VALUE lines from .env without overwriting existing OS env vars."""
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        values[key] = val
    return values


def get_rest_api_key(project_root: Path) -> str:
    """Resolve KAKAO_REST_API_KEY from OS env first, then project .env."""
    env_file = load_dotenv(project_root / ".env")
    return (os.environ.get("KAKAO_REST_API_KEY") or env_file.get("KAKAO_REST_API_KEY") or "").strip()


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Compute great-circle distance in kilometers between two WGS84 coordinates."""
    r_km = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(r_km * c, 2)


def make_coord_key(lat: float, lng: float, dest_lat: float, dest_lng: float) -> str:
    """Create rounded coordinate pair key (~1m precision) for transit cache lookup."""
    return f"{float(lat):.5f},{float(lng):.5f}->{float(dest_lat):.5f},{float(dest_lng):.5f}"


def empty_cache(destination: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "destination": dict(
            destination
            or {
                "name": "테헤란로 501",
                "address": "서울 강남구 테헤란로 501",
                "lat": 37.5073809,
                "lng": 127.0568213,
                "verified": False,
            }
        ),
        "dailyTransitCalls": {},
        "coordsByComplexKey": {},
        "transitByComplexKey": {},
        "transitByCoordKey": {},
    }


def load_transit_cache(cache_path: Path, destination: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load persistent Kakao coords & transit cache from disk."""
    if not cache_path.exists():
        return empty_cache(destination)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty_cache(destination)
        data.setdefault("destination", empty_cache(destination)["destination"])
        data.setdefault("dailyTransitCalls", {})
        data.setdefault("coordsByComplexKey", {})
        data.setdefault("transitByComplexKey", {})
        data.setdefault("transitByCoordKey", {})
        return data
    except (OSError, json.JSONDecodeError):
        return empty_cache(destination)


def save_transit_cache(cache_path: Path, cache: Dict[str, Any]) -> None:
    """Persist Kakao coords & transit cache atomically."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(cache_path)


def seed_cache_from_apartments(
    apartments_payload: Dict[str, Any],
    cache: Dict[str, Any],
    record_date_if_missing: Optional[str] = None,
) -> int:
    """Populate cache from verified entries already stored in apartments.json. Returns count added."""
    meta_dest = (apartments_payload.get("meta") or {}).get("destination")
    if isinstance(meta_dest, dict) and meta_dest.get("verified") and meta_dest.get("lat") is not None:
        cache["destination"] = dict(meta_dest)

    dest = cache["destination"]
    dest_lat = float(dest["lat"])
    dest_lng = float(dest["lng"])

    coords_map = cache.setdefault("coordsByComplexKey", {})
    transit_by_id = cache.setdefault("transitByComplexKey", {})
    transit_by_coord = cache.setdefault("transitByCoordKey", {})

    newly_seeded_transit = 0
    for apt in apartments_payload.get("apartments") or []:
        cid = str(apt.get("id") or "")
        if not cid:
            continue
        if apt.get("coordsVerified") and apt.get("lat") is not None and apt.get("lng") is not None:
            if cid not in coords_map:
                coords_map[cid] = {
                    "lat": float(apt["lat"]),
                    "lng": float(apt["lng"]),
                    "savedAt": now_utc_iso(),
                }
        commute = apt.get("commute")
        if isinstance(commute, dict) and commute.get("verified") and commute.get("minutes") is not None:
            if cid not in transit_by_id:
                transit_by_id[cid] = dict(commute)
                newly_seeded_transit += 1
            if apt.get("lat") is not None and apt.get("lng") is not None:
                ckey = make_coord_key(float(apt["lat"]), float(apt["lng"]), dest_lat, dest_lng)
                if ckey not in transit_by_coord:
                    transit_by_coord[ckey] = dict(commute)

    if record_date_if_missing and newly_seeded_transit > 0:
        daily = cache.setdefault("dailyTransitCalls", {})
        if record_date_if_missing not in daily:
            daily[record_date_if_missing] = newly_seeded_transit

    return newly_seeded_transit


def hydrate_complex_from_cache(
    apt: Dict[str, Any],
    cache: Dict[str, Any],
) -> Tuple[bool, bool]:
    """Hydrate apt coordinates and commute from cache without API calls. Returns (coord_hit, transit_hit)."""
    cid = str(apt.get("id") or "")
    dest = cache.get("destination") or {}
    dest_lat = dest.get("lat")
    dest_lng = dest.get("lng")

    coord_hit = False
    transit_hit = False

    if not apt.get("coordsVerified") or apt.get("lat") is None or apt.get("lng") is None:
        cached_coord = (cache.get("coordsByComplexKey") or {}).get(cid)
        if cached_coord and cached_coord.get("lat") is not None and cached_coord.get("lng") is not None:
            apt["lat"] = float(cached_coord["lat"])
            apt["lng"] = float(cached_coord["lng"])
            apt["coordsVerified"] = True
            coord_hit = True

    if apt.get("lat") is not None and apt.get("lng") is not None and dest_lat is not None and dest_lng is not None:
        apt["straightDistanceKm"] = haversine_km(float(apt["lat"]), float(apt["lng"]), float(dest_lat), float(dest_lng))

    commute = apt.get("commute")
    already_verified = isinstance(commute, dict) and commute.get("verified") and commute.get("minutes") is not None
    if not already_verified:
        cached_transit = (cache.get("transitByComplexKey") or {}).get(cid)
        if not cached_transit and apt.get("lat") is not None and apt.get("lng") is not None and dest_lat is not None and dest_lng is not None:
            ckey = make_coord_key(float(apt["lat"]), float(apt["lng"]), float(dest_lat), float(dest_lng))
            cached_transit = (cache.get("transitByCoordKey") or {}).get(ckey)
        if isinstance(cached_transit, dict) and cached_transit.get("verified") and cached_transit.get("minutes") is not None:
            apt["commute"] = dict(cached_transit)
            transit_hit = True

    return coord_hit, transit_hit


def normalize_name_variants(name: str) -> Dict[str, str]:
    """Produce normalized complex name variants for Kakao place search."""
    raw = " ".join(str(name or "").strip().split())
    name_space_parens = " ".join(re.sub(r"[(),]", " ", raw).split())
    name_join_parens = " ".join(raw.replace("(", "").replace(")", "").replace(",", " ").split())
    name_base = raw.split("(", 1)[0].strip()
    return {
        "raw": raw,
        "space": name_space_parens,
        "join": name_join_parens,
        "base": name_base,
    }


def build_geocode_queries(complex_item: Dict[str, Any]) -> List[str]:
    """Build ordered fallback keyword queries for Kakao Local keyword search."""
    location = str(complex_item.get("location") or "").strip()
    region = str(complex_item.get("region") or "").strip()
    dong = str(complex_item.get("dong") or "").strip()
    district = str(complex_item.get("district") or "").strip()
    variants = normalize_name_variants(str(complex_item.get("name") or ""))

    loc_prefix = f"{region} {district}".strip() if district and district not in location else location
    candidates = [
        f"{location} {variants['join']} 아파트",
        f"{location} {variants['space']} 아파트",
        f"{loc_prefix} {dong} {variants['join']} 아파트",
        f"{region} {dong} {variants['join']} 아파트",
        f"{location} {variants['base']} 아파트",
        f"{region} {dong} {variants['base']} 아파트",
        f"{location} {variants['raw']}",
    ]

    deduped: List[str] = []
    seen = set()
    for q in candidates:
        normalized_q = " ".join(q.split())
        if normalized_q and normalized_q not in seen:
            seen.add(normalized_q)
            deduped.append(normalized_q)
    return deduped


def score_kakao_place(doc: Dict[str, Any], region: str, dong: str, name: str) -> int:
    """Score a Kakao Local place document to prefer matching residential apartment complexes."""
    score = 0
    address_name = str(doc.get("address_name") or "")
    road_address_name = str(doc.get("road_address_name") or "")
    place_name = str(doc.get("place_name") or "")
    category_name = str(doc.get("category_name") or "")

    combined_addr = f"{address_name} {road_address_name}"
    if dong and dong in combined_addr:
        score += 50
    if region and region in address_name:
        score += 30
    if "아파트" in category_name or "아파트" in place_name:
        score += 25

    variants = normalize_name_variants(name)
    base_compact = re.sub(r"\s+", "", variants["base"])
    place_compact = re.sub(r"\s+", "", place_name)
    if base_compact and base_compact in place_compact:
        score += 15

    combined_label = f"{category_name} {place_name}"
    if any(kw in combined_label for kw in NON_RESIDENTIAL_KEYWORDS):
        score -= 45

    return score


def _extract_step_label(step: Dict[str, Any]) -> str:
    s_props = step.get("properties") or {}
    s_type = str(s_props.get("type") or "").upper()
    vehicles = step.get("vehicles") or []
    stops = step.get("stops") or []

    v_name = ""
    if vehicles and isinstance(vehicles[0], dict):
        v_name = str(vehicles[0].get("name") or vehicles[0].get("lineName") or vehicles[0].get("type") or "").strip()
    if not v_name:
        v_name = str(s_props.get("lineName") or s_props.get("routeName") or s_type).strip()

    stop_names = [
        str(st.get("name") or st.get("stationName") or "").strip()
        for st in stops
        if isinstance(st, dict) and str(st.get("name") or st.get("stationName") or "").strip()
    ]
    if not stop_names:
        start_st = str(s_props.get("startStation") or s_props.get("departureName") or "").strip()
        end_st = str(s_props.get("endStation") or s_props.get("arrivalName") or "").strip()
        if start_st and end_st:
            stop_names = [start_st, end_st]

    if v_name and len(stop_names) >= 2:
        return f"{v_name}({stop_names[0]}→{stop_names[-1]})"
    if v_name and len(stop_names) == 1:
        return f"{v_name}({stop_names[0]})"
    return v_name


def parse_publictraffic_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse Kakao /v2/routing/publictraffic response into a compact commute dict."""
    routes = payload.get("routes") or []
    if not isinstance(routes, list) or not routes:
        raise ValueError("대중교통 경로(routes)를 찾지 못했습니다.")

    def route_time(route: Dict[str, Any]) -> int:
        props = route.get("properties") or {}
        t = props.get("totalTime")
        return int(t) if isinstance(t, (int, float)) else 10**9

    best_route = min(routes, key=route_time)
    props = best_route.get("properties") or {}
    total_time_sec = props.get("totalTime")
    if total_time_sec is None:
        raise ValueError("경로 소요시간(totalTime)이 응답에 없습니다.")

    minutes = max(1, int(round(float(total_time_sec) / 60.0)))
    transfers = props.get("transfers")
    route_type = props.get("type")
    distance_meters = props.get("totalDistance")
    fare_obj = props.get("fare") if isinstance(props.get("fare"), dict) else {}
    fare_won = fare_obj.get("value")
    landing_url = (payload.get("properties") or {}).get("landingURL")

    segments: List[str] = []
    guidance_fallbacks: List[str] = []
    for step in best_route.get("steps") or []:
        if not isinstance(step, dict):
            continue
        s_props = step.get("properties") or {}
        s_type = str(s_props.get("type") or "").upper()
        guidance = str(s_props.get("guidance") or "").strip()
        if s_type == "WALKING":
            continue
        if guidance:
            guidance_fallbacks.append(guidance)

        label = _extract_step_label(step)
        if label:
            segments.append(label)

    if segments:
        summary = " → ".join(segments)
    elif guidance_fallbacks:
        summary = " / ".join(guidance_fallbacks[:3])
    else:
        summary = f"대중교통 {minutes}분"

    return {
        "minutes": minutes,
        "transfers": int(transfers) if transfers is not None else None,
        "routeType": str(route_type) if route_type is not None else None,
        "distanceMeters": int(distance_meters) if distance_meters is not None else None,
        "fareWon": int(fare_won) if fare_won is not None else None,
        "summary": summary,
        "landingUrl": str(landing_url) if landing_url else None,
        "verified": True,
    }


def kakao_get_json(url: str, params: Dict[str, Any], rest_api_key: str, timeout: int = 20) -> Dict[str, Any]:
    """Execute GET request against Kakao REST API with retries on transient socket/TLS timeouts."""
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    req = urllib.request.Request(
        full_url,
        headers={
            "Authorization": f"KakaoAK {rest_api_key}",
            "Accept": "application/json",
        },
        method="GET",
    )
    last_err: Optional[Exception] = None
    backoffs = (2, 4, 8, 16)
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            if attempt <= len(backoffs):
                time.sleep(backoffs[attempt - 1])
    assert last_err is not None
    raise last_err


def geocode_destination(address: str, rest_api_key: str) -> Tuple[float, float]:
    """Geocode destination address via Kakao address search, falling back to keyword search."""
    addr_res = kakao_get_json(
        "https://dapi.kakao.com/v2/local/search/address.json",
        {"query": address, "size": 5},
        rest_api_key,
    )
    docs = addr_res.get("documents") or []
    if docs:
        return round(float(docs[0]["y"]), 7), round(float(docs[0]["x"]), 7)

    kw_res = kakao_get_json(
        "https://dapi.kakao.com/v2/local/search/keyword.json",
        {"query": address, "size": 5},
        rest_api_key,
    )
    kw_docs = kw_res.get("documents") or []
    if kw_docs:
        return round(float(kw_docs[0]["y"]), 7), round(float(kw_docs[0]["x"]), 7)
    raise RuntimeError(f"목적지 좌표를 찾지 못했습니다: {address}")


def geocode_complex(complex_item: Dict[str, Any], rest_api_key: str, sleep_sec: float) -> Tuple[Optional[float], Optional[float], str]:
    """Find best coordinates for an apartment complex using fallback keyword queries."""
    queries = build_geocode_queries(complex_item)
    region = str(complex_item.get("region") or "")
    dong = str(complex_item.get("dong") or "")
    name = str(complex_item.get("name") or "")

    best_doc: Optional[Dict[str, Any]] = None
    best_score = -9999
    best_query = ""

    for q in queries:
        res = kakao_get_json(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            {"query": q, "size": 5},
            rest_api_key,
        )
        time.sleep(sleep_sec)
        for doc in res.get("documents") or []:
            sc = score_kakao_place(doc, region=region, dong=dong, name=name)
            if sc > best_score:
                best_score = sc
                best_doc = doc
                best_query = q
        if best_score >= 70:
            break

    if not best_doc:
        return None, None, "검색 결과 없음"

    lat = round(float(best_doc["y"]), 7)
    lng = round(float(best_doc["x"]), 7)
    place_label = f"{best_doc.get('place_name', '')} ({best_doc.get('address_name', '')})"
    if best_score < 30:
        print(
            f"  [경고] {name}: 낮은 매칭 점수({best_score}, query='{best_query}') -> {place_label}",
            file=sys.stderr,
        )
    return lat, lng, place_label


def format_http_error(err: urllib.error.HTTPError) -> str:
    """Return helpful Korean guidance on HTTP errors from Kakao APIs."""
    body = ""
    try:
        body = err.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    if err.code in (401, 403):
        return (
            f"HTTP {err.code} 인증/권한 오류입니다. 아래 항목을 확인해 주세요:\n"
            f"1) .env의 KAKAO_REST_API_KEY가 정확한지 확인\n"
            f"2) Kakao Developers > [앱 설정] > [제품 설정] > [카카오맵] > 사용 설정(ON) 확인\n"
            f"3) 대중교통 API 권한 및 무료 쿼터(첫 번째 활성 앱 일 1,000건) 확인\n"
            f"응답 본문: {body}"
        )
    if err.code == 429:
        return f"HTTP 429 쿼터 초과(일 1,000건 한도 도달)입니다. 캐시가 저장되었으니 내일 이어서 실행하세요.\n응답 본문: {body}"
    return f"HTTP {err.code} 오류: {err.reason} | {body}"


def save_apartments(app_path: Path, payload: Dict[str, Any]) -> None:
    """Persist apartments.json and update enrichedCount metadata."""
    apartments = payload.get("apartments") or []
    meta = payload.setdefault("meta", {})
    meta["coordsCount"] = sum(1 for apt in apartments if apt.get("coordsVerified"))
    meta["enrichedCount"] = sum(
        1
        for apt in apartments
        if apt.get("coordsVerified") and (apt.get("commute") or {}).get("verified")
    )
    meta["enrichedAt"] = now_utc_iso()
    tmp_path = app_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(app_path)


def run_transit_enrichment(
    apartments: List[Dict[str, Any]],
    dest: Dict[str, Any],
    cache: Dict[str, Any],
    rest_key: str,
    max_distance_km: float = 30.0,
    max_transit: int = 850,
    daily_transit_cap: int = 950,
    force_refetch_transit: bool = False,
    sleep_sec: float = 0.15,
    fetcher: Callable[[str, Dict[str, Any], str], Dict[str, Any]] = kakao_get_json,
    on_batch_save: Optional[Callable[[], None]] = None,
) -> Dict[str, int]:
    """Run strictly once-only transit enrichment backed by cache and daily quota caps."""
    dest_lat = float(dest["lat"])
    dest_lng = float(dest["lng"])
    dest_name = str(dest.get("name") or "테헤란로 501")
    today = now_kst_date()
    daily_calls = cache.setdefault("dailyTransitCalls", {})
    used_today = int(daily_calls.get(today, 0))

    stats = {
        "already_verified": 0,
        "cache_hits": 0,
        "api_calls": 0,
        "skipped_distance": 0,
        "skipped_no_coords": 0,
        "capped": 0,
        "errors": 0,
        "used_today": used_today,
    }

    api_queue: List[Tuple[float, Dict[str, Any]]] = []
    for apt in apartments:
        if not apt.get("coordsVerified") or apt.get("lat") is None or apt.get("lng") is None:
            stats["skipped_no_coords"] += 1
            continue

        dist_km = haversine_km(float(apt["lat"]), float(apt["lng"]), dest_lat, dest_lng)
        apt["straightDistanceKm"] = dist_km

        if not force_refetch_transit:
            _, transit_hit = hydrate_complex_from_cache(apt, cache)
            if transit_hit:
                stats["cache_hits"] += 1
                continue
            commute = apt.get("commute")
            if isinstance(commute, dict) and commute.get("verified") and commute.get("minutes") is not None:
                seed_cache_from_apartments({"meta": {"destination": dest}, "apartments": [apt]}, cache)
                stats["already_verified"] += 1
                continue

        if dist_km > max_distance_km:
            commute = apt.setdefault("commute", {})
            if isinstance(commute, dict) and not commute.get("verified"):
                commute["skippedReason"] = f"over_{int(max_distance_km)}km"
            stats["skipped_distance"] += 1
            continue

        api_queue.append((dist_km, apt))

    api_queue.sort(key=lambda pair: (pair[0], str(pair[1].get("name") or "")))

    transit_by_id = cache.setdefault("transitByComplexKey", {})
    transit_by_coord = cache.setdefault("transitByCoordKey", {})
    consecutive_net_errors = 0

    for idx, (dist_km, apt) in enumerate(api_queue, start=1):
        if stats["api_calls"] >= max_transit or used_today >= daily_transit_cap:
            stats["capped"] = len(api_queue) - idx + 1
            break

        ckey = make_coord_key(float(apt["lat"]), float(apt["lng"]), dest_lat, dest_lng)
        if not force_refetch_transit and (apt["id"] in transit_by_id or ckey in transit_by_coord):
            cached = transit_by_id.get(apt["id"]) or transit_by_coord.get(ckey)
            if isinstance(cached, dict) and cached.get("verified"):
                apt["commute"] = dict(cached)
                transit_by_id[apt["id"]] = dict(cached)
                transit_by_coord[ckey] = dict(cached)
                stats["cache_hits"] += 1
                continue

        try:
            transit_res = fetcher(
                "https://dapi.kakao.com/v2/routing/publictraffic",
                {
                    "start_x": str(apt["lng"]),
                    "start_y": str(apt["lat"]),
                    "end_x": str(dest_lng),
                    "end_y": str(dest_lat),
                    "s_name": str(apt["name"]),
                    "e_name": dest_name,
                },
                rest_key,
            )
            consecutive_net_errors = 0
            stats["api_calls"] += 1
            used_today += 1
            daily_calls[today] = used_today
            parsed = parse_publictraffic_response(transit_res)
            apt["commute"] = parsed
            transit_by_id[apt["id"]] = dict(parsed)
            transit_by_coord[ckey] = dict(parsed)
            print(
                f"  [통근 {stats['api_calls']}/{min(len(api_queue), max_transit)}] "
                f"{apt['name']} ({apt['location']}, {dist_km}km) -> "
                f"{parsed['minutes']}분, 환승 {parsed['transfers']}회 | {parsed['summary']}"
            )
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as net_err:
            stats["errors"] += 1
            consecutive_net_errors += 1
            print(f"  [네트워크 경고] {apt['name']}: 일시적 통신 오류로 건너뜀 ({net_err})", file=sys.stderr)
            if consecutive_net_errors >= 5:
                raise RuntimeError(f"연속 네트워크 오류 5회로 대중교통 수집을 안전 중단합니다: {net_err}") from net_err
        except ValueError as ve:
            consecutive_net_errors = 0
            stats["errors"] += 1
            print(f"  [오류] {apt['name']}: 대중교통 경로 파싱 실패 ({ve})", file=sys.stderr)

        if sleep_sec > 0:
            time.sleep(sleep_sec)
        if on_batch_save and stats["api_calls"] % 5 == 0:
            on_batch_save()

    stats["used_today"] = used_today
    return stats


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Enrich apartments.json using Kakao Local + cached Public Transit API.")
    parser.add_argument(
        "--app-json",
        type=Path,
        default=project_root / "docs" / "data" / "apartments.json",
        help="Path to docs/data/apartments.json",
    )
    parser.add_argument(
        "--cache-json",
        type=Path,
        default=project_root / "data" / "kakao_transit_cache.json",
        help="Path to persistent once-only Kakao cache JSON.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print plan without calling APIs.")
    parser.add_argument("--coords-only", action="store_true", help="Run Phase 1 geocoding only (0 transit calls).")
    parser.add_argument("--force", action="store_true", help="Re-fetch coordinates (does NOT re-fetch transit unless --force-refetch-transit).")
    parser.add_argument("--force-refetch-transit", action="store_true", help="Explicitly override once-only transit cache.")
    parser.add_argument("--max-distance-km", type=float, default=30.0, help="Skip transit calls beyond straight-line km (default: 30.0).")
    parser.add_argument("--max-transit", type=int, default=850, help="Max new transit API calls in this run (default: 850).")
    parser.add_argument("--daily-transit-cap", type=int, default=950, help="Hard daily cap across runs (default: 950 < 1,000 free quota).")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N uncached complexes per phase (0 = all).")
    parser.add_argument("--region", choices=["", "서울", "경기"], default="", help="Restrict enrichment to '서울' or '경기'.")
    parser.add_argument("--coord-sleep", type=float, default=0.08, help="Delay between Kakao Local calls (default: 0.08s).")
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between Kakao Transit calls (default: 0.15s).")
    args = parser.parse_args()

    if not args.app_json.exists():
        print(f"오류: {args.app_json} 파일이 없습니다. 먼저 KB 데이터를 수집하세요.", file=sys.stderr)
        return 1

    payload = json.loads(args.app_json.read_text(encoding="utf-8"))
    apartments: List[Dict[str, Any]] = payload.get("apartments") or []
    meta = payload.setdefault("meta", {})
    dest = meta.setdefault(
        "destination",
        {
            "name": "테헤란로 501",
            "address": "서울 강남구 테헤란로 501",
            "lat": 37.5073809,
            "lng": 127.0568213,
            "verified": False,
        },
    )

    cache = load_transit_cache(args.cache_json, destination=dest)
    seeded = seed_cache_from_apartments(payload, cache, record_date_if_missing=now_kst_date())
    if seeded > 0:
        save_transit_cache(args.cache_json, cache)

    cache_coord_hits = 0
    cache_transit_hits = 0
    for apt in apartments:
        ch, th = hydrate_complex_from_cache(apt, cache)
        if ch:
            cache_coord_hits += 1
        if th:
            cache_transit_hits += 1

    scoped_apartments = [apt for apt in apartments if not args.region or apt.get("region") == args.region]

    coord_targets = [
        apt
        for apt in scoped_apartments
        if args.force or not apt.get("coordsVerified") or apt.get("lat") is None or apt.get("lng") is None
    ]
    if args.limit > 0:
        coord_targets = coord_targets[: args.limit]

    rest_key = get_rest_api_key(project_root)
    today = now_kst_date()
    used_today = int((cache.get("dailyTransitCalls") or {}).get(today, 0))

    if args.dry_run:
        print(f"[dry-run] 데이터 파일: {args.app_json}")
        print(f"[dry-run] 영구 캐시: {args.cache_json} (좌표 {len(cache['coordsByComplexKey'])}개, 통근 {len(cache['transitByComplexKey'])}개)")
        print(f"[dry-run] 전체 단지: {len(apartments)}개 | 캐시 복원(좌표 {cache_coord_hits}, 통근 {cache_transit_hits})")
        print(f"[dry-run] Phase 1 좌표 조회 필요: {len(coord_targets)}개")
        print(f"[dry-run] 오늘 대중교통 호출 누적: {used_today}/{args.daily_transit_cap}건 (세션 상한 {args.max_transit}건, 반경 {args.max_distance_km}km)")
        print(f"[dry-run] KAKAO_REST_API_KEY 설정 여부: {'설정됨' if rest_key else '미설정'}")
        return 0

    if not rest_key:
        print(
            "오류: KAKAO_REST_API_KEY가 설정되지 않았습니다.\n"
            f"파일 경로 {project_root / '.env'} 에 KAKAO_REST_API_KEY=... 를 추가한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
        return 1

    coords_saved_count = 0
    transit_stats: Dict[str, int] = {}

    def persist_both() -> None:
        save_transit_cache(args.cache_json, cache)
        save_apartments(args.app_json, payload)

    try:
        if not dest.get("verified"):
            print(f"[0/2] 목적지 좌표 조회 중: {dest['address']}")
            d_lat, d_lng = geocode_destination(str(dest["address"]), rest_key)
            dest["lat"] = d_lat
            dest["lng"] = d_lng
            dest["verified"] = True
            cache["destination"] = dict(dest)
            time.sleep(args.coord_sleep)

        dest_lat = float(dest["lat"])
        dest_lng = float(dest["lng"])

        if coord_targets:
            print(f"[Phase 1] 좌표 미등록 {len(coord_targets)}개 조회 시작 (일 100,000건 무료)...")
            coords_map = cache.setdefault("coordsByComplexKey", {})
            consecutive_coord_net_errors = 0
            for idx, apt in enumerate(coord_targets, start=1):
                try:
                    lat, lng, label = geocode_complex(apt, rest_key, sleep_sec=args.coord_sleep)
                    consecutive_coord_net_errors = 0
                except urllib.error.HTTPError:
                    raise
                except (urllib.error.URLError, TimeoutError, OSError) as net_err:
                    consecutive_coord_net_errors += 1
                    print(f"  [좌표 네트워크 경고 {idx}/{len(coord_targets)}] {apt['name']}: 건너뜀 ({net_err})", file=sys.stderr)
                    if consecutive_coord_net_errors >= 5:
                        raise RuntimeError(f"연속 네트워크 오류 5회로 좌표 수집을 안전 중단합니다: {net_err}") from net_err
                    continue

                if lat is not None and lng is not None:
                    apt["lat"] = lat
                    apt["lng"] = lng
                    apt["coordsVerified"] = True
                    apt["straightDistanceKm"] = haversine_km(lat, lng, dest_lat, dest_lng)
                    coords_map[apt["id"]] = {
                        "lat": lat,
                        "lng": lng,
                        "placeLabel": label,
                        "savedAt": now_utc_iso(),
                    }
                    coords_saved_count += 1
                    if idx % 20 == 0 or idx == len(coord_targets):
                        print(f"  [좌표 {idx}/{len(coord_targets)}] {apt['name']} ({apt['straightDistanceKm']}km)")
                        persist_both()
                else:
                    print(f"  [좌표 실패 {idx}/{len(coord_targets)}] {apt['name']}: {label}", file=sys.stderr)
        else:
            print("[Phase 1] 모든 단지 좌표가 이미 캐시되어 있습니다 (0건 호출).")

        if not args.coords_only:
            transit_subset = scoped_apartments[: args.limit] if args.limit > 0 else scoped_apartments
            print(
                f"[Phase 2] 대중교통 실측 시작 (직선거리 {args.max_distance_km}km 이내, "
                f"오늘 누적 {used_today}/{args.daily_transit_cap}건)..."
            )
            transit_stats = run_transit_enrichment(
                apartments=transit_subset,
                dest=dest,
                cache=cache,
                rest_key=rest_key,
                max_distance_km=args.max_distance_km,
                max_transit=args.max_transit,
                daily_transit_cap=args.daily_transit_cap,
                force_refetch_transit=args.force_refetch_transit,
                sleep_sec=args.sleep,
                on_batch_save=persist_both,
            )

    except urllib.error.HTTPError as http_err:
        print(format_http_error(http_err), file=sys.stderr)
        persist_both()
        return 1
    except KeyboardInterrupt:
        print("\n사용자에 의해 중단되었습니다. 캐시와 데이터를 저장합니다...", file=sys.stderr)
        persist_both()
        return 130
    finally:
        persist_both()

    total_cached_hits = cache_transit_hits + transit_stats.get("cache_hits", 0) + transit_stats.get("already_verified", 0)
    print(
        f"[완료] 좌표 신규: {coords_saved_count}건 | "
        f"통근 캐시 재사용: {total_cached_hits}건 | "
        f"신규 대중교통 API 호출: {transit_stats.get('api_calls', 0)}건 | "
        f"오늘 누적: {transit_stats.get('used_today', used_today)}/{args.daily_transit_cap}건 | "
        f"반경 초과 제외: {transit_stats.get('skipped_distance', 0)}건 | "
        f"쿼터 상한 대기: {transit_stats.get('capped', 0)}건"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
