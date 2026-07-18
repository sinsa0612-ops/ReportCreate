"""보고서 투입 자료 선별 — '필요'를 먼저 채우고, '신뢰도'는 같은 필요 안의 심판으로 쓴다.

배경 (2026-06-10 파이프라인 평가 C2):
  수집은 런당 250~320건이지만 보고서에 투입되는 것은 MAX_DOCS(40)건이다.
  trust_score 단독 정렬은 정부 API의 목록형 메타데이터(특허 초록·R&D 과제 목록·
  통계 스텁, 본문 수백 자)가 S/SS 등급으로 상위를 독식해, 원문이 확보된
  논문·웹 분석자료가 전량 탈락하는 문제를 낳았다(실측: top40의 37건이 500자 미만).

배경 2 (2026-06-10 19차 — 권위 ≠ 필요):
  점수 보정만으로는 등급 기본 점수 격차(B~55 vs S~90)를 못 넘는다. 수소 보고서
  실측: 리뷰가 '가장 치명적 공백'이라 지적한 LCOE·시장규모 자료 33건이 전부
  B급이라 탈락. → 각도(시장·정책·비교·실증)별 최소 슬롯을 보장하는
  '각도 균형' 단계를 추가. 등급은 같은 각도 안에서의 우선순위 심판으로 쓰인다.

선별 규칙:
  1. 선별 점수 = trust_score + 내용 충실도 보정(본문 길이 구간별 -8 ~ +8)
     + 원문 확보 보너스(+6, fulltext_urls 에 URL 이 있을 때)
  2. 목록형 소스는 그룹 쿼터 상한(특허 5 · 통계 3 · 목록 5) — 초과분 건너뜀.
     initial_counts 로 이전 배치(기본 선별)의 그룹 사용량을 이어받아,
     검증 재수집 배치에서 쿼터가 리셋되지 않게 한다(2026-06-10 18차).
  3. 점수 내림차순(동점 시 입력 순서 유지)으로 max_docs 까지 채움
  4. 각도 균형(19차): 시장·정책·비교·실증 각도별 최소 ANGLE_MIN 건을 보장.
     부족한 각도는 탈락분에서 해당 각도 최고점 문서를 끌어올리고, 대신
     '수호자가 아닌'(어떤 각도의 최소선도 지키지 않는) 최저점 문서를 내린다.

쿼터에 밀린 문서도 수집 JSON·참고자료 목록에는 남는다. 보고서 본문 투입만 제한한다.
신뢰도 '등급' 체계(trust.py)는 건드리지 않는다 — 투입 순위만 별도로 계산한다.
"""
import re

# 목록형(메타데이터 위주) 소스 → 쿼터 그룹
LISTING_GROUP = {
    "kipris": "patent", "kipris_foreign": "patent",
    "kosis": "stats", "eia": "stats",
    "gas_safety": "listing", "nalib": "listing",
}
GROUP_CAPS = {"patent": 5, "stats": 3, "listing": 5}
FULLTEXT_BONUS = 6   # 원문 파일이 확보된 문서 가점 (스니펫 길이만으로는 알 수 없는 충실도)

# 각도 균형(19차) — 보고서 챕터가 항상 필요로 하는 자료 종류별 최소 슬롯.
# '기술 현황' 각도는 수집물 대다수가 자연 커버하므로 쿼터를 두지 않는다.
ANGLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "market":  ("lcoe", "lcoh", "capex", "opex", "cagr", "levelized",
                "market", "cost", "price", "investment", "usd", "krw",
                "billion", "시장", "비용", "단가", "원가", "경제성", "가격", "투자"),
    "policy":  ("policy", "regulation", "subsidy", "roadmap", "strategy",
                "incentive", "ministry", "정책", "정부", "로드맵", "제도",
                "예산", "보조금", "지원사업", "고시", "법안", "입법"),
    "compare": ("comparison", "versus", "alternative", "benchmark",
                "trade-off", "비교", "대안", "대비", "경쟁"),
    "demo":    ("pilot", "demonstration", "deployment", "commissioning",
                "실증", "파일럿", "상용화", "착공", "준공", "실증사업"),
}
ANGLE_MIN = 3   # 각도별 최소 확보 건수 (해당 각도 자료가 풀에 있을 때만)


def content_bonus(doc: dict) -> float:
    """본문(스니펫) 길이 기반 내용 충실도 보정. 스텁(-8) ~ 풍부(+8)."""
    n = len((doc.get("content") or "").strip())
    if n >= 2000:
        return 8
    if n >= 800:
        return 4
    if n >= 300:
        return 0
    return -8


def selection_score(doc: dict, fulltext_urls: set | None = None) -> float:
    """보고서 투입 순위 점수. 등급 점수에 내용 충실도·원문 확보를 보정한다."""
    score = (doc.get("trust_score") or 0) + content_bonus(doc)
    if fulltext_urls and doc.get("url") in fulltext_urls:
        score += FULLTEXT_BONUS
    return score


