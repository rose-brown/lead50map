const BUDGET_BUCKETS = [
  { id: 'under4', label: '4억 이하', test: (man) => man <= 40000 },
  { id: '4to6', label: '4~6억', test: (man) => man > 40000 && man <= 60000 },
  { id: '6to8', label: '6~8억', test: (man) => man > 60000 && man <= 80000 },
  { id: '8to12', label: '8~12억', test: (man) => man > 80000 && man <= 120000 },
  { id: 'over12', label: '12억 이상', test: (man) => man > 120000 },
];

const REFERENCE_YEAR = 2026;
const PAGE_SIZE = 50;

const state = {
  config: null,
  meta: null,
  apartments: [],
  filtered: [],
  visibleCount: PAGE_SIZE,
  selectedId: null,
  kakaoMap: null,
  kakaoClusterer: null,
  kakaoOverlays: [],
  kakaoDestOverlay: null,
  kakaoReady: false,
  kakaoError: null,
  filters: {
    source: 'all',
    region: 'all',
    districts: new Set(),
    maxCommute: 60,
    allowOver60: false,
    includeUnmeasured: true,
    maxTransfers: 'all',
    pyeong: 34,
    budgets: new Set(),
    minPyeongPrice: '',
    maxPyeongPrice: '',
    minHouseholds: '',
    minBuiltYear: '',
    maxBuiltYear: '',
    search: '',
    sort: 'commute',
  },
};

function escapeHtml(text) {
  return String(text ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function estimatePriceMan(apt, pyeong) {
  const safePyeong = Number.isFinite(pyeong) && pyeong > 0 ? pyeong : 34;
  return apt.pricePerPyeongMan * safePyeong;
}

function formatManToEok(man) {
  if (!Number.isFinite(man)) return '-';
  return `${(man / 10000).toFixed(1)}억`;
}

function formatNumber(n) {
  return Number(n).toLocaleString('ko-KR');
}

function getCommuteTier(apt) {
  const minutes = apt.commute?.minutes;
  if (minutes === null || minutes === undefined) {
    return { cls: 'tier-unverified', pillCls: 'commute-unverified', label: '통근 미측정' };
  }
  if (minutes <= 30) {
    return { cls: 'tier-fast', pillCls: 'commute-fast', label: `${minutes}분` };
  }
  if (minutes <= 45) {
    return { cls: 'tier-mid', pillCls: 'commute-mid', label: `${minutes}분` };
  }
  if (minutes <= 60) {
    return { cls: 'tier-slow', pillCls: 'commute-slow', label: `${minutes}분` };
  }
  return { cls: 'tier-over', pillCls: 'commute-over', label: `${minutes}분` };
}

function matchesNonBudgetFilters(apt) {
  const f = state.filters;

  if (f.source !== 'all' && !(apt.sources || []).includes(f.source)) {
    return false;
  }
  if (f.region !== 'all' && apt.region !== f.region) {
    return false;
  }
  if (f.districts.size > 0 && !f.districts.has(apt.district || '')) {
    return false;
  }

  const minutes = apt.commute?.minutes;
  const transfers = apt.commute?.transfers;

  if (minutes === null || minutes === undefined) {
    if (!f.includeUnmeasured) return false;
  } else {
    if (!f.allowOver60 && minutes > f.maxCommute) {
      return false;
    }
    if (f.maxTransfers !== 'all') {
      const limit = Number(f.maxTransfers);
      if (transfers === null || transfers === undefined || transfers > limit) {
        return false;
      }
    }
  }

  const minPP = f.minPyeongPrice !== '' ? Number(f.minPyeongPrice) : null;
  const maxPP = f.maxPyeongPrice !== '' ? Number(f.maxPyeongPrice) : null;
  if (minPP !== null && Number.isFinite(minPP) && apt.pricePerPyeongMan < minPP) {
    return false;
  }
  if (maxPP !== null && Number.isFinite(maxPP) && apt.pricePerPyeongMan > maxPP) {
    return false;
  }

  const minHH = f.minHouseholds !== '' ? Number(f.minHouseholds) : null;
  if (minHH !== null && Number.isFinite(minHH) && apt.households < minHH) {
    return false;
  }

  const minYear = f.minBuiltYear !== '' ? Number(f.minBuiltYear) : null;
  const maxYear = f.maxBuiltYear !== '' ? Number(f.maxBuiltYear) : null;
  if (minYear !== null && Number.isFinite(minYear) && apt.builtYear < minYear) {
    return false;
  }
  if (maxYear !== null && Number.isFinite(maxYear) && apt.builtYear > maxYear) {
    return false;
  }

  const q = f.search.trim().toLowerCase();
  if (q) {
    const hay = `${apt.name} ${apt.location} ${apt.dong} ${apt.district || ''} ${apt.region}`.toLowerCase();
    if (!hay.includes(q)) return false;
  }

  return true;
}

function matchesBudget(apt) {
  if (state.filters.budgets.size === 0) return true;
  const est = estimatePriceMan(apt, state.filters.pyeong);
  for (const bId of state.filters.budgets) {
    const bucket = BUDGET_BUCKETS.find((b) => b.id === bId);
    if (bucket && bucket.test(est)) return true;
  }
  return false;
}

function sortApartments(items) {
  const sortKey = state.filters.sort;
  const pyeong = state.filters.pyeong;
  return [...items].sort((a, b) => {
    if (sortKey === 'commute') {
      const am = a.commute?.minutes ?? 9999;
      const bm = b.commute?.minutes ?? 9999;
      if (am !== bm) return am - bm;
      return (a.lead50Rank ?? 9999) - (b.lead50Rank ?? 9999);
    }
    if (sortKey === 'estPriceAsc') {
      return estimatePriceMan(a, pyeong) - estimatePriceMan(b, pyeong);
    }
    if (sortKey === 'pyeongPriceAsc') {
      return a.pricePerPyeongMan - b.pricePerPyeongMan;
    }
    if (sortKey === 'newest') {
      return b.builtYM.localeCompare(a.builtYM);
    }
    if (sortKey === 'householdsDesc') {
      return b.households - a.households;
    }
    if (sortKey === 'marketCapDesc') {
      return b.marketCapTrillion - a.marketCapTrillion;
    }
    return 0;
  });
}

function renderDistrictChips() {
  const container = document.getElementById('district-chips');
  if (!container) return;

  const counts = new Map();
  for (const apt of state.apartments) {
    if (state.filters.region !== 'all' && apt.region !== state.filters.region) continue;
    const dist = apt.district;
    if (!dist) continue;
    const key = `${apt.region}|${dist}`;
    const prev = counts.get(key);
    counts.set(key, {
      region: apt.region,
      district: dist,
      count: (prev ? prev.count : 0) + 1,
    });
  }

  const list = [...counts.values()].sort((a, b) => {
    if (a.region !== b.region) return a.region === '서울' ? -1 : 1;
    return a.district.localeCompare(b.district, 'ko');
  });

  container.innerHTML = list
    .map((item) => {
      const active = state.filters.districts.has(item.district) ? 'active' : '';
      const shortLabel = state.filters.region === 'all' ? `${item.region} ${item.district}` : item.district;
      return `
        <button type="button" class="chip district-chip ${active}" data-district="${escapeHtml(item.district)}">
          <span>${escapeHtml(shortLabel)}</span>
          <small class="chip-count">${item.count}</small>
        </button>
      `;
    })
    .join('');

  container.querySelectorAll('[data-district]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const dist = btn.getAttribute('data-district');
      if (!dist) return;
      if (state.filters.districts.has(dist)) {
        state.filters.districts.delete(dist);
      } else {
        state.filters.districts.add(dist);
      }
      applyFiltersAndRender(true);
    });
  });

  const clearBtn = document.getElementById('btn-clear-districts');
  if (clearBtn) {
    clearBtn.textContent =
      state.filters.districts.size > 0 ? `선택 해제 (${state.filters.districts.size})` : '시/구 전체';
  }
}

