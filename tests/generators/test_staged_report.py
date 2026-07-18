"""단계적 보고서 생성 테스트 — claude 호출은 전부 mock.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.generators.test_staged_report -v
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.generators import staged_report as sr


def _docs():
    return [
        {"title": "IEA 전망", "url": "library://iea/x.pdf", "content": "수소 시장 100GW",
         "source": "library", "trust_grade": "S"},
        {"title": "전해조 효율 논문", "url": "http://a/1", "content": "효율 74%",
         "source": "exa", "trust_grade": "A"},
        {"title": "위키 연료전지", "url": "http://w/1", "content": "개요",
         "source": "tavily", "trust_grade": "C"},
    ]


class HelpersTest(unittest.TestCase):
    def test_split_chapter_with_memo(self):
        raw = "## 1. 기술 개요\n본문입니다.\n<<<MEMO>>>\n핵심 주장: X"
        body, memo = sr._split_chapter(raw)
        self.assertEqual(body, "## 1. 기술 개요\n본문입니다.")
        self.assertEqual(memo, "핵심 주장: X")

    def test_split_chapter_without_memo(self):
        raw = "## 1. 기술 개요\n본문만 있음"
        body, memo = sr._split_chapter(raw)
        self.assertEqual(body, raw.strip())
        self.assertEqual(memo, "")

    def test_strip_fences(self):
        self.assertEqual(sr._strip_fences('```json\n{"a":1}\n```'), '{"a":1}')
        self.assertEqual(sr._strip_fences('{"a":1}'), '{"a":1}')

    def test_chapter_prompt_has_memo_length_limit(self):
        self.assertIn("700자 이내", sr.CHAPTER_PROMPT)

    def test_outline_prompt_has_overlap_limit_rule(self):
        self.assertIn("3개 이상의 챕터에 중복 배분하지", sr.OUTLINE_PROMPT)

    def test_outline_prompt_has_template_placeholders(self):
        """양식화(템플릿) 이후: 챕터 수·양식별 규칙·골격이 자리표시자로 주입된다."""
        self.assertIn("{n_chapters}", sr.OUTLINE_PROMPT)
        self.assertIn("{extra_rules}", sr.OUTLINE_PROMPT)
        self.assertIn("{skeleton}", sr.OUTLINE_PROMPT)

    def test_default_template_synthesis_rules(self):
        """18차 규칙(Ch7 자료 배분 금지 + Ch4 정량 우선)은 기본 양식의 배분 규칙에 존재."""
        from src.report_templates import DEFAULT_TEMPLATE
        rules = DEFAULT_TEMPLATE.outline_extra_rules
        self.assertIn("챕터 7(시사점)에는 자료를 배분하지", rules)
        self.assertIn("챕터 4(정량 비교)", rules)

    def test_chapter_prompt_has_gap_and_stub_rules(self):
        """18차: '데이터 없음' 기반 결론 금지 + 특허·통계 스텁 동향 집계 한정."""
        self.assertIn("결론의 근거로 사용하지 않는다", sr.CHAPTER_PROMPT)
        self.assertIn("동향 집계", sr.CHAPTER_PROMPT)

    def test_valid_chapter_header_check(self):
        self.assertTrue(sr._valid_chapter("## 4. 정량 비교 분석\n본문", 4))
        self.assertTrue(sr._valid_chapter("  ## 4. 제목", 4))
        self.assertFalse(sr._valid_chapter("요 합계가 없습니다.\n본문", 4))
        self.assertFalse(sr._valid_chapter("## 5. 다른 챕터", 4))

    def test_doc_listing_includes_quant_preview(self):
        """blocks 가 주어지면 정량 수치 문장을 '수치:' 미리보기로 덧붙인다."""
        blocks = [
            "이 전해조의 효율은 74.4 % 수준으로 보고되었습니다. 나머지 설명.",
            "수치가 전혀 없는 일반 설명 블록입니다.",
            "짧음",
        ]
        out = sr._doc_listing(_docs(), blocks)
        self.assertIn("수치: 이 전해조의 효율은 74.4 %", out)

    def test_doc_listing_without_blocks_unchanged(self):
        out = sr._doc_listing(_docs())
        self.assertNotIn("수치:", out)
        self.assertIn("IEA 전망", out)

    def test_format_memos_empty(self):
        self.assertIn("첫 챕터", sr._format_memos([]))

    def test_format_memos_joined(self):
        out = sr._format_memos(["메모1", "메모2"])
        self.assertIn("메모1", out)
        self.assertIn("메모2", out)

    def test_build_references_sorted_by_grade(self):
        refs = sr._build_references(_docs())
        s_pos = refs.index("[S]")
        a_pos = refs.index("[A]")
        c_pos = refs.index("[C]")
        self.assertLess(s_pos, a_pos)
        self.assertLess(a_pos, c_pos)


class Stage1Test(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.drafts = Path(self._tmp.name) / "drafts"
        self._p = patch.object(sr, "DRAFTS_DIR", self.drafts)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_outline_written(self):
        fake_json = json.dumps({"chapters": [
            {"id": 1, "key_points": ["TRL 6"], "relevant_doc_indices": [1, 2]}
        ]})
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", return_value=fake_json):
            json_path = sr.stage1_outline("test-slug")

        self.assertTrue(json_path.exists())
        outline = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(len(outline["chapters"]), 7)        # 골격 7개 유지
        self.assertEqual(outline["chapters"][0]["relevant_doc_indices"], [1, 2])
        self.assertTrue((self.drafts / "test-slug" / "outline.md").exists())


class Stage2Test(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.drafts = Path(self._tmp.name) / "drafts"
        self._p = patch.object(sr, "DRAFTS_DIR", self.drafts)
        self._p.start()
        # 최소 골격의 outline.json 준비 (챕터 2개만)
        d = self.drafts / "test-slug"
        d.mkdir(parents=True)
        outline = {"slug": "test-slug", "queries": ["수소"], "chapters": [
            {"id": 1, "title": "기술 개요", "key_question": "q1",
             "must_include": "TRL", "key_points": ["a"], "relevant_doc_indices": [1]},
            {"id": 2, "title": "시장 맥락", "key_question": "q2",
             "must_include": "CAGR", "key_points": ["b"], "relevant_doc_indices": [2]},
        ]}
        (d / "outline.json").write_text(json.dumps(outline, ensure_ascii=False),
                                        encoding="utf-8")

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_chapters_written_body_only(self):
        outputs = iter([
            "## 1. 기술 개요\n본문1\n<<<MEMO>>>\n핵심 주장: A",
            "## 2. 시장 맥락\n본문2\n<<<MEMO>>>\n핵심 주장: B",
        ])
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=lambda *a, **k: next(outputs)):
            paths = sr.stage2_chapters("test-slug")

        self.assertEqual(len(paths), 2)
        ch1 = paths[0].read_text(encoding="utf-8")
        self.assertIn("본문1", ch1)
        self.assertNotIn("<<<MEMO>>>", ch1)   # 메모는 파일에 안 들어감

    def test_second_chapter_receives_first_memo(self):
        outputs = iter([
            "## 1. 기술 개요\n본문1\n<<<MEMO>>>\n핵심 주장: 효율 74%",
            "## 2. 시장 맥락\n본문2\n<<<MEMO>>>\n핵심 주장: B",
        ])
        seen_prompts = []

        def capture(prompt, model):
            seen_prompts.append(prompt)
            return next(outputs)

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=capture):
            sr.stage2_chapters("test-slug")

        # 2번째 챕터 프롬프트에 1번 챕터 메모가 주입됐는지
        self.assertIn("효율 74%", seen_prompts[1])

    def test_chapter_models_selective_opus(self):
        """Ch1 → Opus, Ch2 → Sonnet 모델 선택 검증."""
        outputs = iter([
            "## 1. 기술 개요\n본문1\n<<<MEMO>>>\n핵심 주장: A",
            "## 2. 시장 맥락\n본문2\n<<<MEMO>>>\n핵심 주장: B",
        ])
        seen_models = []

        def capture(prompt, model):
            seen_models.append(model)
            return next(outputs)

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=capture):
            sr.stage2_chapters("test-slug")

        self.assertEqual(seen_models[0], sr.OPUS_MODEL)    # Ch1 → Opus
        self.assertEqual(seen_models[1], sr.STRUCT_MODEL)  # Ch2 → Sonnet

    def test_missing_outline_raises(self):
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1"], ["수소"])):
            with self.assertRaises(FileNotFoundError):
                sr.stage2_chapters("no-such-slug")

    def test_chapters_persist_memo_to_disk(self):
        """각 챕터 작성 후 ch{n}_memo.txt 가 디스크에 저장된다(부분 재생성 선결 과제)."""
        outputs = iter([
            "## 1. 기술 개요\n본문1\n<<<MEMO>>>\n핵심 주장: A",
            "## 2. 시장 맥락\n본문2\n<<<MEMO>>>\n핵심 주장: B",
        ])
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=lambda *a, **k: next(outputs)):
            sr.stage2_chapters("test-slug")

        d = self.drafts / "test-slug"
        self.assertEqual((d / "ch01_memo.txt").read_text(encoding="utf-8"),
                         "핵심 주장: A")
        self.assertEqual((d / "ch02_memo.txt").read_text(encoding="utf-8"),
                         "핵심 주장: B")


class ChapterOutputValidationTest(unittest.TestCase):
    """18차 A1 — 챕터 출력 앞부분 소실 검출(제목 검사 + 1회 재시도)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.drafts = Path(self._tmp.name) / "drafts"
        self._p = patch.object(sr, "DRAFTS_DIR", self.drafts)
        self._p.start()
        d = self.drafts / "test-slug"
        d.mkdir(parents=True)
        outline = {"slug": "test-slug", "queries": ["수소"], "chapters": [
            {"id": 1, "title": "기술 개요", "key_question": "q1",
             "must_include": "TRL", "key_points": ["a"], "relevant_doc_indices": [1]},
        ]}
        (d / "outline.json").write_text(json.dumps(outline, ensure_ascii=False),
                                        encoding="utf-8")

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_malformed_output_retried_once(self):
        """제목 없는 출력(앞부분 소실)이면 같은 프롬프트로 1회 재시도한다."""
        outputs = iter([
            "요 합계가 없습니다. 문장 중간부터 시작하는 잘린 출력",
            "## 1. 기술 개요\n복구된 본문\n<<<MEMO>>>\n핵심: A",
        ])
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=lambda *a, **k: next(outputs)):
            paths = sr.stage2_chapters("test-slug")

        text = paths[0].read_text(encoding="utf-8")
        self.assertIn("복구된 본문", text)
        self.assertNotIn("잘린 출력", text)

    def test_double_failure_prepends_header(self):
        """재시도도 실패하면 제목을 보정해 저장한다(파이프라인 중단 없이 경고)."""
        outputs = iter([
            "잘린 출력 첫 번째 시도입니다",
            "잘린 출력 두 번째 시도입니다",
        ])
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=lambda *a, **k: next(outputs)):
            paths = sr.stage2_chapters("test-slug")

        text = paths[0].read_text(encoding="utf-8")
        self.assertTrue(text.startswith("## 1. 기술 개요"))
        self.assertIn("잘린 출력 두 번째 시도입니다", text)


