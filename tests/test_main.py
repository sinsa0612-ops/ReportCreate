"""main.py CLI 인자 파싱 테스트 — --generate/--template 플래그 분리 검증.

실행 (프로젝트 루트에서):
    .venv\\Scripts\\python.exe -m unittest tests.test_main -v
"""
import unittest

from src.main import parse_args


class ParseArgsTest(unittest.TestCase):
    def test_queries_only(self):
        """플래그 없이 쿼리만 주면 generate=False, validate=False, max_iter=1, template=None."""
        queries, generate, validate, max_iter, template = parse_args(
            ["green hydrogen", "electrolyzer"])
        self.assertEqual(queries, ["green hydrogen", "electrolyzer"])
        self.assertFalse(generate)
        self.assertFalse(validate)
        self.assertEqual(max_iter, 1)
        self.assertIsNone(template)

    def test_generate_flag_sets_true(self):
        """--generate 플래그가 있으면 generate=True 이고 쿼리에서는 제거된다."""
        queries, generate, validate, _, _ = parse_args(["연료전지", "--generate"])
        self.assertEqual(queries, ["연료전지"])
        self.assertTrue(generate)
        self.assertFalse(validate)

    def test_generate_flag_position_irrelevant(self):
        """--generate 위치는 무관하다(쿼리 사이에 와도 분리)."""
        queries, generate, validate, _, _ = parse_args(["--generate", "수소", "연료전지"])
        self.assertEqual(queries, ["수소", "연료전지"])
        self.assertTrue(generate)

    def test_validate_flag_implies_generate(self):
        """--validate 는 generate=True 를 함의한다(생성 후 검증)."""
        queries, generate, validate, _, _ = parse_args(["수소", "--validate"])
        self.assertEqual(queries, ["수소"])
        self.assertTrue(generate)
        self.assertTrue(validate)

    def test_unknown_flags_exit_with_error(self):
        """알 수 없는 --플래그는 즉시 에러 종료한다(20차 — 오타가 쿼리로 오염 방지)."""
        with self.assertRaises(SystemExit):
            parse_args(["수소", "--verbose"])

    def test_max_iter_parsed(self):
        """--max-iter N 이 정수로 파싱된다."""
        _, _, _, max_iter, _ = parse_args(["수소", "--max-iter", "3"])
        self.assertEqual(max_iter, 3)

    def test_template_parsed(self):
        """--template NAME 이 파싱되고 값은 쿼리에서 제거된다(등록된 양식)."""
        queries, _, _, _, template = parse_args(
            ["석유화학 GX AX", "--template", "petrochem-gx-ax"])
        self.assertEqual(queries, ["석유화학 GX AX"])
        self.assertEqual(template, "petrochem-gx-ax")

    def test_unknown_template_exits(self):
        """등록되지 않은 양식 이름은 즉시 에러 종료한다(오타 방지)."""
        with self.assertRaises(SystemExit):
            parse_args(["수소", "--template", "no-such-template"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