function applyFiltersAndRender(resetPaging = true) {
  if (resetPaging) {
    state.visibleCount = PAGE_SIZE;
  }

  renderDistrictChips();

  const preBudget = state.apartments.filter(matchesNonBudgetFilters);
  for (const bucket of BUDGET_BUCKETS) {
    const count = preBudget.filter((apt) => bucket.test(estimatePriceMan(apt, state.filters.pyeong))).length;
    const countEl = document.querySelector(`[data-budget-count="${bucket.id}"]`);
    if (countEl) countEl.textContent = String(count);
  }

  state.filtered = sortApartments(preBudget.filter(matchesBudget));

  updateHeaderPills();
  renderApartmentList();
  renderMapOrFallback();

  if (state.selectedId) {
    const selected = state.apartments.find((a) => a.id === state.selectedId);
    if (selected) renderDetailPanel(selected);
  }
}

function updateHeaderPills() {
  const total = state.apartments.length;
  const enriched = state.meta?.enrichedCount ?? 0;
  const asOf = state.meta?.asOf || '2026-10';

  document.getElementById('pill-asof').textContent = `기준 ${asOf}`;
  document.getElementById('pill-filtered').textContent = `표시 ${state.filtered.length} / ${total}`;
  const enrichedPill = document.getElementById('pill-enriched');
  enrichedPill.textContent = `카카오 좌표·통근 실측 ${enriched} / ${total}`;
  enrichedPill.className = enriched === total && total > 0 ? 'status-pill status-pill-ok' : 'status-pill';

  const mobileCount = document.getElementById('mobile-list-count');
  if (mobileCount) mobileCount.textContent = String(state.filtered.length);
  const mobileApply = document.getElementById('btn-mobile-apply-filters');
  if (mobileApply) mobileApply.textContent = `적용하고 지도 보기 (${state.filtered.length}개)`;
}

function setMobileView(view) {
  const appEl = document.getElementById('app');
  if (!appEl) return;
  appEl.classList.remove('mobile-view-map', 'mobile-view-list', 'mobile-view-filter');
  appEl.classList.add(`mobile-view-${view}`);
  document.querySelectorAll('[data-mobile-view]').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-mobile-view') === view);
  });
  if (view === 'map') {
    setTimeout(() => {
      if (state.kakaoMap && typeof state.kakaoMap.relayout === 'function') {
        state.kakaoMap.relayout();
      }
      renderVisibleMapMarkers();
    }, 50);
  }
}

