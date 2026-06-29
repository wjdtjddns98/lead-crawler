"""normalize name: 티어 canonical_key 국가표기 (대한민국→KR)

도메인 없는 기업의 ``name:`` 티어 canonical_key 가 국가표기를 정규화 없이 써서,
import 시드('name:대한민국:...')와 라이브 크롤(세그먼트 'name:kr:...')이 같은 기업을
다른 key 로 저장 → 제약①(재추출 금지) 위반. 코드 수정(:func:`leadcrawler.dedup.
normalize_country`)과 함께, 이미 적재된 ``name:%`` 행도 ISO2 정규화로 리키한다.

canonical_key 는 PK 이고, 이를 가리키는 참조가 두 종류 있다:
  1) **FK** — ``discovered_company.duplicate_of``(self, SET NULL), ``company.canonical_key``(RESTRICT).
  2) **비-FK 파생/값 참조** — ``company.id``(= ``company_id_for(key)`` 해시), ``dedup_candidate``
     의 ``key_a/key_b``(쌍 정규형, id=``pair_id`` 해시)·``survivor_key``.
ON UPDATE CASCADE 가 없으므로 **새 PK 행 insert → 모든 참조 repoint(파생 id 재계산 포함)
→ 옛 행 delete** 순서로 처리해 SQLite(FK ON)·Postgres 양쪽에서 안전하게,
참조-완전하게 리키한다. 이미 정규화된 행·옛 행 부재는 건너뛰어 멱등.

리키 대상은 라이브 실측상 충돌 0·FK참조 0·company 0·dedup_candidate 0 이지만,
다른 환경/향후 데이터에서도 손실 없도록 위 모든 참조를 일반적으로 처리한다.
downgrade 는 비가역(여러 옛 표기가 ISO2 하나로 수렴)이라 no-op.

주의: 이 마이그레이션은 표기 규칙 단일 출처 유지를 위해 ``leadcrawler`` 앱 코드
(``normalize_country``/``company_id_for``/``pair_id``)를 import 한다 — "마이그레이션 동결"
원칙과의 트레이드오프이며, 1회성 데이터 보정이라 수용한다(드리프트 시 재적용 결과가
당시 코드 기준이 됨).

Revision ID: f1a9c3d7b2e4
Revises: a7c3e1d9f2b5
Create Date: 2026-06-29 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a9c3d7b2e4"
down_revision: str | None = "a7c3e1d9f2b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rekey_map(conn: sa.engine.Connection) -> dict[str, str]:
    """현재 ``name:%`` 행 중 정규화로 key 가 바뀌는 것만 {옛key: 새key} 로 반환."""
    # 코드와 동일한 정규화 단일 출처를 재사용한다(표기 규칙 드리프트 방지).
    from leadcrawler.dedup import normalize_country

    rows = conn.execute(
        sa.text("SELECT canonical_key FROM discovered_company WHERE canonical_key LIKE 'name:%'")
    ).scalars()
    mapping: dict[str, str] = {}
    for key in rows:
        # name:<country>:<normname> — country 만 정규화, 나머지(normname)는 보존.
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        _, country, rest = parts
        new_key = f"name:{normalize_country(country)}:{rest}"
        if new_key != key:
            mapping[key] = new_key
    return mapping


def _rekey_company(conn: sa.engine.Connection, old_key: str, new_key: str) -> None:
    """옛 key 를 참조하는 ``company`` 행을 새 key 로 리키한다(파생 id + 자식 FK 포함).

    ``company.id`` 는 ``company_id_for(canonical_key)`` 해시라 canonical_key 만 바꾸면
    불변식이 깨진다. 새 id 로 행을 다시 만들고 자식(contact·review_queue, FK→company.id)을
    repoint 한 뒤 옛 행을 지운다. (새 discovered_company 행은 이미 존재해 FK 충족.)
    """
    from leadcrawler.storage.repository import company_id_for

    companies = conn.execute(
        sa.text("SELECT * FROM company WHERE canonical_key = :old"), {"old": old_key}
    ).mappings().all()
    for comp in companies:
        new_id = company_id_for(new_key)
        old_id = comp["id"]
        if new_id == old_id:
            # 이론상 불가(다른 key→다른 해시)지만, 같으면 칼럼만 갱신.
            conn.execute(
                sa.text("UPDATE company SET canonical_key = :new WHERE id = :id"),
                {"new": new_key, "id": old_id},
            )
            continue
        data = dict(comp)
        data["id"] = new_id
        data["canonical_key"] = new_key
        cols = ", ".join(data.keys())
        binds = ", ".join(f":{c}" for c in data.keys())
        conn.execute(sa.text(f"INSERT INTO company ({cols}) VALUES ({binds})"), data)
        for child in ("contact", "review_queue"):
            conn.execute(
                sa.text(f"UPDATE {child} SET company_id = :new WHERE company_id = :old"),
                {"new": new_id, "old": old_id},
            )
        conn.execute(sa.text("DELETE FROM company WHERE id = :id"), {"id": old_id})


def _rekey_dedup_candidate(conn: sa.engine.Connection, mapping: dict[str, str]) -> None:
    """``dedup_candidate`` 의 key_a/key_b/survivor_key 를 새 key 로 repoint 한다.

    FK 가 아닌 값 참조(원장 행 삭제와 무관)라 마이그레이션이 안 건드리면 옛 key 를 가리켜
    스테일 후보가 된다(워크벤치 ``stale`` 표시되긴 하나 사람 결정 보존을 위해 정정).
    key_a<key_b 정규형·id=``pair_id`` 해시도 재계산하며, 새 id 가 이미 있으면(쌍 중복)
    옛 행을 지워 PK 충돌을 피한다.
    """
    from leadcrawler.storage.dedup_candidate import pair_id

    insp = sa.inspect(conn)
    if "dedup_candidate" not in insp.get_table_names():
        return
    rows = conn.execute(sa.text("SELECT * FROM dedup_candidate")).mappings().all()
    existing_ids = {r["id"] for r in rows}
    for row in rows:
        new_a = mapping.get(row["key_a"], row["key_a"])
        new_b = mapping.get(row["key_b"], row["key_b"])
        new_sv = mapping.get(row["survivor_key"]) if row["survivor_key"] else None
        a, b = sorted((new_a, new_b))
        new_id = pair_id(a, b)
        survivor = new_sv or row["survivor_key"]
        if (a, b, survivor) == (row["key_a"], row["key_b"], row["survivor_key"]):
            continue  # 변화 없음.
        if new_id != row["id"] and new_id in existing_ids:
            # 같은 쌍이 이미 존재(정규화로 두 후보가 한 쌍으로 수렴) → 옛 행 제거(중복).
            conn.execute(
                sa.text("DELETE FROM dedup_candidate WHERE id = :id"), {"id": row["id"]}
            )
            existing_ids.discard(row["id"])
            continue
        conn.execute(
            sa.text(
                "UPDATE dedup_candidate SET id = :nid, key_a = :a, key_b = :b, "
                "survivor_key = :sv WHERE id = :oid"
            ),
            {"nid": new_id, "a": a, "b": b, "sv": survivor, "oid": row["id"]},
        )
        existing_ids.discard(row["id"])
        existing_ids.add(new_id)


def _apply(conn: sa.engine.Connection) -> int:
    """리키를 수행하고 변경(삭제된 옛) 행 수를 반환한다(테스트가 op 프록시 없이 호출)."""
    mapping = _rekey_map(conn)
    if not mapping:
        return 0

    existing = set(
        conn.execute(sa.text("SELECT canonical_key FROM discovered_company")).scalars()
    )
    removed = 0
    for old_key, new_key in mapping.items():
        # 멱등: 옛 행이 이미 사라졌으면(재실행) 건너뛴다.
        row = conn.execute(
            sa.text("SELECT * FROM discovered_company WHERE canonical_key = :k"),
            {"k": old_key},
        ).mappings().first()
        if row is None:
            continue

        # 1) 새 PK 행 보장 — 없을 때만 옛 행 복사로 insert(충돌 시 기존 새 행 유지=머지).
        if new_key not in existing:
            data = dict(row)
            data["canonical_key"] = new_key
            cols = ", ".join(data.keys())
            binds = ", ".join(f":{c}" for c in data.keys())
            conn.execute(
                sa.text(f"INSERT INTO discovered_company ({cols}) VALUES ({binds})"), data
            )
            existing.add(new_key)

        # 2) 자식 repoint(옛 → 새). 옛 행 delete 전에 끊어야 FK 위반·SET NULL 방지.
        conn.execute(
            sa.text(
                "UPDATE discovered_company SET duplicate_of = :new WHERE duplicate_of = :old"
            ),
            {"new": new_key, "old": old_key},
        )
        # company 는 단순 칼럼 갱신이 아니라 파생 id 재계산이 필요(불변식 보존).
        _rekey_company(conn, old_key, new_key)

        # 3) 옛 원장 행 삭제.
        conn.execute(
            sa.text("DELETE FROM discovered_company WHERE canonical_key = :k"), {"k": old_key}
        )
        existing.discard(old_key)
        removed += 1

    # 4) FK 아닌 값 참조(dedup_candidate)는 전체 매핑으로 일괄 repoint(순서 무관).
    _rekey_dedup_candidate(conn, mapping)
    return removed


def upgrade() -> None:
    _apply(op.get_bind())


def downgrade() -> None:
    # ISO2 정규화는 비가역(여러 옛 표기 '대한민국'/'korea'/'KR' 가 'kr' 하나로 수렴)이라
    # 원래 표기를 복원할 수 없다. 데이터 보존을 위해 no-op 으로 둔다.
    pass
