/**
 * KB부동산 월간 선도아파트 (https://data.kbland.kr/kbstats/lead50)
 * Chrome DevTools Console 추출 스니펫 (읽기 전용 DOM 파서)
 *
 * 사용법:
 * 1. 브라우저에서 KB선도아파트50 또는 경기 시세총액 TOP 테이블의 "더보기"를 끝까지 눌러 50개 행이 모두 보이게 합니다.
 * 2. F12 -> Console 탭에 아래 코드를 붙여넣고 Enter를 누릅니다.
 * 3. 클립보드에 JSON이 복사되면 파일(예: lead50.json, gyeonggi.json)로 저장한 뒤:
 *    python3 scripts/import_kb_table.py --lead50 lead50.json --gyeonggi gyeonggi.json
 */
(function extractKbLeadTable() {
  const rows = [];
  const trList = Array.from(document.querySelectorAll('table tbody tr'));

  for (const tr of trList) {
    const cells = Array.from(tr.querySelectorAll('td')).map((td) =>
      td.innerText.replace(/\s+/g, ' ').trim()
    );
    if (cells.length < 6) continue;

    const rank = parseInt(cells[0].replace(/[^\d]/g, ''), 10);
    if (!Number.isFinite(rank)) continue;

    // 2번째 셀: "단지명" + 줄바꿈 + "위치" 또는 공백 구분
    const secondTd = tr.querySelectorAll('td')[1];
    const lines = secondTd
      ? secondTd.innerText
          .split('\n')
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    const name = lines[0] || cells[1];
    const location = lines.slice(1).join(' ') || '';

    rows.push({
      rank,
      name,
      location,
      marketCap: cells[2],
      pricePerPyeong: cells[3],
      households: cells[4],
      builtYM: cells[5],
    });
  }

  const jsonText = JSON.stringify(rows, null, 2);
  console.log('[KB 추출 완료] 총 ' + rows.length + '개 단지');
  if (typeof copy === 'function') {
    copy(jsonText);
    console.log('클립보드에 JSON이 복사되었습니다.');
  } else if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(jsonText).catch(() => {});
  }
  return rows;
})();