function renderSourceBadges(apt) {
  const badges = [];
  if (apt.lead50Rank !== null && apt.lead50Rank !== undefined) {
    badges.push(`<span class="badge badge-lead">KB선도 #${apt.lead50Rank}</span>`);
  }
  if (apt.gyeonggiTopRank !== null && apt.gyeonggiTopRank !== undefined) {
    badges.push(`<span class="badge badge-gg">경기TOP #${apt.gyeonggiTopRank}</span>`);
  }
  if (apt.districtRank !== null && apt.districtRank !== undefined && apt.district) {
    badges.push(`<span class="badge badge-dist">${escapeHtml(apt.district)} #${apt.districtRank}</span>`);
  }
  return badges.join('');
}

function renderApartmentList() {
  const listEl = document.getElementById('apt-list');
  if (state.filtered.length === 0) {
    listEl.innerHTML = `<div class="empty-list">조건에 맞는 단지가 없습니다.<br />필터를 완화하거나 초기화해 보세요.</div>`;
    return;
  }

  const pyeong = state.filters.pyeong;
  const shown = state.filtered.slice(0, state.visibleCount);
  const remaining = Math.max(0, state.filtered.length - shown.length);

  let html = shown
    .map((apt) => {
      const tier = getCommuteTier(apt);
      const estEok = formatManToEok(estimatePriceMan(apt, pyeong));
      const isSelected = apt.id === state.selectedId;
      return `
        <button type="button" class="apt-card ${isSelected ? 'selected' : ''}" data-apt-id="${escapeHtml(apt.id)}" role="listitem">
          <div class="card-badges">${renderSourceBadges(apt)}</div>
          <div class="apt-card-top">
            <div>
              <div class="apt-name">${escapeHtml(apt.name)}</div>
              <div class="apt-loc">${escapeHtml(apt.location)}</div>
            </div>
            <span class="commute-pill ${tier.pillCls}">${escapeHtml(tier.label)}</span>
          </div>
          <div class="apt-card-metrics">
            <span>${pyeong}평 추정 <strong>${estEok}</strong></span>
            <span>평당 <strong>${formatNumber(apt.pricePerPyeongMan)}만</strong></span>
            <span>${formatNumber(apt.households)}세대</span>
            <span>${escapeHtml(apt.builtYM)}</span>
          </div>
        </button>
      `;
    })
    .join('');

  if (remaining > 0) {
    html += `<button type="button" id="btn-load-more" class="load-more-btn">더보기 (${remaining}개 남음 · ${PAGE_SIZE}개씩 추가)</button>`;
  }

  listEl.innerHTML = html;

  listEl.querySelectorAll('.apt-card').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-apt-id');
      if (id) selectApartment(id, true);
    });
  });

  document.getElementById('btn-load-more')?.addEventListener('click', () => {
    state.visibleCount += PAGE_SIZE;
    renderApartmentList();
  });
}

function selectApartment(id, panMap = false) {
  state.selectedId = id;
  const apt = state.apartments.find((a) => a.id === id);
  if (!apt) return;

  renderApartmentList();
  renderDetailPanel(apt);

  const isMobile = window.innerWidth <= 960;
  if (isMobile && panMap) {
    setMobileView('map');
  }

  if (state.kakaoReady) {
    if (panMap && state.kakaoMap && apt.lat !== null && apt.lng !== null) {
      const centerAndZoom = () => {
        if (typeof state.kakaoMap.relayout === 'function') state.kakaoMap.relayout();
        if (state.kakaoMap.getLevel() > 6) {
          state.kakaoMap.setLevel(5);
        }
        state.kakaoMap.panTo(new window.kakao.maps.LatLng(apt.lat, apt.lng));
        renderVisibleMapMarkers();
      };
      if (isMobile) {
        setTimeout(centerAndZoom, 80);
      } else {
        centerAndZoom();
      }
    } else {
      renderVisibleMapMarkers();
    }
  }
}

