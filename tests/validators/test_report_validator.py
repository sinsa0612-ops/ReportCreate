"""보고서 검증 루프 테스트 — Flash JSON 파싱 + 재수집 병합 검증.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.validators.test_report_validator -v

모든 테스트는 mock 기반 — 실제 agy/네트워크/claude 를 호출하지 않는다.
RAW_DIR 모듈 상수를 임시 디렉터리로 교체해 파일시스템을 격리한다.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.models import Document
from src.validators import report_validator as rv


# ─── _extract_json: robust JSON 추출 ───────────────────────────────────────

class ExtractJsonTest(unittest.TestCase):

    def test_plain_json(self):
        self.assertEqual(rv._extract_json('{"a": 1}'), {"a": 1})

    def test_with_surrounding_whitespace(self):
        self.assertEqual(rv._extract_json('\n  {"a": 1}\n '), {"a": 1})

    def test_code_fence(self):
        text = '```json\n{"a": 1, "b": [2, 3]}\n```'
        self.assertEqual(rv._extract_json(text), {"a": 1, "b": [2, 3]})

    def test_prose_around_object(self):
        text = '다음은 결과입니다:\n{"a": 1}\n이상입니다.'
        self.assertEqual(rv._extract_json(text), {"a": 1})

    def test_unparseable_returns_none(self):
        self.assertIsNone(rv._extract_json("not json at all"))


# ─── parse_query_plan: 트리거 판단 + 쿼리 평탄화 ─────────────────────────────

class ParseQueryPlanTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload) -> Path:
        p = self.dir / "plan.json"
        if isinstance(payload, str):
            p.write_text(payload, encoding="utf-8")
        else:
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_trigger_with_queries_flattened_and_deduped(self):
        path = self._write({
            "should_trigger": True,
            "verdict": "NEEDS_RECOLLECTION",
            "query_plan": [
                {"rationale": "공백A", "priority": "high",
                 "queries": ["쿼리1", "query a", "query b"]},
                {"rationale": "공백B", "priority": "medium",
                 "queries": ["쿼리2", "query a"]},  # query a 중복
            ],
        })
        should, queries = rv.parse_query_plan(path)
        self.assertTrue(should)
        self.assertEqual(queries, ["쿼리1", "query a", "query b", "쿼리2"])

    def test_should_trigger_false(self):
        path = self._write({
            "should_trigger": False, "verdict": "APPROVED", "query_plan": [],
        })
        self.assertEqual(rv.parse_query_plan(path), (False, []))

    def test_verdict_approved_overrides_trigger(self):
        path = self._write({
            "should_trigger": True, "verdict": "APPROVED",
            "query_plan": [{"queries": ["x"]}],
        })
        self.assertEqual(rv.parse_query_plan(path), (False, []))

    def test_trigger_true_but_empty_queries(self):
        path = self._write({
            "should_trigger": True, "verdict": "NEEDS_RECOLLECTION",
            "query_plan": [{"rationale": "x", "queries": []}],
        })
        self.assertEqual(rv.parse_query_plan(path), (False, []))

    def test_unparseable_json_returns_false(self):
        path = self._write("그냥 텍스트, JSON 아님")
        self.assertEqual(rv.parse_query_plan(path), (False, []))

    def test_code_fenced_json_parses(self):
        path = self._write(
            '```json\n{"should_trigger": true, "verdict": "NEEDS_RECOLLECTION",'
            ' "query_plan": [{"queries": ["q1", "q2"]}]}\n```')
        should, queries = rv.parse_query_plan(path)
        self.assertTrue(should)
        self.assertEqual(queries, ["q1", "q2"])


# ─── _as_chapter_id: 챕터 번호 정규화 ───────────────────────────────────────

class AsChapterIdTest(unittest.TestCase):

    def test_valid_ints(self):
        self.assertEqual(rv._as_chapter_id(1), 1)
        self.assertEqual(rv._as_chapter_id(7), 7)

    def test_string_coerced(self):
        self.assertEqual(rv._as_chapter_id("3"), 3)

    def test_out_of_range_none(self):
        self.assertIsNone(rv._as_chapter_id(0))
        self.assertIsNone(rv._as_chapter_id(8))

    def test_non_numeric_none(self):
        self.assertIsNone(rv._as_chapter_id("x"))
        self.assertIsNone(rv._as_chapter_id(None))


# ─── parse_plan: 영향 챕터 식별 (부분 재생성) ────────────────────────────────

class ParsePlanTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload) -> Path:
        p = self.dir / "plan.json"
        p.write_text(
            payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_recollection_with_affected_chapters(self):
        path = self._write({
            "should_trigger": True, "verdict": "NEEDS_RECOLLECTION",
            "query_plan": [
                {"rationale": "공백A", "priority": "high",
                 "queries": ["쿼리1", "q a"], "affected_chapters": [3, 4]},
                {"rationale": "공백B", "priority": "high",
                 "queries": ["쿼리2", "q a"], "affected_chapters": [4]},
            ],
            "revision_plan": [{"chapter": 5, "issue": "논리 비약"}],
        })
        res = rv.parse_plan(path)
        self.assertTrue(res.should_recollect)
        self.assertEqual(res.queries, ["쿼리1", "q a", "쿼리2"])     # 중복 제거
        self.assertEqual(set(res.recollect_chapters), {3, 4})
        self.assertIn("공백A", res.recollect_chapters[3])
        self.assertEqual(res.revision_chapters, {5: "논리 비약"})

    def test_approved_returns_empty(self):
        path = self._write({
            "should_trigger": True, "verdict": "APPROVED",
            "query_plan": [{"queries": ["x"], "affected_chapters": [1]}],
            "revision_plan": [{"chapter": 2, "issue": "y"}],
        })
        res = rv.parse_plan(path)
        self.assertFalse(res.should_recollect)
        self.assertEqual(res.queries, [])
        self.assertEqual(res.recollect_chapters, {})
        self.assertEqual(res.revision_chapters, {})

    def test_revision_only_no_recollection(self):
        path = self._write({
            "should_trigger": False, "verdict": "NEEDS_REVISION",
            "query_plan": [],
            "revision_plan": [{"chapter": 2, "issue": "설명 부족"},
                              {"chapter": 6, "issue": "구조 혼란"}],
        })
        res = rv.parse_plan(path)
        self.assertFalse(res.should_recollect)
        self.assertEqual(res.queries, [])
        self.assertEqual(res.recollect_chapters, {})
        self.assertEqual(res.revision_chapters, {2: "설명 부족", 6: "구조 혼란"})

    def test_out_of_range_chapters_ignored(self):
        path = self._write({
            "should_trigger": True, "verdict": "NEEDS_RECOLLECTION",
            "query_plan": [{"rationale": "r", "queries": ["q1", "q2"],
                            "affected_chapters": [0, 9, 3]}],
            "revision_plan": [{"chapter": 99, "issue": "z"}],
        })
        res = rv.parse_plan(path)
        self.assertEqual(set(res.recollect_chapters), {3})       # 0, 9 무시
        self.assertEqual(res.revision_chapters, {})              # 99 무시

    def test_unparseable_returns_empty(self):
        path = self._write("JSON 아님")
        res = rv.parse_plan(path)
        self.assertFalse(res.should_recollect)
        self.assertEqual(res.recollect_chapters, {})
        self.assertEqual(res.revision_chapters, {})

    def test_trigger_true_but_no_queries_not_recollect(self):
        path = self._write({
            "should_trigger": True, "verdict": "NEEDS_RECOLLECTION",
            "query_plan": [{"rationale": "r", "queries": [],
                            "affected_chapters": [3]}],
        })
        res = rv.parse_plan(path)
        self.assertFalse(res.should_recollect)       # 쿼리 없으면 재수집 아님
        self.assertEqual(res.queries, [])

    def test_query_groups_preserved(self):
        """공백(그룹) 단위 원본이 query_groups 로 보존된다 — 풀 승격용(19차)."""
        path = self._write({
            "should_trigger": True, "verdict": "NEEDS_RECOLLECTION",
            "query_plan": [
                {"rationale": "LCOE 공백", "queries": ["q1", "q2"],
                 "affected_chapters": [4]},
                {"rationale": "정책 공백", "queries": ["q3"],
                 "affected_chapters": [6, 99]},      # 99 는 무시
                {"rationale": "빈 그룹", "queries": []},  # 그룹에서 제외
            ],
        })
        res = rv.parse_plan(path)
        self.assertEqual(len(res.query_groups), 2)
        self.assertEqual(res.query_groups[0]["queries"], ["q1", "q2"])
        self.assertEqual(res.query_groups[0]["affected_chapters"], [4])
        self.assertEqual(res.query_groups[0]["rationale"], "LCOE 공백")
        self.assertEqual(res.query_groups[1]["affected_chapters"], [6])


# ─── plan_queries: 기존 수집 문서 목록 주입 ──────────────────────────────────

class PlanQueriesExistingDocsTest(unittest.TestCase):
    """plan_queries()가 기존 수집 문서 목록을 Flash 프롬프트에 주입하는지 검증."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.review_dir = Path(self._tmp.name)
        self._patcher_review = patch.object(rv, "REVIEW_DIR", self.review_dir)
        self._patcher_review.start()

    def tearDown(self):
        self._patcher_review.stop()
        self._tmp.cleanup()

    def _raw_payload(self, titles):
        return {
            "documents": [
                {"title": t, "url": f"http://x/{i}", "trust_grade": "A"}
                for i, t in enumerate(titles)
            ]
        }

    def test_existing_docs_injected_into_prompt(self):
        """find_raw 가 성공하면 프롬프트에 문서 제목 목록이 포함된다."""
        raw = Path(self._tmp.name) / "slug_2026-06-09.json"
        raw.write_text(
            json.dumps(self._raw_payload(["수소 논문", "SOFC review"])),
            encoding="utf-8",
        )
        review = Path(self._tmp.name) / "slug-review-v1.md"
        review.write_text("리뷰 내용", encoding="utf-8")

        captured_prompts = []

        def fake_run_agy(model, prompt, out_path):
            captured_prompts.append(prompt)
            out_path.write_text("{}", encoding="utf-8")
            return out_path

        with patch.object(rv, "find_raw", return_value=raw), \
             patch.object(rv, "_run_agy", side_effect=fake_run_agy):
            rv.plan_queries("slug", review, iteration=1)

        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("수소 논문", captured_prompts[0])
        self.assertIn("SOFC review", captured_prompts[0])

    def test_find_raw_failure_falls_back_gracefully(self):
        """find_raw 가 실패하면 '목록 로드 실패' 폴백으로 프롬프트를 생성한다."""
        review = Path(self._tmp.name) / "slug-review-v1.md"
        review.write_text("리뷰 내용", encoding="utf-8")

        captured_prompts = []

        def fake_run_agy(model, prompt, out_path):
            captured_prompts.append(prompt)
            out_path.write_text("{}", encoding="utf-8")
            return out_path

        with patch.object(rv, "find_raw", side_effect=FileNotFoundError("없음")), \
             patch.object(rv, "_run_agy", side_effect=fake_run_agy):
            rv.plan_queries("slug", review, iteration=1)

        self.assertEqual(len(captured_prompts), 1)
        self.assertIn("목록 로드 실패", captured_prompts[0])


