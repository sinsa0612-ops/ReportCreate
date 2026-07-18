"""보고서 양식(템플릿) 시스템 테스트 — 파일 I/O 는 임시 폴더로 격리.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.test_report_templates -v
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src import report_templates as rt


def _min_template_dict(name="custom-x", n=3):
    return {
        "name": name,
        "label": f"라벨 {name}",
        "description": "테스트 양식",
        "chapters": [
            {"id": i, "title": f"제{i}장", "key_question": f"질문{i}",
             "must_include": f"포함{i}"}
            for i in range(1, n + 1)
        ],
        "opus_chapters": [1],
        "prev_body_chapters": [n],
        "memo_only_chapters": [n],
        "outline_extra_rules": "- 특별 규칙",
    }


class DefaultTemplateTest(unittest.TestCase):
    def test_default_is_seven_chapters(self):
        t = rt.DEFAULT_TEMPLATE
        self.assertEqual(t.n_chapters, 7)
        self.assertEqual(t.max_id, 7)
        self.assertEqual(t.chapter_ids, [1, 2, 3, 4, 5, 6, 7])

    def test_default_synthesis_and_memo_chapters(self):
        self.assertEqual(rt.DEFAULT_TEMPLATE.prev_body_chapters, frozenset({4}))
        self.assertEqual(rt.DEFAULT_TEMPLATE.memo_only_chapters, frozenset({7}))

    def test_models_split_opus_sonnet(self):
        models = rt.DEFAULT_TEMPLATE.models("OPUS", "SON")
        self.assertEqual(models[1], "OPUS")   # opus_chapters={1,5,6}
        self.assertEqual(models[5], "OPUS")
        self.assertEqual(models[6], "OPUS")
        self.assertEqual(models[2], "SON")
        self.assertEqual(models[4], "SON")

    def test_skeleton_and_ref_text(self):
        skel = rt.DEFAULT_TEMPLATE.skeleton_text()
        self.assertIn("1. 기술 개요", skel)
        self.assertIn("포함:", skel)
        ref = rt.DEFAULT_TEMPLATE.chapters_ref()
        self.assertIn("7. 시사점 및 권고안", ref)
        self.assertNotIn("포함:", ref)   # ref 는 제목만


class FromDictTest(unittest.TestCase):
    def test_roundtrip_valid(self):
        t = rt.from_dict(_min_template_dict(n=3))
        self.assertEqual(t.name, "custom-x")
        self.assertEqual(t.n_chapters, 3)
        self.assertEqual(t.max_id, 3)
        self.assertEqual(t.opus_chapters, frozenset({1}))
        self.assertEqual(t.memo_only_chapters, frozenset({3}))

    def test_missing_chapter_key_raises(self):
        d = _min_template_dict()
        del d["chapters"][0]["title"]
        with self.assertRaises(ValueError):
            rt.from_dict(d)

    def test_duplicate_chapter_id_raises(self):
        d = _min_template_dict()
        d["chapters"][1]["id"] = 1
        with self.assertRaises(ValueError):
            rt.from_dict(d)

    def test_out_of_range_id_sets_filtered(self):
        """opus/prev/memo 집합의 잘못된 id(챕터에 없는 번호)는 걸러진다."""
        d = _min_template_dict(n=3)
        d["opus_chapters"] = [1, 99]   # 99 는 존재하지 않음
        t = rt.from_dict(d)
        self.assertEqual(t.opus_chapters, frozenset({1}))


class RegistryTest(unittest.TestCase):
    def test_get_template_none_returns_default(self):
        self.assertIs(rt.get_template(None), rt.DEFAULT_TEMPLATE)

    def test_get_unknown_returns_default(self):
        with TemporaryDirectory() as d:
            self.assertIs(rt.get_template("nope", Path(d)), rt.DEFAULT_TEMPLATE)

    def test_json_template_loaded_and_overrides(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "custom-x.json").write_text(
                json.dumps(_min_template_dict("custom-x", 4)), encoding="utf-8")
            reg = rt.all_templates(p)
            self.assertIn("custom-x", reg)
            self.assertIn(rt.DEFAULT_TEMPLATE.name, reg)   # builtin 도 여전히 존재
            self.assertEqual(reg["custom-x"].n_chapters, 4)
            self.assertEqual(rt.get_template("custom-x", p).max_id, 4)

    def test_broken_json_is_skipped(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "bad.json").write_text("{not valid json", encoding="utf-8")
            reg = rt.all_templates(p)   # 예외 없이 건너뛰고 builtin 만 남음
            self.assertIn(rt.DEFAULT_TEMPLATE.name, reg)
            self.assertNotIn("bad", reg)


class PetrochemTemplateTest(unittest.TestCase):
    """실제 templates/petrochem-gx-ax.json 이 유효하게 로드되는지 확인."""

    def test_petrochem_template_valid(self):
        reg = rt.all_templates()   # 실제 templates/ 폴더 사용
        self.assertIn("petrochem-gx-ax", reg)
        t = reg["petrochem-gx-ax"]
        self.assertEqual(t.n_chapters, 7)
        self.assertEqual(t.prev_body_chapters, frozenset({4}))
        self.assertEqual(t.memo_only_chapters, frozenset({7}))
        # GX/AX 를 독립 챕터로 분리했는지
        titles = [c["title"] for c in t.chapters]
        self.assertTrue(any("GX" in x for x in titles))
        self.assertTrue(any("AX" in x for x in titles))


if __name__ == "__main__":
    unittest.main()