function renderDetailPanel(apt) {
  const panel = document.getElementById('detail-panel');
  const pyeong = state.filters.pyeong;
  const estMan = estimatePriceMan(apt, pyeong);
  const tier = getCommuteTier(apt);

  document.getElementById('detail-badges').innerHTML = renderSourceBadges(apt);
  document.getElementById('detail-name').textContent = apt.name;
  document.getElementById('detail-location').textContent = `${apt.region} · ${apt.location}`;
  document.getElementById('detail-est-label').textContent = `추정 매매가 (${pyeong}평 기준)`;
  document.getElementById('detail-est-price').textContent = `${formatManToEok(estMan)} (${formatNumber(estMan)}만원)`;
  document.getElementById('detail-pyeong-price').textContent = `${formatNumber(apt.pricePerPyeongMan)}만원`;
  document.getElementById('detail-market-cap').textContent = `${apt.marketCapTrillion.toFixed(2)}조`;
  document.getElementById('detail-households').textContent = `${formatNumber(apt.households)}세대`;
  document.getElementById('detail-built').textContent = apt.builtText || apt.builtYM;

  const rankParts = [];
  if (apt.lead50Rank !== null && apt.lead50Rank !== undefined) rankParts.push(`선도50 #${apt.lead50Rank}`);
  if (apt.gyeonggiTopRank !== null && apt.gyeonggiTopRank !== undefined) rankParts.push(`경기TOP #${apt.gyeonggiTopRank}`);
  if (apt.districtRank !== null && apt.districtRank !== undefined && apt.district) {
    rankParts.push(`${apt.district} #${apt.districtRank}`);
  }
  if (apt.straightDistanceKm !== null && apt.straightDistanceKm !== undefined) {
    rankParts.push(`직선 ${apt.straightDistanceKm}km`);
  }
  document.getElementById('detail-ranks').textContent = rankParts.join(' · ') || '-';

  const commuteBox = document.getElementById('detail-commute-box');
  if (apt.commute?.verified && apt.commute?.minutes !== null) {
    const trText =
      apt.commute.transfers !== null && apt.commute.transfers !== undefined ? `환승 ${apt.commute.transfers}회` : '';
    const fareText = apt.commute.fareWon ? ` · 요금 ${formatNumber(apt.commute.fareWon)}원` : '';
    commuteBox.innerHTML = `
      <div><strong>테헤란로 501 대중교통: ${escapeHtml(tier.label)}</strong> ${escapeHtml(trText)}${escapeHtml(fareText)}</div>
      <div class="muted" style="margin-top: 4px;">${escapeHtml(apt.commute.summary || '')}</div>
    `;
  } else if (apt.commute?.skippedReason) {
    commuteBox.innerHTML = `
      <div><strong>대중교통 미측정 (반경 초과 제외)</strong></div>
      <div class="muted" style="margin-top: 4px;">테헤란로 501 기준 직선거리 30km 초과로 자동 측정에서 제외되었습니다. 카카오맵 길찾기 버튼으로 즉시 확인할 수 있습니다.</div>
    `;
  } else {
    commuteBox.innerHTML = `
      <div><strong>대중교통 통근시간 미측정</strong></div>
      <div class="muted" style="margin-top: 4px;"><code>python3 scripts/enrich_kakao.py</code> 실행 시 1회 실측 후 영구 캐시에 저장됩니다.</div>
    `;
  }

  const destName = state.config?.destination?.address || '서울 강남구 테헤란로 501';
  const kakaoUrl =
    apt.commute?.landingUrl ||
    `https://map.kakao.com/?sName=${encodeURIComponent(`${apt.location} ${apt.name}`)}&eName=${encodeURIComponent(destName)}`;
  const naverUrl = `https://map.naver.com/p/search/${encodeURIComponent(`${apt.location} ${apt.name}`)}`;
  const kbLink = document.getElementById('link-kb-detail');
  if (apt.kbComplexId) {
    kbLink.href = `https://kbland.kr/c/${encodeURIComponent(String(apt.kbComplexId))}`;
    kbLink.textContent = 'KB 단지 상세 보기';
  } else {
    kbLink.href = 'https://data.kbland.kr/kbstats/lead50';
    kbLink.textContent = 'KB 선도아파트 보기';
  }

  document.getElementById('link-kakao-route').href = kakaoUrl;
  document.getElementById('link-naver-search').href = naverUrl;
  panel.classList.remove('hidden');
}

function renderMapOrFallback() {
  const banner = document.getElementById('map-banner');
  const fallback = document.getElementById('fallback-canvas');
  const controls = document.getElementById('map-controls');
  const legend = document.getElementById('map-legend');
  const zoomHint = document.getElementById('map-zoom-hint');

  if (state.kakaoReady && state.kakaoMap) {
    fallback.classList.add('hidden');
    controls.classList.remove('hidden');
    legend.classList.remove('hidden');

    const withCoords = state.filtered.filter((a) => a.lat !== null && a.lng !== null);
    if (withCoords.length === 0 && state.filtered.length > 0) {
      banner.innerHTML = `
        <span>카카오 지도는 연결되었지만 아직 아파트 좌표가 비어 있습니다. <code>python3 scripts/enrich_kakao.py</code>를 실행하면 지도 핀과 통근시간이 채워집니다.</span>
        <button type="button" class="btn-outline" id="btn-banner-guide">실행 가이드</button>
      `;
      banner.classList.remove('hidden');
      zoomHint?.classList.add('hidden');
      document.getElementById('btn-banner-guide')?.addEventListener('click', openGuideModal);
    } else {
      banner.classList.add('hidden');
    }

    renderVisibleMapMarkers();
    return;
  }

  controls.classList.add('hidden');
  legend.classList.add('hidden');
  banner.classList.add('hidden');
  zoomHint?.classList.add('hidden');
  fallback.classList.remove('hidden');

  if (state.kakaoError) {
    document.getElementById('fallback-title').textContent = '카카오맵 SDK 로드에 실패했습니다';
    document.getElementById('fallback-desc').textContent = state.kakaoError;
  }
  const envPath = state.config?.envPath || '/Users/hkeum/.gemini/antigravity/scratch/apt-commute-map/.env';
  document.getElementById('fallback-env-path').textContent = envPath;

  renderFallbackSummary();
}

