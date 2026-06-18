"""연락처 보강(Enrich) — IR이메일·전화·문의폼 추출.

실 구현(BFS 크롤 → 헤드리스 → OCR/비전 → 폼 폴백)은 후속 마일스톤에서 추가한다.
dry_run 보강은 :func:`leadcrawler.pipeline.run.dry_run_enrich` 가 담당한다.
"""
