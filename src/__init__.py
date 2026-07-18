"""Crawling 파이프라인 패키지.

Windows 콘솔(cp949)에서 유니코드 출력 시 UnicodeEncodeError 가 나는 것을
막기 위해, 패키지 로드 시 표준 출력/에러를 UTF-8 로 재구성한다.
(`python -m src.xxx` 실행 모두에 적용된다.)
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