# ─── promote_from_pool: 탈락 풀 승격 (19차) ─────────────────────────────────

class PromoteFromPoolTest(unittest.TestCase):
    """공백 쿼리로 탈락 풀을 키워드 검색해 승격하는 로직 검증."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.raw_dir = Path(self._tmp.name)
        self._p_raw = patch.object(rv, "RAW_DIR", self.raw_dir)
        self._p_raw.start()
        self._p_fetch = patch.object(rv, "fetch_sources")
        self.mock_fetch = self._p_fetch.start()

    def tearDown(self):
        self._p_fetch.stop()
        self._p_raw.stop()
        self._tmp.cleanup()

    def _write_raw(self, docs: list[dict]) -> Path:
        p = self.raw_dir / "demo_2026-06-10.json"
        p.write_text(json.dumps({"queries": ["수소"], "documents": docs},
                                ensure_ascii=False), encoding="utf-8")
        return p

    @staticmethod
    def _pool_doc(url: str, content: str, grade: str = "B",
                  published: str | None = None) -> dict:
        return {"title": url, "url": url, "content": content,
                "source": "tavily", "trust_grade": grade,
                "trust_score": 55.0, "published": published}

    def _plan(self, queries, chapters=(4,)):
        return rv.PlanResult(
            True, list(queries), {c: "공백" for c in chapters}, {},
            query_groups=[{"queries": list(queries),
                           "affected_chapters": list(chapters),
                           "rationale": "테스트 공백"}])

    def test_promotes_matching_pool_docs(self):
        """공백 용어 2개 이상 일치하는 탈락 문서가 승격·기록·fetch 된다."""
        docs = [
            self._pool_doc("http://p/lcoe", "수전해 LCOE 분석과 levelized 비용"),
            self._pool_doc("http://p/etc", "전혀 무관한 내용의 문서"),
        ]
        raw = self._write_raw(docs)
        plan = self._plan(["수전해 LCOE levelized cost"])
        with patch.object(rv, "find_raw", return_value=raw), \
             patch.object(rv, "_selected_docs", return_value=([], [], raw)):
            n, remaining = rv.promote_from_pool("demo", plan, iteration=1)

        self.assertEqual(n, 1)
        # 승격본 파일명은 '실행일' 날짜라 테스트 작성일에 고정하지 않는다
        v1_files = list(self.raw_dir.glob("demo_*_v1.json"))
        self.assertEqual(len(v1_files), 1)
        merged = json.loads(v1_files[0].read_text(encoding="utf-8"))
        self.assertEqual(merged["promoted_urls"], ["http://p/lcoe"])
        self.assertEqual(merged["last_promoted_urls"], ["http://p/lcoe"])
        _args, kwargs = self.mock_fetch.call_args
        self.assertEqual(kwargs["only_urls"], {"http://p/lcoe"})
        # B급(고등급 아님) → 웹 재수집 병행
        self.assertEqual(remaining, ["수전해 LCOE levelized cost"])

    def test_strong_promotion_skips_web_queries(self):
        """고등급+최신 자료로 충족된 공백은 웹 재수집을 생략한다."""
        docs = [self._pool_doc("http://p/iea", "수전해 LCOE levelized 전망",
                               grade="S", published="2026-01-15")]
        raw = self._write_raw(docs)
        plan = self._plan(["수전해 LCOE levelized"])
        with patch.object(rv, "find_raw", return_value=raw), \
             patch.object(rv, "_selected_docs", return_value=([], [], raw)):
            n, remaining = rv.promote_from_pool("demo", plan, iteration=1)
        self.assertEqual(n, 1)
        self.assertEqual(remaining, [])

    def test_no_match_returns_original_queries(self):
        """풀에 맞는 자료가 없으면 승격 0건 + 전체 쿼리 반환(파일 미생성)."""
        raw = self._write_raw([self._pool_doc("http://p/x", "무관한 내용")])
        plan = self._plan(["수전해 LCOE levelized"])
        with patch.object(rv, "find_raw", return_value=raw), \
             patch.object(rv, "_selected_docs", return_value=([], [], raw)):
            n, remaining = rv.promote_from_pool("demo", plan, iteration=1)
        self.assertEqual(n, 0)
        self.assertEqual(remaining, plan.queries)
        self.assertFalse(list(self.raw_dir.glob("demo_*_v1.json")))
        self.mock_fetch.assert_not_called()

    def test_promotion_capped_per_gap(self):
        """공백 하나당 PROMOTE_PER_GAP 건까지만 승격한다."""
        docs = [self._pool_doc(f"http://p/{i}", "수전해 LCOE levelized 비용")
                for i in range(rv.PROMOTE_PER_GAP + 5)]
        raw = self._write_raw(docs)
        plan = self._plan(["수전해 LCOE levelized"])
        with patch.object(rv, "find_raw", return_value=raw), \
             patch.object(rv, "_selected_docs", return_value=([], [], raw)):
            n, _ = rv.promote_from_pool("demo", plan, iteration=1)
        self.assertEqual(n, rv.PROMOTE_PER_GAP)

    def test_used_docs_not_promoted(self):
        """이미 투입된 문서는 승격 대상이 아니다."""
        used_doc = self._pool_doc("http://p/used", "수전해 LCOE levelized 비용")
        raw = self._write_raw([used_doc])
        plan = self._plan(["수전해 LCOE levelized"])
        with patch.object(rv, "find_raw", return_value=raw), \
             patch.object(rv, "_selected_docs",
                          return_value=([used_doc], [], raw)):
            n, remaining = rv.promote_from_pool("demo", plan, iteration=1)
        self.assertEqual(n, 0)
        self.assertEqual(remaining, plan.queries)


# ─── _existing_docs_block: 투입/미투입 라벨 (19차) ──────────────────────────

class ExistingDocsBlockTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.raw = Path(self._tmp.name) / "demo_2026-06-10.json"
        docs = [
            {"title": "투입 논문", "url": "http://u/1", "trust_grade": "S",
             "content": "x", "trust_score": 90.0},
            {"title": "탈락 기사", "url": "http://u/2", "trust_grade": "B",
             "content": "x", "trust_score": 55.0},
        ]
        self.raw.write_text(json.dumps({"documents": docs}, ensure_ascii=False),
                            encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_labels_used_and_pool(self):
        used = [{"title": "투입 논문", "url": "http://u/1"}]
        with patch.object(rv, "find_raw", return_value=self.raw), \
             patch.object(rv, "_selected_docs",
                          return_value=(used, [], self.raw)):
            block = rv._existing_docs_block("demo")
        self.assertIn("[투입|S] 투입 논문", block)
        self.assertIn("[미투입|B] 탈락 기사", block)

    def test_selection_failure_labels_all_pool(self):
        """투입 판별 실패 시 전체를 [미투입]으로 폴백(목록은 유지)."""
        with patch.object(rv, "find_raw", return_value=self.raw), \
             patch.object(rv, "_selected_docs",
                          side_effect=FileNotFoundError("x")):
            block = rv._existing_docs_block("demo")
        self.assertIn("[미투입|S] 투입 논문", block)
        self.assertIn("[미투입|B] 탈락 기사", block)


# ─── collect_and_merge: 중복 제거 병합 ──────────────────────────────────────

def _doc(url: str, title: str = "t") -> Document:
    return Document(title=title, url=url, content="c",
                    source="tavily", source_type="web", trust_grade="A")


class CollectAndMergeTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.raw = Path(self._tmp.name)
        self._patcher = patch.object(rv, "RAW_DIR", self.raw)
        self._patcher.start()
        # fetch_sources 는 네트워크를 타므로 mock (호출 인자만 검증)
        self._fetch_patcher = patch.object(rv, "fetch_sources")
        self.mock_fetch = self._fetch_patcher.start()
        # 기존 raw JSON 한 건 작성 (url1 보유)
        self.original = self.raw / "demo_2026-06-08.json"
        self.original.write_text(json.dumps({
            "queries": ["원본 쿼리"],
            "count": 1,
            "documents": [{"title": "기존", "url": "http://x/1",
                           "content": "c", "source": "tavily",
                           "source_type": "web", "trust_grade": "A"}],
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self._fetch_patcher.stop()
        self._patcher.stop()
        self._tmp.cleanup()

    def test_merge_dedupes_existing_url(self):
        import datetime
        # url1(중복) + url2(신규) 수집 → url2 만 추가
        new_docs = [_doc("http://x/1"), _doc("http://x/2")]
        with patch.object(rv, "collect", return_value=new_docs), \
             patch.object(rv, "find_raw", return_value=self.original):
            out, added = rv.collect_and_merge("demo", ["q"], iteration=1)

        self.assertEqual(added, 1)
        self.assertEqual(out.name, f"demo_{datetime.date.today().isoformat()}_v1.json")
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 2)
        urls = {d["url"] for d in payload["documents"]}
        self.assertEqual(urls, {"http://x/1", "http://x/2"})
        self.assertEqual(payload["validation_iteration"], 1)
        self.assertEqual(payload["gap_queries"], ["q"])

    def test_merge_calls_fetch_on_base_sources_incrementally(self):
        # 신규 추가 시 base _sources 폴더에 incremental fetch 호출
        new_docs = [_doc("http://x/2")]
        with patch.object(rv, "collect", return_value=new_docs), \
             patch.object(rv, "find_raw", return_value=self.original):
            out, added = rv.collect_and_merge("demo", ["q"], iteration=1)

        self.mock_fetch.assert_called_once()
        args, kwargs = self.mock_fetch.call_args
        self.assertEqual(args[0], out)  # _v1.json 전체를 넘김
        self.assertTrue(kwargs["incremental"])
        # base _sources = 원본 stem(_v 없음) 기반
        self.assertEqual(Path(kwargs["out_dir"]).name, "demo_2026-06-08_sources")

    def test_added_urls_recorded_and_accumulated(self):
        """last_added_urls(이번 회차) + validation_added_urls(누적) 기록 검증.

        validation_added_urls 는 _selected_docs 가 신규 문서를 기본 선별과
        분리해 항상 투입하는 근거다(평가 보고서 C1).
        """
        with patch.object(rv, "collect", return_value=[_doc("http://x/2")]), \
             patch.object(rv, "find_raw", return_value=self.original):
            out1, _ = rv.collect_and_merge("demo", ["q"], iteration=1)
        p1 = json.loads(out1.read_text(encoding="utf-8"))
        self.assertEqual(p1["last_added_urls"], ["http://x/2"])
        self.assertEqual(p1["validation_added_urls"], ["http://x/2"])

        # 2회차: 기존 누적분 위에 신규 URL 이 더해진다
        with patch.object(rv, "collect", return_value=[_doc("http://x/3")]), \
             patch.object(rv, "find_raw", return_value=out1):
            out2, _ = rv.collect_and_merge("demo", ["q2"], iteration=2)
        p2 = json.loads(out2.read_text(encoding="utf-8"))
        self.assertEqual(p2["last_added_urls"], ["http://x/3"])
        self.assertEqual(p2["validation_added_urls"],
                         ["http://x/2", "http://x/3"])

    def test_fetch_receives_only_added_urls(self):
        """incremental fetch 가 이번 회차 신규 URL 만 대상으로 받는다(H3)."""
        with patch.object(rv, "collect", return_value=[_doc("http://x/2")]), \
             patch.object(rv, "find_raw", return_value=self.original):
            rv.collect_and_merge("demo", ["q"], iteration=1)
        _args, kwargs = self.mock_fetch.call_args
        self.assertEqual(kwargs["only_urls"], {"http://x/2"})

    def test_no_new_docs_returns_original_with_zero(self):
        with patch.object(rv, "collect", return_value=[]), \
             patch.object(rv, "find_raw", return_value=self.original):
            out, added = rv.collect_and_merge("demo", ["q"], iteration=1)
        self.assertEqual(out, self.original)
        self.assertEqual(added, 0)
        self.mock_fetch.assert_not_called()  # 신규 0건이면 fetch 안 함

    def test_all_duplicates_no_new_file_added_zero(self):
        # 신규 수집했으나 전부 기존 url → 새 파일 만들지 않고 (원본, 0) 반환
        with patch.object(rv, "collect", return_value=[_doc("http://x/1")]), \
             patch.object(rv, "find_raw", return_value=self.original):
            out, added = rv.collect_and_merge("demo", ["q"], iteration=2)
        self.assertEqual(out, self.original)
        self.assertEqual(added, 0)
        self.mock_fetch.assert_not_called()
        # _v2.json 파일이 생성되지 않았는지 확인
        self.assertFalse((self.raw / "demo_2026-06-08_v2.json").exists())


# ─── _hallucination_guard: 환각 수치 결정적 가드 (21차 문제1) ─────────────────

class HallucinationGuardTest(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.drafts = Path(self._tmp.name)
        self._p = patch.object(rv, "DRAFTS_DIR", self.drafts)
        self._p.start()
        d = self.drafts / "demo"
        d.mkdir(parents=True)
        (d / "ch04_정량비교분석.md").write_text(
            "## 4. 정량 비교\nCAPEX EUR 244.5M [AA | MDPI 2025]", encoding="utf-8")
        (d / "ch07_시사점및권고안.md").write_text(
            "## 7. 시사점\n약 244.5M 투자 필요", encoding="utf-8")
        (d / "ch01_기술개요.md").write_text(
            "## 1. 개요\n환각 수치 없음", encoding="utf-8")

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_flagged_chapters_get_notes_including_memo_only(self):
        """미확인 수치를 인용한 챕터(메모 전용 Ch7 포함)에 제거 노트가 강제 추가된다."""
        findings = [{"chapter": "4", "ctx": "CAPEX EUR 244.5M [AA | MDPI 2025]",
                     "missing": ["244.5"]}]
        with patch("src.validators.citation_audit.audit_findings",
                   return_value=(10, findings)):
            notes, tokens = rv._hallucination_guard("demo")
        self.assertEqual(tokens, {"244.5"})
        self.assertIn(4, notes)
        self.assertIn(7, notes)            # 드래프트 grep 으로 Ch7 도 포착
        self.assertNotIn(1, notes)
        self.assertIn("환각 의심 수치 제거", notes[4])

    def test_clean_audit_returns_empty(self):
        with patch("src.validators.citation_audit.audit_findings",
                   return_value=(10, [])):
            notes, tokens = rv._hallucination_guard("demo")
        self.assertEqual(notes, {})
        self.assertEqual(tokens, set())

    def test_audit_failure_is_swallowed(self):
        """감사 실패는 가드를 생략할 뿐 루프를 중단시키지 않는다."""
        with patch("src.validators.citation_audit.audit_findings",
                   side_effect=FileNotFoundError("no report")):
            notes, tokens = rv._hallucination_guard("demo")
        self.assertEqual(notes, {})
        self.assertEqual(tokens, set())


if __name__ == "__main__":
    unittest.main()
