"""중복해소(Entity Resolution) — canonical_key 1차 필터 이후의 cross-key 중복 제거.

:mod:`leadcrawler.dedup` 의 ``canonical_key`` 가 정확 동치(같은 등록처/도메인/이름)를
1차로 합친 뒤에도 남는, **다른 key 인데 같은 기업**(표기 차이·도메인 유무 차이 등)을
사다리(렉시컬→도메인→LLM→사람)로 걸러낸다. 이 패키지는 그 사다리의 무료·결정적
단계(C1 배치 리포트)를 담는다. 유료(LLM)·사람(워크벤치) 단계는 후속 트랙에서 추가된다.
"""