def count_groups(docs: list[dict]) -> dict[str, int]:
    """문서 목록의 목록형 그룹별 건수 — 배치 간 쿼터 누적용."""
    counts: dict[str, int] = {}
    for d in docs:
        group = LISTING_GROUP.get(d.get("source") or "")
        if group:
            counts[group] = counts.get(group, 0) + 1
    return counts


def doc_angles(doc: dict) -> set[str]:
    """문서가 커버하는 각도 집합 — 제목+본문(스니펫) 키워드 매칭.

    영어 키워드는 단어 경계(\\b) 매칭('usd'가 'used'에 오매칭 방지),
    한국어 키워드는 부분 문자열 매칭(조사 결합 허용).
    """
    text = ((doc.get("title") or "") + " " + (doc.get("content") or "")).lower()
    angles: set[str] = set()
    for angle, terms in ANGLE_KEYWORDS.items():
        for t in terms:
            if t.isascii():
                if re.search(rf"\b{re.escape(t)}\b", text):
                    angles.add(angle)
                    break
            elif t in text:
                angles.add(angle)
                break
    return angles


def _balance_angles(out: list[dict], rest: list[dict],
                    counts: dict[str, int], max_docs: int,
                    angle_min: int) -> None:
    """부족한 각도를 탈락분(rest)에서 끌어올려 최소 슬롯을 보장한다(out 직접 수정).

    - 후보: rest 에서 해당 각도 매칭 + 그룹 쿼터 여유가 있는 최고점 문서.
    - 자리가 꽉 찼으면 '수호자가 아닌' 문서(자신이 속한 모든 각도가 이미
      최소선 초과이거나 어떤 각도도 커버하지 않는)를 최저점부터 내린다.
    - 각도 순서·후보 순서가 모두 고정이라 결정적이다.
    """
    angle_cache: dict[int, set[str]] = {}

    def _angles(d: dict) -> set[str]:
        key = id(d)
        if key not in angle_cache:
            angle_cache[key] = doc_angles(d)
        return angle_cache[key]

    tally = {a: 0 for a in ANGLE_KEYWORDS}
    for d in out:
        for a in _angles(d):
            tally[a] += 1

    for angle in ANGLE_KEYWORDS:            # dict 정의 순서 고정 → 결정적
        while tally[angle] < angle_min:
            cand = None
            for d in rest:                  # rest 는 점수순 → 최고점 후보
                if angle not in _angles(d):
                    continue
                group = LISTING_GROUP.get(d.get("source") or "")
                if group and counts.get(group, 0) >= GROUP_CAPS[group]:
                    continue
                cand = d
                break
            if cand is None:                # 풀에 이 각도 자료가 없음
                break

            if len(out) >= max_docs:
                victim = None
                for d in reversed(out):     # 최저점부터
                    if all(tally[a] > angle_min for a in _angles(d)):
                        victim = d
                        break
                if victim is None:          # 모두 수호자 → 더 못 바꿈
                    break
                out.remove(victim)
                vg = LISTING_GROUP.get(victim.get("source") or "")
                if vg:
                    counts[vg] = counts.get(vg, 0) - 1
                for a in _angles(victim):
                    tally[a] -= 1

            rest.remove(cand)
            cg = LISTING_GROUP.get(cand.get("source") or "")
            if cg:
                counts[cg] = counts.get(cg, 0) + 1
            out.append(cand)
            for a in _angles(cand):
                tally[a] += 1


def select_for_report(docs: list[dict], max_docs: int,
                      fulltext_urls: set | None = None,
                      initial_counts: dict[str, int] | None = None,
                      angle_min: int = ANGLE_MIN) -> list[dict]:
    """보고서에 투입할 문서를 선별한다.

    입력이 같으면 결과도 같다(결정적) — outline 의 위치 기반 자료 인덱스가
    stage 간·검증 반복 간에 흔들리지 않기 위한 필수 성질.
    (fulltext_urls 는 fetch 완료 후 고정되는 manifest 기반이므로 결정성을 해치지 않는다.)

    - fulltext_urls: 원문 파일이 확보된 URL 집합 → 선별 점수 +6
    - initial_counts: 이전 배치가 이미 사용한 그룹 쿼터(count_groups 결과).
      검증 재수집 배치 선별 시 기본 선별의 사용량을 이어받아 쿼터 리셋을 막는다.
    - angle_min: 각도(시장·정책·비교·실증)별 최소 확보 건수. 0이면 균형 단계 생략
      (검증 재수집 배치처럼 이미 공백을 겨냥해 수집된 자료에 사용).
    """
    ranked = sorted(docs, key=lambda d: selection_score(d, fulltext_urls),
                    reverse=True)  # 안정 정렬
    counts: dict[str, int] = dict(initial_counts or {})
    out: list[dict] = []
    rest: list[dict] = []
    for d in ranked:
        if len(out) >= max_docs:
            rest.append(d)
            continue
        group = LISTING_GROUP.get(d.get("source") or "")
        if group:
            if counts.get(group, 0) >= GROUP_CAPS[group]:
                rest.append(d)
                continue
            counts[group] = counts.get(group, 0) + 1
        out.append(d)

    if angle_min and rest:
        _balance_angles(out, rest, counts, max_docs, angle_min)
    return out