function renderFallbackSummary() {
  const total = state.filtered.length;
  const pyeong = state.filters.pyeong;
  document.getElementById('summary-total-label').textContent = `일치 ${total}개 / 전체 ${state.apartments.length}개`;
  document.getElementById('summary-budget-title').textContent = `예산대 분포 (${pyeong}평 기준 추정가)`;

  const seoul = state.filtered.filter((a) => a.region === '서울').length;
  const gyeonggi = state.filtered.filter((a) => a.region === '경기').length;
  const maxRegion = Math.max(1, seoul, gyeonggi);

  document.getElementById('summary-regions').innerHTML = [
    { label: '서울', count: seoul },
    { label: '경기', count: gyeonggi },
  ]
    .map(
      (r) => `
    <div class="bar-row">
      <span>${r.label}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round((r.count / maxRegion) * 100)}%"></div></div>
      <span class="bar-count">${r.count}</span>
    </div>`
    )
    .join('');

  const budgetCounts = BUDGET_BUCKETS.map((b) => ({
    label: b.label,
    count: state.filtered.filter((a) => b.test(estimatePriceMan(a, pyeong))).length,
  }));
  const maxBudget = Math.max(1, ...budgetCounts.map((b) => b.count));

  document.getElementById('summary-budgets').innerHTML = budgetCounts
    .map(
      (b) => `
    <div class="bar-row">
      <span>${b.label}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round((b.count / maxBudget) * 100)}%"></div></div>
      <span class="bar-count">${b.count}</span>
    </div>`
    )
    .join('');

  const previewItems = state.filtered.slice(0, 12);
  document.getElementById('summary-preview-grid').innerHTML = previewItems.length
    ? previewItems
        .map(
          (apt) => `
      <button type="button" class="preview-tile" data-preview-id="${escapeHtml(apt.id)}">
        <strong>${escapeHtml(apt.name)}</strong>
        <span>${escapeHtml(apt.location)} · ${pyeong}평 추정 ${formatManToEok(estimatePriceMan(apt, pyeong))}</span>
      </button>`
        )
        .join('')
    : '<p class="muted">현재 필터 조건에 해당하는 단지가 없습니다.</p>';

  document.querySelectorAll('.preview-tile').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-preview-id');
      if (id) selectApartment(id, false);
    });
  });
}

function clearMapOverlays() {
  for (const overlay of state.kakaoOverlays) {
    overlay.setMap(null);
  }
  state.kakaoOverlays = [];
}

function getPinRankInfo(apt) {
  if (apt.districtRank != null) {
    return { label: `#${apt.districtRank}`, isTop3: apt.districtRank <= 3 };
  }
  if (apt.lead50Rank != null) {
    return { label: `선도#${apt.lead50Rank}`, isTop3: apt.lead50Rank <= 3 };
  }
  if (apt.gyeonggiTopRank != null) {
    return { label: `경기#${apt.gyeonggiTopRank}`, isTop3: apt.gyeonggiTopRank <= 3 };
  }
  return null;
}

function createPillOverlay(apt) {
  const pyeong = state.filters.pyeong;
  const tier = getCommuteTier(apt);
  const estEok = formatManToEok(estimatePriceMan(apt, pyeong));
  const rank = getPinRankInfo(apt);
  const el = document.createElement('button');
  el.type = 'button';
  el.className = `kakao-pill ${tier.cls}${apt.id === state.selectedId ? ' selected' : ''}`;
  const rankHtml = rank
    ? `<span class="kakao-rank${rank.isTop3 ? ' top3' : ''}">${escapeHtml(rank.label)}</span>`
    : '';
  const timeLabel = apt.commute?.minutes != null ? `${apt.commute.minutes}분` : '미측정';
  el.innerHTML = `${rankHtml}<span>${escapeHtml(apt.name)}</span><span>${escapeHtml(timeLabel)}</span><span>${escapeHtml(estEok)}</span>`;
  el.addEventListener('click', () => selectApartment(apt.id, false));

  const overlay = new window.kakao.maps.CustomOverlay({
    position: new window.kakao.maps.LatLng(apt.lat, apt.lng),
    content: el,
    yAnchor: 1,
    zIndex: apt.id === state.selectedId ? 60 : (rank?.isTop3 ? 25 : 10),
  });
  overlay.setMap(state.kakaoMap);
  state.kakaoOverlays.push(overlay);
}

function renderVisibleMapMarkers() {
  if (!state.kakaoReady || !state.kakaoMap) return;
  clearMapOverlays();
  if (state.kakaoClusterer) {
    state.kakaoClusterer.clear();
  }

  const zoomHint = document.getElementById('map-zoom-hint');
  const withCoords = state.filtered.filter((a) => a.lat !== null && a.lng !== null);
  const level = state.kakaoMap.getLevel();
  const useCluster = Boolean(state.kakaoClusterer) && level >= 7 && withCoords.length > 60;

  if (useCluster) {
    zoomHint?.classList.remove('hidden');
    const pyeong = state.filters.pyeong;
    const markers = withCoords.map((apt) => {
      const rank = getPinRankInfo(apt);
      const timeLabel = apt.commute?.minutes != null ? `${apt.commute.minutes}분` : '미측정';
      const estEok = formatManToEok(estimatePriceMan(apt, pyeong));
      const rankPrefix = rank ? `${rank.label} ` : '';
      const marker = new window.kakao.maps.Marker({
        position: new window.kakao.maps.LatLng(apt.lat, apt.lng),
        title: `${rankPrefix}${apt.name} (${timeLabel}, ${estEok})`,
      });
      window.kakao.maps.event.addListener(marker, 'click', () => selectApartment(apt.id, false));
      return marker;
    });
    state.kakaoClusterer.addMarkers(markers);

    if (state.selectedId) {
      const sel = withCoords.find((a) => a.id === state.selectedId);
      if (sel) createPillOverlay(sel);
    }
    return;
  }

  zoomHint?.classList.add('hidden');
  const bounds = state.kakaoMap.getBounds();
  for (const apt of withCoords) {
    const pos = new window.kakao.maps.LatLng(apt.lat, apt.lng);
    const inBounds = bounds ? bounds.contain(pos) : true;
    if (inBounds || apt.id === state.selectedId) {
      createPillOverlay(apt);
    }
  }
}