class SynthesisChapterTest(unittest.TestCase):
    """18차 A2/B2 — Ch4(이전 본문 주입 종합)·Ch7(메모만, 자료 미투입)."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.drafts = Path(self._tmp.name) / "drafts"
        self._p = patch.object(sr, "DRAFTS_DIR", self.drafts)
        self._p.start()
        self.d = self.drafts / "test-slug"
        self.d.mkdir(parents=True)
        outline = {"slug": "test-slug", "queries": ["수소"], "chapters": [
            {"id": 1, "title": "기술 개요", "key_question": "q1",
             "must_include": "TRL", "key_points": [], "relevant_doc_indices": [1]},
            {"id": 4, "title": "정량 비교 분석", "key_question": "q4",
             "must_include": "비교표", "key_points": [], "relevant_doc_indices": [2]},
            {"id": 7, "title": "시사점 및 권고안", "key_question": "q7",
             "must_include": "권고", "key_points": [], "relevant_doc_indices": [3]},
        ]}
        (self.d / "outline.json").write_text(
            json.dumps(outline, ensure_ascii=False), encoding="utf-8")
        # 챕터 1은 이미 작성된 상태(스킵) — Ch4 가 이 본문을 종합 대상으로 받는다
        (self.d / "ch01_기술개요.md").write_text(
            "## 1. 기술 개요\n효율 74.4% 인용 본문", encoding="utf-8")
        (self.d / "ch01_memo.txt").write_text("핵심 주장: 효율 74.4%", encoding="utf-8")

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _run(self):
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            cid = 4 if len(seen) == 1 else 7
            return f"## {cid}. 챕터\n본문\n<<<MEMO>>>\n핵심: nm"

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage2_chapters("test-slug")
        return seen

    def test_ch4_receives_previous_chapter_bodies(self):
        """Ch4 프롬프트에 이전 챕터 수치 다이제스트(종합 대상)와 배분 자료가 함께 들어간다."""
        seen = self._run()
        self.assertIn("이전 챕터 수치 다이제스트", seen[0])
        self.assertIn("효율 74.4% 인용 본문", seen[0])   # ch1 본문 주입
        self.assertIn("b2", seen[0])                      # 배분 자료도 유지

    def test_ch7_memo_only_no_docs(self):
        """Ch7 은 outline 이 자료를 배분했어도 자료 블록 없이 메모만 받는다."""
        seen = self._run()
        ch7_prompt = seen[1]
        self.assertNotIn("b3", ch7_prompt)               # 배분 자료 미투입
        self.assertIn("배분된 자료 없음", ch7_prompt)
        self.assertIn("효율 74.4%", ch7_prompt)          # ch1 메모는 전달됨


class Stage2PatchTest(unittest.TestCase):
    """stage2_patch — 영향 챕터만 재작성, 나머지 보존."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.drafts = Path(self._tmp.name) / "drafts"
        self._p = patch.object(sr, "DRAFTS_DIR", self.drafts)
        self._p.start()
        self.d = self.drafts / "test-slug"
        self.d.mkdir(parents=True)
        outline = {"slug": "test-slug", "queries": ["수소"], "chapters": [
            {"id": 1, "title": "기술 개요", "key_question": "q1",
             "must_include": "TRL", "key_points": ["a"], "relevant_doc_indices": [1]},
            {"id": 2, "title": "시장 맥락", "key_question": "q2",
             "must_include": "CAGR", "key_points": ["b"], "relevant_doc_indices": [2]},
        ]}
        (self.d / "outline.json").write_text(
            json.dumps(outline, ensure_ascii=False), encoding="utf-8")
        # 기존 챕터 본문 + 메모 선작성
        (self.d / "ch01_기술개요.md").write_text("## 1. 기술 개요\nOLD1", encoding="utf-8")
        (self.d / "ch02_시장맥락.md").write_text("## 2. 시장 맥락\nOLD2", encoding="utf-8")
        (self.d / "ch01_memo.txt").write_text("M1메모", encoding="utf-8")
        (self.d / "ch02_memo.txt").write_text("M2메모", encoding="utf-8")

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_only_target_chapter_regenerated(self):
        calls = []

        def fake(prompt, model):
            calls.append((prompt, model))
            return "## 1. 기술 개요\nNEW1\n<<<MEMO>>>\n핵심: nm1"

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_added_doc_indices", return_value=[]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage2_patch("test-slug", {1}, notes={1: "공백X"})

        self.assertEqual(len(calls), 1)                       # 챕터 1만 호출
        self.assertIn("NEW1", (self.d / "ch01_기술개요.md").read_text(encoding="utf-8"))
        self.assertIn("OLD2", (self.d / "ch02_시장맥락.md").read_text(encoding="utf-8"))  # 2 보존
        self.assertEqual((self.d / "ch01_memo.txt").read_text(encoding="utf-8"), "핵심: nm1")

    def test_patch_note_injected_into_prompt(self):
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            return "## 1. 기술 개요\nNEW1\n<<<MEMO>>>\n핵심: nm1"

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_added_doc_indices", return_value=[]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage2_patch("test-slug", {1}, notes={1: "근거가 부족함"})

        self.assertIn("개정 지침", seen[0])
        self.assertIn("근거가 부족함", seen[0])

    def test_nontarget_memo_forwarded_to_target(self):
        """비대상 챕터1의 디스크 메모가 대상 챕터2 프롬프트로 전달된다."""
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            return "## 2. 시장 맥락\nNEW2\n<<<MEMO>>>\n핵심: nm2"

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_added_doc_indices", return_value=[]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage2_patch("test-slug", {2}, notes={2: "x"})

        self.assertEqual(len(seen), 1)
        self.assertIn("M1메모", seen[0])   # 챕터1 메모가 챕터2 프롬프트에 포함

    def test_added_docs_injected_into_target(self):
        """재수집 신규 문서 인덱스가 대상 챕터 자료에 추가된다."""
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            return "## 1. 기술 개요\nNEW1\n<<<MEMO>>>\n핵심: nm1"

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_added_doc_indices", return_value=[3]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage2_patch("test-slug", {1}, notes={1: "x"})

        # 챕터1 relevant=[1] + 주입 [3] → 자료 블록 b1, b3
        self.assertIn("b1", seen[0])
        self.assertIn("b3", seen[0])

    def test_added_docs_injected_only_into_primary_target(self):
        """신규 자료는 대상 챕터 중 1곳(관련자료 최다, 동률 시 낮은 id)에만 주입된다."""
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            # 호출 순서대로 챕터 1, 2 — 출력 검증을 통과하도록 올바른 제목 사용
            return f"## {len(seen)}. 챕터\n본문\n<<<MEMO>>>\n핵심: nm"

        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1", "b2", "b3"], ["수소"])), \
             patch.object(sr, "_added_doc_indices", return_value=[3]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage2_patch("test-slug", {1, 2}, notes={1: "x", 2: "y"})

        self.assertEqual(len(seen), 2)
        self.assertIn("b3", seen[0])       # 챕터1(동률 시 낮은 id) = primary → 주입됨
        self.assertNotIn("b3", seen[1])    # 챕터2 = 비주입, 기존 자료(b2)만 유지

    def test_missing_outline_raises(self):
        with patch.object(sr, "_prepare_docs",
                          return_value=(_docs(), ["b1"], ["수소"])):
            with self.assertRaises(FileNotFoundError):
                sr.stage2_patch("no-such-slug", {1})

    def test_run_partial_patches_then_assembles(self):
        with patch.object(sr, "stage2_patch") as m_patch, \
             patch.object(sr, "stage3_assemble",
                          return_value=Path("out.md")) as m_asm:
            out = sr.run_partial("test-slug", {1, 3}, {1: "n"})

        m_patch.assert_called_once_with("test-slug", {1, 3}, {1: "n"},
                                        query_groups=None, excluded_numbers=None)
        m_asm.assert_called_once_with("test-slug")
        self.assertEqual(out, Path("out.md"))


