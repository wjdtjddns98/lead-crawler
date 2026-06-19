"""연락처 보강(Enrich) — IR이메일·전화·문의폼 추출.

:class:`leadcrawler.enrich.enricher.Enricher` 가 진입점. dry_run 은 결정적 더미,
라이브는 정적 BFS(홈페이지 + IR/문의 후보 페이지) 추출. 헤드리스/OCR/비전
escalation 은 후속 마일스톤.
"""