function fitMapBounds() {
  if (!state.kakaoReady || !state.kakaoMap) return;
  const dest = state.meta?.destination || state.config?.destination;
  const bounds = new window.kakao.maps.LatLngBounds();
  let count = 0;

  if (dest?.lat && dest?.lng) {
    bounds.extend(new window.kakao.maps.LatLng(dest.lat, dest.lng));
    count += 1;
  }
  for (const apt of state.filtered) {
    if (apt.lat !== null && apt.lng !== null) {
      bounds.extend(new window.kakao.maps.LatLng(apt.lat, apt.lng));
      count += 1;
    }
  }
  if (count >= 2) {
    state.kakaoMap.setBounds(bounds);
  } else if (dest?.lat && dest?.lng) {
    state.kakaoMap.setCenter(new window.kakao.maps.LatLng(dest.lat, dest.lng));
    state.kakaoMap.setLevel(6);
  }
}

function initKakaoMap(jsKey) {
  return new Promise((resolve) => {
    const script = document.createElement('script');
    script.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(jsKey)}&autoload=false&libraries=clusterer`;
    script.async = true;

    script.onload = () => {
      const currentOrigin = window.location.origin || 'http://localhost:5173';
      if (!window.kakao || !window.kakao.maps) {
        state.kakaoError =
          `카카오맵 SDK 객체를 불러오지 못했습니다. Kakao Developers > [제품 설정] > [카카오맵] 사용 설정 ON 및 [앱 설정] > [앱] > [플랫폼 키] > [JavaScript 키] > JavaScript SDK 도메인(${currentOrigin})을 확인하세요.`;
        resolve(false);
        return;
      }
      window.kakao.maps.load(() => {
        const dest = state.meta?.destination || state.config?.destination || { lat: 37.5073809, lng: 127.0568213 };
        const container = document.getElementById('kakao-map');
        const map = new window.kakao.maps.Map(container, {
          center: new window.kakao.maps.LatLng(dest.lat, dest.lng),
          level: 7,
        });
        state.kakaoMap = map;
        state.kakaoReady = true;

        if (window.kakao.maps.MarkerClusterer) {
          state.kakaoClusterer = new window.kakao.maps.MarkerClusterer({
            map,
            averageCenter: true,
            minLevel: 7,
            disableClickZoom: false,
            styles: [
              {
                width: '40px',
                height: '40px',
                background: 'rgba(37, 99, 235, 0.88)',
                borderRadius: '20px',
                color: '#ffffff',
                textAlign: 'center',
                fontWeight: '700',
                lineHeight: '40px',
                border: '2px solid #ffffff',
                boxShadow: '0 2px 8px rgba(15, 23, 42, 0.25)',
              },
            ],
          });
        }

        const destPin = document.createElement('div');
        destPin.className = 'kakao-dest-pin';
        destPin.textContent = '★ 테헤란로 501 (기준)';
        state.kakaoDestOverlay = new window.kakao.maps.CustomOverlay({
          position: new window.kakao.maps.LatLng(dest.lat, dest.lng),
          content: destPin,
          yAnchor: 1.2,
          zIndex: 80,
        });
        state.kakaoDestOverlay.setMap(map);

        window.kakao.maps.event.addListener(map, 'idle', renderVisibleMapMarkers);
        resolve(true);
      });
    };

    script.onerror = () => {
      const currentOrigin = window.location.origin || 'http://localhost:5173';
      state.kakaoError =
        `카카오맵 SDK 스크립트를 불러오지 못했습니다. JavaScript 키 설정, [카카오맵] 사용 설정 ON, [앱 설정] > [앱] > [플랫폼 키] > [JavaScript 키] > JavaScript SDK 도메인(${currentOrigin}) 등록을 확인하세요.`;
      resolve(false);
    };

    document.head.appendChild(script);
  });
}

function openGuideModal() {
  const modal = document.getElementById('guide-modal');
  const hasJs = Boolean(state.config?.hasKakaoJsKey);
  const hasRest = Boolean(state.config?.hasRestKey);
  const enriched = state.meta?.enrichedCount ?? 0;
  const total = state.apartments.length;

  document.getElementById('modal-js-status').textContent = hasJs ? '설정됨' : '미설정';
  document.getElementById('modal-rest-status').textContent = hasRest ? '설정됨' : '미설정';
  document.getElementById('modal-enriched-status').textContent = `${enriched} / ${total}`;
  document.getElementById('modal-env-path').textContent =
    state.config?.envPath || '/Users/hkeum/.gemini/antigravity/scratch/apt-commute-map/.env';
  modal.classList.remove('hidden');
}

function syncFilterControlsFromState() {
  const f = state.filters;
  document.querySelectorAll('[data-filter-source]').forEach((el) => {
    el.classList.toggle('active', el.getAttribute('data-filter-source') === f.source);
  });
  document.querySelectorAll('[data-filter-region]').forEach((el) => {
    el.classList.toggle('active', el.getAttribute('data-filter-region') === f.region);
  });
  document.getElementById('input-max-commute').value = String(f.maxCommute);
  document.getElementById('commute-slider-label').textContent = f.allowOver60
    ? '전체 (상한 없음)'
    : `${f.maxCommute}분 이내`;
  document.getElementById('chk-over-60').checked = f.allowOver60;
  document.getElementById('chk-include-unmeasured').checked = f.includeUnmeasured;
  document.querySelectorAll('[data-filter-transfer]').forEach((el) => {
    el.classList.toggle('active', el.getAttribute('data-filter-transfer') === String(f.maxTransfers));
  });

  document.getElementById('input-pyeong').value = String(f.pyeong);
  document.getElementById('pyeong-badge').textContent = `${f.pyeong}평 기준`;
  document.querySelectorAll('[data-pyeong]').forEach((el) => {
    el.classList.toggle('active', Number(el.getAttribute('data-pyeong')) === f.pyeong);
  });
  document.querySelectorAll('[data-budget]').forEach((el) => {
    el.classList.toggle('active', f.budgets.has(el.getAttribute('data-budget')));
  });

  document.getElementById('input-price-min').value = f.minPyeongPrice;
  document.getElementById('input-price-max').value = f.maxPyeongPrice;
  document.getElementById('input-min-households').value = f.minHouseholds;
  document.querySelectorAll('[data-households]').forEach((el) => {
    el.classList.toggle('active', el.getAttribute('data-households') === String(f.minHouseholds));
  });
  document.getElementById('input-year-min').value = f.minBuiltYear;
  document.getElementById('input-year-max').value = f.maxBuiltYear;
  document.getElementById('input-search').value = f.search;
  document.getElementById('select-sort').value = f.sort;
}

function pruneDistrictsForRegion() {
  if (state.filters.region === 'all') return;
  const allowed = new Set(
    state.apartments.filter((a) => a.region === state.filters.region && a.district).map((a) => a.district)
  );
  for (const d of [...state.filters.districts]) {
    if (!allowed.has(d)) state.filters.districts.delete(d);
  }
}

function bindEvents() {
  document.querySelectorAll('[data-filter-source]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.filters.source = btn.getAttribute('data-filter-source') || 'all';
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });

  document.querySelectorAll('[data-filter-region]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.filters.region = btn.getAttribute('data-filter-region') || 'all';
      pruneDistrictsForRegion();
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });

  document.getElementById('btn-clear-districts')?.addEventListener('click', () => {
    state.filters.districts.clear();
    applyFiltersAndRender(true);
  });

  document.getElementById('input-max-commute').addEventListener('input', (e) => {
    state.filters.maxCommute = Number(e.target.value);
    state.filters.allowOver60 = false;
    syncFilterControlsFromState();
    applyFiltersAndRender(true);
  });

  document.getElementById('chk-over-60').addEventListener('change', (e) => {
    state.filters.allowOver60 = e.target.checked;
    syncFilterControlsFromState();
    applyFiltersAndRender(true);
  });

  document.getElementById('chk-include-unmeasured').addEventListener('change', (e) => {
    state.filters.includeUnmeasured = e.target.checked;
    applyFiltersAndRender(true);
  });

  document.querySelectorAll('[data-filter-transfer]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.filters.maxTransfers = btn.getAttribute('data-filter-transfer') || 'all';
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });

  document.querySelectorAll('[data-pyeong]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.filters.pyeong = Number(btn.getAttribute('data-pyeong')) || 34;
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });

  document.getElementById('input-pyeong').addEventListener('input', (e) => {
    const val = Number(e.target.value);
    if (Number.isFinite(val) && val > 0) {
      state.filters.pyeong = val;
      document.getElementById('pyeong-badge').textContent = `${val}평 기준`;
      document.querySelectorAll('[data-pyeong]').forEach((el) => {
        el.classList.toggle('active', Number(el.getAttribute('data-pyeong')) === val);
      });
      applyFiltersAndRender(true);
    }
  });

  document.querySelectorAll('[data-budget]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-budget');
      if (!id) return;
      if (state.filters.budgets.has(id)) {
        state.filters.budgets.delete(id);
      } else {
        state.filters.budgets.add(id);
      }
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });

  document.getElementById('input-price-min').addEventListener('input', (e) => {
    state.filters.minPyeongPrice = e.target.value;
    applyFiltersAndRender(true);
  });
  document.getElementById('input-price-max').addEventListener('input', (e) => {
    state.filters.maxPyeongPrice = e.target.value;
    applyFiltersAndRender(true);
  });

  document.querySelectorAll('[data-households]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.filters.minHouseholds = btn.getAttribute('data-households') || '';
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });
  document.getElementById('input-min-households').addEventListener('input', (e) => {
    state.filters.minHouseholds = e.target.value;
    document.querySelectorAll('[data-households]').forEach((el) => {
      el.classList.toggle('active', el.getAttribute('data-households') === e.target.value);
    });
    applyFiltersAndRender(true);
  });

  document.querySelectorAll('[data-age-preset]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const preset = btn.getAttribute('data-age-preset');
      document.querySelectorAll('[data-age-preset]').forEach((el) => el.classList.remove('active'));
      btn.classList.add('active');
      if (preset === 'all') {
        state.filters.minBuiltYear = '';
        state.filters.maxBuiltYear = '';
      } else if (preset === '5y') {
        state.filters.minBuiltYear = String(REFERENCE_YEAR - 5);
        state.filters.maxBuiltYear = '';
      } else if (preset === '10y') {
        state.filters.minBuiltYear = String(REFERENCE_YEAR - 10);
        state.filters.maxBuiltYear = '';
      } else if (preset === '20y') {
        state.filters.minBuiltYear = String(REFERENCE_YEAR - 20);
        state.filters.maxBuiltYear = '';
      } else if (preset === 'over30y') {
        state.filters.minBuiltYear = '';
        state.filters.maxBuiltYear = String(REFERENCE_YEAR - 30);
      }
      syncFilterControlsFromState();
      applyFiltersAndRender(true);
    });
  });

  document.getElementById('input-year-min').addEventListener('input', (e) => {
    state.filters.minBuiltYear = e.target.value;
    document.querySelectorAll('[data-age-preset]').forEach((el) => el.classList.remove('active'));
    applyFiltersAndRender(true);
  });
  document.getElementById('input-year-max').addEventListener('input', (e) => {
    state.filters.maxBuiltYear = e.target.value;
    document.querySelectorAll('[data-age-preset]').forEach((el) => el.classList.remove('active'));
    applyFiltersAndRender(true);
  });

  document.getElementById('input-search').addEventListener('input', (e) => {
    state.filters.search = e.target.value;
    applyFiltersAndRender(true);
  });
  document.getElementById('select-sort').addEventListener('change', (e) => {
    state.filters.sort = e.target.value;
    applyFiltersAndRender(true);
  });

  document.getElementById('btn-reset-filters').addEventListener('click', () => {
    state.filters = {
      source: 'all',
      region: 'all',
      districts: new Set(),
      maxCommute: 60,
      allowOver60: false,
      includeUnmeasured: true,
      maxTransfers: 'all',
      pyeong: 34,
      budgets: new Set(),
      minPyeongPrice: '',
      maxPyeongPrice: '',
      minHouseholds: '',
      minBuiltYear: '',
      maxBuiltYear: '',
      search: '',
      sort: 'commute',
    };
    document.querySelectorAll('[data-age-preset]').forEach((el) => {
      el.classList.toggle('active', el.getAttribute('data-age-preset') === 'all');
    });
    syncFilterControlsFromState();
    applyFiltersAndRender(true);
  });

  document.getElementById('btn-close-detail').addEventListener('click', () => {
    state.selectedId = null;
    document.getElementById('detail-panel').classList.add('hidden');
    renderApartmentList();
    if (state.kakaoReady) renderVisibleMapMarkers();
  });

  document.getElementById('btn-center-dest').addEventListener('click', () => {
    if (!state.kakaoMap) return;
    const dest = state.meta?.destination || state.config?.destination;
    if (dest) {
      state.kakaoMap.panTo(new window.kakao.maps.LatLng(dest.lat, dest.lng));
      state.kakaoMap.setLevel(7);
    }
  });

  document.getElementById('btn-fit-bounds').addEventListener('click', fitMapBounds);

  document.querySelectorAll('[data-mobile-view]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const view = btn.getAttribute('data-mobile-view') || 'map';
      setMobileView(view);
    });
  });
  document.getElementById('btn-mobile-apply-filters')?.addEventListener('click', () => {
    setMobileView('map');
  });

  document.getElementById('btn-open-guide').addEventListener('click', openGuideModal);
  document.getElementById('btn-close-modal').addEventListener('click', () => {
    document.getElementById('guide-modal').classList.add('hidden');
  });
  document.getElementById('guide-modal').addEventListener('click', (e) => {
    if (e.target.id === 'guide-modal') {
      document.getElementById('guide-modal').classList.add('hidden');
    }
  });
}

async function loadConfig() {
  const host = window.location.hostname;
  const isLocal = host === 'localhost' || host === '127.0.0.1';
  if (isLocal) {
    try {
      const localRes = await fetch('./api/config', { cache: 'no-store' });
      if (localRes.ok) return await localRes.json();
    } catch (_) {
      // Fallback to static config.json
    }
  }
  const staticRes = await fetch('./config.json', { cache: 'no-store' });
  if (!staticRes.ok) {
    throw new Error(`config.json 로드 실패 (${staticRes.status})`);
  }
  return await staticRes.json();
}

async function bootstrap() {
  bindEvents();
  syncFilterControlsFromState();

  try {
    const [cfg, dataRes] = await Promise.all([
      loadConfig(),
      fetch('./data/apartments.json', { cache: 'no-store' }),
    ]);

    state.config = cfg;
    const data = await dataRes.json();
    state.meta = data.meta || {};
    state.apartments = data.apartments || [];

    if (state.config?.hasKakaoJsKey && state.config?.kakaoJsKey) {
      await initKakaoMap(state.config.kakaoJsKey);
    }
  } catch (err) {
    state.kakaoError = `초기 데이터를 불러오는 중 오류가 발생했습니다: ${err.message}`;
  }

  applyFiltersAndRender(true);
}

bootstrap();