class QuantDigestExcludedTest(unittest.TestCase):
    """_quant_digest 환각 수치 제외 전처리 (21차 문제1 원인②)."""

    BODY = ("## 3. 기술 경로\n"
            "CAPEX는 EUR 244.5M으로 추정됩니다 [AA | MDPI 2025].\n"
            "효율은 74.4%입니다 [A | ScienceDirect].\n"
            "| 항목 | 값 |\n| CAPEX | 244.5M |\n")

    def test_excluded_number_lines_removed(self):
        out = sr._quant_digest(self.BODY, 8000, excluded_numbers={"244.5"})
        self.assertNotIn("244.5", out)
        self.assertIn("74.4%", out)        # 정상 수치는 보존
        self.assertIn("## 3. 기술 경로", out)

    def test_no_exclusion_keeps_all(self):
        out = sr._quant_digest(self.BODY, 8000)
        self.assertIn("244.5", out)

    def test_comma_normalized_match(self):
        body = "## 1. x\n비용 1,400 MW 규모입니다 [B | x].\n"
        out = sr._quant_digest(body, 8000, excluded_numbers={"1400"})
        self.assertNotIn("1,400", out)


class AddedDocChapterMapTest(unittest.TestCase):
    """_added_doc_chapter_map — 신규 자료의 그룹별 챕터 분산 (21차 문제2)."""

    DOCS = [
        {"title": "DOE TRL MRL analysis", "url": "u1",
         "content": "hydrogen storage TRL maturity readiness"},
        {"title": "LCOE market outlook", "url": "u2",
         "content": "hydrogen storage LCOE market CAGR"},
        {"title": "무관 문서", "url": "u3", "content": "전혀 다른 주제"},
    ]
    GROUPS = [
        {"queries": ["hydrogen storage TRL maturity"],
         "affected_chapters": [1], "rationale": "TRL 공백"},
        {"queries": ["hydrogen storage LCOE market CAGR"],
         "affected_chapters": [2, 4], "rationale": "시장 공백"},
    ]

    def test_docs_routed_to_matching_group_chapters(self):
        mapping, leftover = sr._added_doc_chapter_map(
            self.DOCS, [1, 2, 3], self.GROUPS, {1, 2, 4})
        self.assertIn(1, mapping.get(1, []))   # TRL 문서 → Ch1
        # LCOE 문서 → 그룹2의 챕터(2 또는 4)
        self.assertTrue(2 in mapping.get(2, []) or 2 in mapping.get(4, []))
        self.assertEqual(leftover, [3])        # 무매칭 문서는 폴백 대상

    def test_memo_only_and_nontarget_chapters_excluded(self):
        groups = [{"queries": ["hydrogen storage TRL"],
                   "affected_chapters": [7], "rationale": "x"}]
        mapping, leftover = sr._added_doc_chapter_map(
            self.DOCS, [1], groups, {1, 7})
        self.assertEqual(mapping, {})          # Ch7(메모 전용)은 주입 불가
        self.assertEqual(leftover, [1])

    def test_per_chapter_cap_respected(self):
        docs = [{"title": f"hydrogen storage TRL doc {i}", "url": f"u{i}",
                 "content": "hydrogen storage TRL maturity"} for i in range(12)]
        groups = [{"queries": ["hydrogen storage TRL maturity"],
                   "affected_chapters": [1], "rationale": "x"}]
        mapping, leftover = sr._added_doc_chapter_map(
            docs, list(range(1, 13)), groups, {1})
        self.assertEqual(len(mapping.get(1, [])), sr.PATCH_INJECT_CAP)
        self.assertEqual(len(leftover), 12 - sr.PATCH_INJECT_CAP)


class AddedDocIndicesTest(unittest.TestCase):
    """_added_doc_indices — last_added_urls → 1-based 전역 인덱스 매핑."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.raw = Path(self._tmp.name) / "slug_2026-06-09_v1.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_maps_added_urls_to_indices(self):
        self.raw.write_text(json.dumps(
            {"last_added_urls": ["http://a/1", "http://w/1"]}), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            idxs = sr._added_doc_indices("slug", _docs())
        # _docs()[1].url = http://a/1 (idx 2), _docs()[2].url = http://w/1 (idx 3)
        self.assertEqual(idxs, [2, 3])

    def test_no_added_urls_returns_empty(self):
        self.raw.write_text(json.dumps({"documents": []}), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            self.assertEqual(sr._added_doc_indices("slug", _docs()), [])

    def test_includes_promoted_urls(self):
        """풀 승격 문서(last_promoted_urls)도 챕터 주입 대상에 포함된다(19차)."""
        self.raw.write_text(json.dumps(
            {"last_added_urls": ["http://a/1"],
             "last_promoted_urls": ["http://w/1"]}), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            idxs = sr._added_doc_indices("slug", _docs())
        self.assertEqual(idxs, [2, 3])   # 웹 신규 + 승격 모두

    def test_find_raw_failure_returns_empty(self):
        with patch.object(sr, "find_raw", side_effect=FileNotFoundError):
            self.assertEqual(sr._added_doc_indices("slug", _docs()), [])


class SelectedDocsTest(unittest.TestCase):
    """_selected_docs — 검증 재수집 신규 문서가 MAX_DOCS 절단과 무관하게 투입되는지.

    (회귀 테스트) 이전에는 신규 문서가 payload 끝에 붙고 [:MAX_DOCS] 로 잘려
    보고서에 0건 반영됐다 — 2026-06-10 평가 보고서 C1.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.raw = Path(self._tmp.name) / "slug_2026-06-10_v1.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _payload(self, n_base: int, added_urls: list[str]) -> dict:
        docs = [{"title": f"기본{i}", "url": f"http://base/{i}",
                 "content": "본문 " * 300, "source": "tavily",
                 "trust_score": 90.0 - i * 0.1} for i in range(n_base)]
        docs += [{"title": f"신규{u}", "url": u, "content": "재수집 본문 " * 200,
                  "source": "exa", "trust_score": 75.0} for u in added_urls]
        return {"queries": ["수소"], "documents": docs,
                "last_added_urls": added_urls,
                "validation_added_urls": added_urls}

    def test_added_docs_survive_max_docs_cut(self):
        """기본 문서가 MAX_DOCS 를 넘어도 재수집 신규 문서는 반드시 포함된다."""
        added = ["http://new/1", "http://new/2", "http://new/3"]
        self.raw.write_text(json.dumps(self._payload(sr.MAX_DOCS + 10, added)),
                            encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            selected, _q, _p = sr._selected_docs("slug")
        urls = [d["url"] for d in selected]
        for u in added:
            self.assertIn(u, urls)

    def test_base_selection_unchanged_by_added(self):
        """신규 문서 추가가 기본 선별 결과(앞부분)를 바꾸지 않는다 — 인덱스 안정."""
        base_only = self._payload(20, [])
        with_added = self._payload(20, ["http://new/1"])
        self.raw.write_text(json.dumps(base_only), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            sel1, _, _ = sr._selected_docs("slug")
        self.raw.write_text(json.dumps(with_added), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            sel2, _, _ = sr._selected_docs("slug")
        self.assertEqual([d["url"] for d in sel1],
                         [d["url"] for d in sel2[:len(sel1)]])
        self.assertEqual(sel2[-1]["url"], "http://new/1")

    def test_added_docs_capped(self):
        """신규 문서도 ADDED_DOCS_CAP 을 넘지 않는다(단일 챕터 주입 폭주 방지)."""
        added = [f"http://new/{i}" for i in range(sr.ADDED_DOCS_CAP + 10)]
        self.raw.write_text(json.dumps(self._payload(5, added)), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            selected, _q, _p = sr._selected_docs("slug")
        n_added = sum(1 for d in selected if d["url"].startswith("http://new/"))
        self.assertEqual(n_added, sr.ADDED_DOCS_CAP)

    def test_promoted_docs_always_included_without_competition(self):
        """promoted_urls 문서는 점수·상한과 무관하게 항상 맨 뒤에 투입된다(19차).

        승격 문서는 원래 선별에서 탈락한 저점수 문서 — 재경쟁시키면 도로
        탈락한다. 경쟁은 승격 시점에 끝났으므로 무조건 포함되어야 한다.
        """
        promoted_urls = ["http://promo/1", "http://promo/2"]
        payload = self._payload(sr.MAX_DOCS + 10, [])
        payload["documents"] += [
            {"title": f"승격{u}", "url": u, "content": "짧은 스텁",
             "source": "tavily", "trust_score": 10.0}      # 최저점
            for u in promoted_urls
        ]
        payload["promoted_urls"] = promoted_urls
        self.raw.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            selected, _q, _p = sr._selected_docs("slug")
        urls = [d["url"] for d in selected]
        self.assertEqual(urls[-2:], promoted_urls)             # 맨 뒤 무경쟁 투입
        self.assertEqual(len(selected), sr.MAX_DOCS + 2)       # 기본 40 + 승격 2

    def test_promoted_excluded_from_base_pool(self):
        """승격 문서는 기본 선별 풀에서 빠져 기본 40건 결과를 바꾸지 않는다."""
        payload_plain = self._payload(20, [])
        self.raw.write_text(json.dumps(payload_plain), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            sel_plain, _, _ = sr._selected_docs("slug")

        payload_promo = self._payload(20, [])
        payload_promo["promoted_urls"] = ["http://base/5"]   # 기본 문서 하나를 승격 처리
        self.raw.write_text(json.dumps(payload_promo), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            sel_promo, _, _ = sr._selected_docs("slug")

        # 승격 문서는 기본 구간에서 빠지고 맨 뒤로 이동
        self.assertEqual(sel_promo[-1]["url"], "http://base/5")
        self.assertNotIn("http://base/5",
                         [d["url"] for d in sel_promo[:-1]])

    def test_added_batch_respects_cumulative_group_quota(self):
        """기본 선별이 특허 쿼터(5)를 소진하면 재수집 특허는 추가되지 않는다(18차).

        (회귀) 이전에는 추가 배치 선별에서 쿼터가 리셋돼 특허가 배치당 5건씩,
        합계 10건까지 들어왔다 — 수소 보고서 실측.
        """
        def patent(url, source):
            return {"title": url, "url": url, "content": "메타데이터 " * 20,
                    "source": source, "trust_score": 95.0}

        added_urls = [f"http://newp/{i}" for i in range(4)]
        docs = ([patent(f"http://p/{i}", "kipris") for i in range(8)]
                + [patent(u, "kipris_foreign") for u in added_urls])
        payload = {"queries": ["수소"], "documents": docs,
                   "last_added_urls": added_urls,
                   "validation_added_urls": added_urls}
        self.raw.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(sr, "find_raw", return_value=self.raw):
            selected, _q, _p = sr._selected_docs("slug")
        n_patent = sum(1 for d in selected
                       if d["source"] in ("kipris", "kipris_foreign"))
        self.assertEqual(n_patent, 5)   # 합산 쿼터 유지


class Stage3Test(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.drafts = tmp / "drafts"
        self.reports = tmp / "reports"
        self._p1 = patch.object(sr, "DRAFTS_DIR", self.drafts)
        self._p2 = patch.object(sr, "REPORT_DIR", self.reports)
        self._p1.start()
        self._p2.start()
        d = self.drafts / "test-slug"
        d.mkdir(parents=True)
        (d / "ch01_기술개요.md").write_text("## 1. 기술 개요\n본문1", encoding="utf-8")
        (d / "ch02_시장맥락.md").write_text("## 2. 시장 맥락\n본문2", encoding="utf-8")

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self._tmp.cleanup()

    def test_assemble_combines_summary_chapters_references(self):
        with patch.object(sr, "_selected_docs",
                          return_value=(_docs(), ["수소"], None)), \
             patch.object(sr, "list_library", return_value=[]), \
             patch.object(sr, "_call_claude",
                          return_value="## 핵심 요약\n한 줄 결론"):
            out = sr.stage3_assemble("test-slug")

        text = out.read_text(encoding="utf-8")
        self.assertIn("핵심 요약", text)       # 요약
        self.assertIn("본문1", text)           # 챕터
        self.assertIn("본문2", text)
        self.assertIn("참고 자료", text)       # 참고자료
        # 순서: 요약 → 본문 → 참고자료
        self.assertLess(text.index("핵심 요약"), text.index("본문1"))
        self.assertLess(text.index("본문2"), text.index("참고 자료"))

    def test_summary_falls_back_to_full_body_without_memos(self):
        """메모 파일이 없으면(레거시) 본문 앞부분을 요약 입력으로 사용한다."""
        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            return "## 핵심 요약\n한 줄 결론"

        with patch.object(sr, "_selected_docs",
                          return_value=(_docs(), ["수소"], None)), \
             patch.object(sr, "list_library", return_value=[]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage3_assemble("test-slug")

        self.assertIn("본문1", seen[0])
        self.assertIn("본문2", seen[0])

    def test_summary_uses_memos_when_available(self):
        """메모가 있으면 본문 전체 대신 누적 메모를 요약 입력으로 사용한다."""
        d = self.drafts / "test-slug"
        (d / "ch01_memo.txt").write_text("메모1핵심", encoding="utf-8")
        (d / "ch02_memo.txt").write_text("메모2핵심", encoding="utf-8")

        seen = []

        def fake(prompt, model):
            seen.append(prompt)
            return "## 핵심 요약\n한 줄 결론"

        with patch.object(sr, "_selected_docs",
                          return_value=(_docs(), ["수소"], None)), \
             patch.object(sr, "list_library", return_value=[]), \
             patch.object(sr, "_call_claude", side_effect=fake):
            sr.stage3_assemble("test-slug")

        self.assertIn("메모1핵심", seen[0])
        self.assertIn("메모2핵심", seen[0])
        self.assertNotIn("본문1", seen[0])

    def test_missing_chapters_raises(self):
        with self.assertRaises(FileNotFoundError):
            sr.stage3_assemble("empty-slug")


if __name__ == "__main__":
    unittest.main(verbosity=2)
