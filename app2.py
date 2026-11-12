"""G팀 선행기술 조사·검토 시스템 — Streamlit 시안

실행:  streamlit run app.py
설치:  pip install -U streamlit pandas      (표 너비 옵션 width="stretch" 때문에 최신 버전 권장)
파일:  app.py 와 같은 폴더의 patent_app.db  (build_db.py 로 만든 것. 원본은 건드리지 않고 patent_demo.db 로 복사해서 사용)
       다른 DB 를 쓰려면 환경변수 PATENT_DB_SRC (원본), PATENT_DB (작업 사본) 지정

구조
  1) SQL 상수            : 화면마다 쓰는 조회 쿼리 (화면의 'SQL 보기'에 그대로 표시)
  2) 업무 규칙 함수       : 프로젝트·비교요소·검색어 관리·후보 등록·검토 저장/제출·담당자 결정·프로젝트 확정
                          (앱에서 먼저 검사하고, 트리거가 있는 DB에서는 같은 규칙을 DB가 한 번 더 막음.
                           후보 상태 전이(검토중·재검토·담당자결정·결론)는 트리거가 하므로 앱은 건드리지 않음.
                           DB 거부는 sqlite3.IntegrityError 로 오고, 화면은 그 메시지를 그대로 보여줌)
  3) 화면(Streamlit)      : 대시보드 / 특허 탐색 / 검토 / 프로젝트 관리(프로젝트·비교요소·검색어·진행 전환)
"""
import hashlib
import os
import re
import shutil
import sqlite3

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))     # 어느 폴더에서 실행해도 app.py 옆 파일을 찾도록


def _abs(path):
    return path if os.path.isabs(path) else os.path.join(HERE, path)


SRC_DB = _abs(os.environ.get("PATENT_DB_SRC", "patent_app.db"))
WORK_DB = _abs(os.environ.get("PATENT_DB", "patent_demo.db"))
MARKS = ["○", "△", "X"]
CONCLUSIONS = ["채택", "제외", "보류"]


class RuleError(Exception):
    """업무 규칙 위반 (앱 단계에서 검사한 것)"""


# ───────────────────────── 1) SQL ─────────────────────────
def _en(col):
    """영어 검색어: 앞뒤 글자가 영문자가 아닐 때만 적중 (복수형 s 허용, 대소문자 무시)
    → face 가 interface·surface 에 걸리지 않고, 'CNN을' 처럼 한글 조사가 붙은 경우는 적중"""
    body = f"' ' || LOWER(COALESCE({col}, '')) || ' '"
    return (f"({body} GLOB '*[^a-z]' || LOWER(t.term) || '[^a-z]*'"
            f" OR {body} GLOB '*[^a-z]' || LOWER(t.term) || 's[^a-z]*')")


# 영문자가 하나라도 든 검색어 → 위 영어 규칙(국문 명칭·영문 명칭·요약)
# 한글 검색어           → 띄어쓰기를 지우고 부분 일치(국문 명칭·요약)
_MATCH = f"""(CASE WHEN t.term GLOB '*[A-Za-z]*'
     THEN {_en('p.title_ko')} OR {_en('p.title_en')} OR {_en('p.abstract')}
     ELSE REPLACE(p.title_ko, ' ', '') LIKE '%' || REPLACE(t.term, ' ', '') || '%'
       OR REPLACE(p.abstract, ' ', '') LIKE '%' || REPLACE(t.term, ' ', '') || '%' END)"""

SQL_PROGRESS = """
SELECT p.project_id, p.title AS 프로젝트, m.name AS 담당자, p.status AS 상태,
       COUNT(c.candidate_id)                              AS 후보수,
       COALESCE(SUM(c.status = '결론'), 0)                 AS 결론완료,
       COALESCE(ROUND(100.0 * SUM(c.status = '결론')
                      / NULLIF(COUNT(c.candidate_id), 0)), 0) AS 진행률
FROM project p
JOIN team_member m ON m.member_id = p.owner_id
LEFT JOIN candidate c ON c.project_id = p.project_id
GROUP BY p.project_id
ORDER BY p.project_id
"""

SQL_TODO = """
SELECT c.candidate_id, p.title AS 프로젝트, pt.application_no AS 출원번호, pt.title_ko AS 명칭,
       c.status AS 후보상태,
       CASE c.status WHEN '재검토' THEN 2 ELSE 1 END AS 차수,
       COALESCE(r.status, '미작성')                    AS 내_검토
FROM candidate_reviewer cr
JOIN candidate c ON c.candidate_id = cr.candidate_id
JOIN project   p ON p.project_id   = c.project_id
JOIN patent   pt ON pt.patent_id   = c.patent_id
LEFT JOIN review r ON r.candidate_id = c.candidate_id AND r.member_id = cr.member_id
                  AND r.round = CASE c.status WHEN '재검토' THEN 2 ELSE 1 END
WHERE cr.member_id = ?
  AND c.status IN ('미검토', '검토중', '재검토')
  AND COALESCE(r.status, 'DRAFT') <> 'SUBMITTED'
ORDER BY c.project_id, c.candidate_id
"""

# 내 검토 진행률: 배정된 후보 중, 지금 내가 더 할 일이 없는 후보의 비율
#   (내 할 일 = 후보가 미검토/검토중/재검토이고 현재 차수의 내 검토가 아직 제출되지 않은 것)
SQL_MY_PROGRESS = """
SELECT COUNT(*) AS 배정,
       SUM(CASE WHEN c.status IN ('미검토', '검토중', '재검토')
                 AND COALESCE(r.status, 'DRAFT') <> 'SUBMITTED' THEN 0 ELSE 1 END) AS 완료
FROM candidate_reviewer cr
JOIN candidate c ON c.candidate_id = cr.candidate_id
LEFT JOIN review r ON r.candidate_id = c.candidate_id AND r.member_id = cr.member_id
                  AND r.round = CASE c.status WHEN '재검토' THEN 2 ELSE 1 END
WHERE cr.member_id = ?
"""

_BLOCKERS = """
SELECT c.candidate_id, pt.application_no AS 출원번호, pt.title_ko AS 명칭, c.status AS 후보상태,
       COUNT(cr.member_id) AS 검토자수,
       CASE WHEN COUNT(cr.member_id) < 2 THEN '검토자 2명 미만'
            WHEN c.status = '담당자결정'   THEN '담당자 결정 필요'
            WHEN c.status = '재검토'       THEN '2차 검토 대기'
            ELSE '1차 검토 미완료' END      AS 사유
FROM candidate c
JOIN patent pt ON pt.patent_id = c.patent_id
LEFT JOIN candidate_reviewer cr ON cr.candidate_id = c.candidate_id
WHERE c.project_id = ?
GROUP BY c.candidate_id
HAVING c.final_conclusion IS NULL OR COUNT(cr.member_id) < 2
"""
SQL_BLOCKERS = _BLOCKERS + "ORDER BY 사유, c.candidate_id"
SQL_BLOCKER_COUNT = "SELECT COUNT(*) FROM (" + _BLOCKERS + ")"


SQL_IPC_OPTIONS = """
SELECT DISTINCT ipc_subclass
FROM patent_ipc
WHERE ipc_subclass IS NOT NULL AND TRIM(ipc_subclass) <> ''
ORDER BY ipc_subclass
"""

# 초록이 20자 미만이거나 한글 명칭과 같은 특허는 검토할 본문이 없어 검색·추천에서 뺀다
USABLE = "(LENGTH(TRIM(p.abstract)) >= 20 AND TRIM(p.abstract) <> TRIM(p.title_ko))"

SQL_SEARCH = f"""
SELECT p.patent_id, p.application_no AS 출원번호, p.title_ko AS 명칭, p.application_date AS 출원일,
       COALESCE(p.cited_count, 0)         AS 피인용,
       COUNT(DISTINCT t.term)             AS 적중수,
       GROUP_CONCAT(DISTINCT t.term)      AS 적중_검색어,
       EXISTS (SELECT 1 FROM candidate c
               WHERE c.project_id = :pid AND c.patent_id = p.patent_id) AS 등록됨
FROM patent p
JOIN search_term t ON t.criterion_id = :c AND {_MATCH}
WHERE {USABLE}
  AND (:f IS NULL OR p.application_date >= :f)
  AND (:t IS NULL OR p.application_date <= :t)
  AND (:a IS NULL OR EXISTS (SELECT 1 FROM patent_applicant pa JOIN applicant a USING (applicant_id)
                             WHERE pa.patent_id = p.patent_id AND a.name_norm LIKE '%' || :a || '%'))
  AND (:s IS NULL OR EXISTS (SELECT 1 FROM patent_ipc i
                             WHERE i.patent_id = p.patent_id AND i.ipc_subclass = :s))
GROUP BY p.patent_id
ORDER BY 적중수 DESC, 피인용 DESC, p.application_date
LIMIT :lim
"""

SQL_RECOMMEND = f"""
WITH terms AS (
  SELECT t.criterion_id, t.term
  FROM search_term t JOIN criterion k ON k.criterion_id = t.criterion_id
  WHERE k.project_id = :pid
), h AS (
  SELECT p.patent_id,
         COUNT(DISTINCT t.criterion_id) AS 적중_요소수,
         COUNT(DISTINCT t.term)         AS 적중_검색어수
  FROM patent p JOIN terms t ON {_MATCH}
  GROUP BY p.patent_id
)
SELECT p.patent_id, p.application_no AS 출원번호, p.title_ko AS 명칭, p.application_date AS 출원일,
       h.적중_요소수, h.적중_검색어수, COALESCE(p.cited_count, 0) AS 피인용,
       p.abstract AS 요약
FROM h JOIN patent p ON p.patent_id = h.patent_id
WHERE {USABLE}
  AND NOT EXISTS (SELECT 1 FROM candidate c WHERE c.project_id = :pid AND c.patent_id = h.patent_id)
ORDER BY h.적중_요소수 DESC, 피인용 DESC, h.적중_검색어수 DESC, p.application_date
LIMIT :lim
"""

# ───────────────────────── DB 연결 ─────────────────────────
def connect(path=None):
    con = sqlite3.connect(path or WORK_DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")      # SQLite 는 연결마다 꺼져 있는 게 기본
    return con


def df(sql, params=(), path=None):
    con = connect(path)
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def reset_db():
    shutil.copyfile(SRC_DB, WORK_DB)


# ───────────────────── 2) 업무 규칙 함수 ─────────────────────
def pending_round(status):
    """후보 상태 → 지금 작성해야 하는 검토 차수 (없으면 None)"""
    return {"미검토": 1, "검토중": 1, "재검토": 2}.get(status)


def _project(con, project_id):
    row = con.execute("SELECT * FROM project WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        raise RuleError("없는 프로젝트입니다 (규칙 1)")
    return row


def criteria_of(con, project_id):
    return con.execute("SELECT criterion_id, no, name FROM criterion WHERE project_id = ? ORDER BY no",
                       (project_id,)).fetchall()


def register_candidate(con, project_id, patent_id, reviewers, by_member=None):
    """후보 등록 + 검토자 2명 배정을 한 트랜잭션으로 (규칙 2·4). by_member 가 주어지면 담당자만 등록 가능"""
    proj = _project(con, project_id)
    if by_member is not None and proj["owner_id"] != by_member:
        raise RuleError("프로젝트 담당자만 후보를 등록할 수 있습니다")
    if proj["status"] == "확정":
        raise RuleError("확정된 프로젝트는 수정할 수 없습니다 (규칙 10)")
    reviewers = list(reviewers)
    if len(set(reviewers)) != 2 or len(reviewers) != 2:
        raise RuleError("검토자는 서로 다른 2명이어야 합니다 (규칙 4)")
    if proj["owner_id"] in reviewers:
        raise RuleError("프로젝트 담당자는 검토자가 될 수 없습니다 (규칙 4)")
    with con:                                    # 예외가 나면 전체 롤백
        cur = con.execute("INSERT INTO candidate(project_id, patent_id) VALUES (?,?)",
                          (project_id, patent_id))                # 중복이면 UNIQUE 가 IntegrityError (규칙 2)
        con.executemany("INSERT INTO candidate_reviewer(candidate_id, member_id) VALUES (?,?)",
                        [(cur.lastrowid, m) for m in reviewers])
    return cur.lastrowid


def _load_review(con, candidate_id, member_id, rnd):
    return con.execute("SELECT * FROM review WHERE candidate_id=? AND member_id=? AND round=?",
                       (candidate_id, member_id, rnd)).fetchone()


def save_review(con, candidate_id, member_id, marks, conclusion, note=None, submit=False):
    """검토 임시저장/제출.  marks = {criterion_id: (판정, 근거)}  (규칙 4·5·6·7·8)"""
    cand = con.execute("SELECT * FROM candidate WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if cand is None:
        raise RuleError("없는 후보입니다 (규칙 1)")
    proj_status = _project(con, cand["project_id"])["status"]
    if proj_status == "확정":
        raise RuleError("확정된 프로젝트는 수정할 수 없습니다 (규칙 10)")
    if proj_status != "진행":
        raise RuleError("검토는 진행 중인 프로젝트에서만 작성할 수 있습니다 (규칙 9)")
    rnd = pending_round(cand["status"])
    if rnd is None:
        raise RuleError(f"'{cand['status']}' 상태의 후보는 검토를 작성할 수 없습니다 (규칙 8)")
    if con.execute("SELECT 1 FROM candidate_reviewer WHERE candidate_id=? AND member_id=?",
                   (candidate_id, member_id)).fetchone() is None:
        raise RuleError("배정된 검토자만 검토를 작성할 수 있습니다 (규칙 4)")
    crit_ids = {c["criterion_id"] for c in criteria_of(con, cand["project_id"])}
    if not set(marks) <= crit_ids:
        raise RuleError("이 후보가 속한 프로젝트의 비교요소로만 판정할 수 있습니다 (규칙 5)")
    if any(m not in MARKS for m, _ in marks.values()):
        raise RuleError("판정은 ○/△/X 만 가능합니다 (규칙 6)")
    if any(not (ev or "").strip() for _, ev in marks.values()):
        raise RuleError("판정을 고른 비교요소에는 근거를 한 줄씩 적어야 합니다")
    note = (note or "").strip() or None
    old = _load_review(con, candidate_id, member_id, rnd)
    if old is not None and old["status"] == "SUBMITTED":
        raise RuleError("제출된 검토는 수정·삭제할 수 없습니다 (규칙 7)")
    if submit:
        if set(marks) != crit_ids:
            raise RuleError(f"비교요소 {len(crit_ids)}개 판정이 모두 있어야 제출할 수 있습니다 — 전체 취소 (규칙 6)")
        if conclusion not in CONCLUSIONS:
            raise RuleError("결론은 채택/제외/보류 중 하나여야 합니다 (규칙 6)")
    elif conclusion not in CONCLUSIONS:
        conclusion = None

    with con:                                    # 어디서든 거부되면 전체 취소 (규칙 6)
        if old is None:                          # 항상 DRAFT 로 만든다 (규칙 6: 제출은 UPDATE 로만)
            rid = con.execute("INSERT INTO review(candidate_id, member_id, round, status, conclusion, note) "
                              "VALUES (?,?,?,?,?,?)",
                              (candidate_id, member_id, rnd, "DRAFT", conclusion, note)).lastrowid
        else:
            rid = old["review_id"]
            con.execute("UPDATE review SET conclusion=?, note=? WHERE review_id=?", (conclusion, note, rid))
            con.execute("DELETE FROM review_item WHERE review_id = ?", (rid,))
        con.executemany("INSERT INTO review_item(review_id, criterion_id, mark, evidence) VALUES (?,?,?,?)",
                        [(rid, cid, m, ev.strip()) for cid, (m, ev) in marks.items()])
        if submit:                               # 판정 4개를 다 넣은 뒤 마지막에 제출 → 후보 상태는 트리거가 바꿈
            con.execute("UPDATE review SET status='SUBMITTED' WHERE review_id=?", (rid,))
    return rid


def delete_draft(con, candidate_id, member_id):
    cand = con.execute("SELECT status FROM candidate WHERE candidate_id = ?", (candidate_id,)).fetchone()
    rnd = pending_round(cand["status"]) if cand else None
    old = _load_review(con, candidate_id, member_id, rnd) if rnd else None
    if old is None:
        raise RuleError("삭제할 임시 검토가 없습니다")
    if old["status"] == "SUBMITTED":
        raise RuleError("제출된 검토는 수정·삭제할 수 없습니다 (규칙 7)")
    with con:
        con.execute("DELETE FROM review_item WHERE review_id = ?", (old["review_id"],))
        con.execute("DELETE FROM review WHERE review_id = ?", (old["review_id"],))


def decide(con, candidate_id, member_id, conclusion, note):
    """담당자 결정: 담당자만, 채택/제외 + 사유 필수 (규칙 8)"""
    cand = con.execute("SELECT c.status, p.owner_id FROM candidate c JOIN project p USING(project_id) "
                       "WHERE c.candidate_id=?", (candidate_id,)).fetchone()
    if cand is None:
        raise RuleError("없는 후보입니다 (규칙 1)")
    if cand["status"] != "담당자결정":
        raise RuleError("담당자 결정 대기 상태의 후보만 결정할 수 있습니다 (규칙 8)")
    if cand["owner_id"] != member_id:
        raise RuleError("프로젝트 담당자만 결정할 수 있습니다 (규칙 8)")
    if conclusion not in ("채택", "제외"):
        raise RuleError("담당자 결정은 채택/제외 중 하나여야 합니다 (규칙 8)")
    if not (note or "").strip():
        raise RuleError("결정 사유를 기록해야 합니다 (규칙 8)")
    with con:
        con.execute("UPDATE candidate SET status='결론', final_conclusion=?, decided_by=?, decision_note=? "
                    "WHERE candidate_id=?", (conclusion, member_id, note.strip(), candidate_id))


def finalize_project(con, project_id, member_id):
    """프로젝트 확정: 후보 없음·결론 없는 후보·검토자 2명 미만 후보가 있으면 불가 (규칙 10)"""
    proj = _project(con, project_id)
    if proj["owner_id"] != member_id:
        raise RuleError("프로젝트 담당자만 확정할 수 있습니다")
    if proj["status"] == "확정":
        raise RuleError("이미 확정된 프로젝트입니다")
    n = con.execute("SELECT COUNT(*) FROM candidate WHERE project_id=?", (project_id,)).fetchone()[0]
    if n == 0:
        raise RuleError("후보가 없어 확정할 수 없습니다 (규칙 10)")
    bad = con.execute(SQL_BLOCKER_COUNT, (project_id,)).fetchone()[0]
    if bad:
        raise RuleError(f"결론이 없거나 검토자가 2명 미만인 후보가 {bad}건 있어 확정할 수 없습니다 (규칙 10)")
    with con:
        con.execute("UPDATE project SET status='확정' WHERE project_id=?", (project_id,))


# ── 프로젝트 준비: 프로젝트·비교요소·검색어 (규칙 9) ──
def _owner_project(con, project_id, member_id, action):
    proj = _project(con, project_id)
    if proj["owner_id"] != member_id:
        raise RuleError(f"프로젝트 담당자만 {action}할 수 있습니다")
    return proj


def create_project(con, title, owner_id):
    """프로젝트는 항상 '준비' 상태로 만든다 (규칙 9). 비교요소 4개를 넣은 뒤 '진행'으로 바꾼다"""
    title = (title or "").strip()
    if not title:
        raise RuleError("프로젝트 제목을 입력하세요")
    with con:
        cur = con.execute("INSERT INTO project(owner_id, title, status) VALUES (?,?,'준비')", (owner_id, title))
    return cur.lastrowid


def save_criteria(con, project_id, names, member_id):
    """비교요소 저장. names = {1: 이름, 2: 이름, 3: 이름, 4: 이름}, 빈칸이면 그 번호는 삭제.
    '준비' 상태에서만 추가·수정·삭제할 수 있다 (규칙 9)"""
    proj = _owner_project(con, project_id, member_id, "비교요소를 수정")
    if proj["status"] != "준비":
        raise RuleError("비교요소는 프로젝트가 준비 상태일 때만 추가·수정·삭제할 수 있습니다 (규칙 9)")
    current = {c["no"]: c for c in criteria_of(con, project_id)}
    with con:                                    # 하나라도 거부되면 전체 취소
        for no in (1, 2, 3, 4):
            name = (names.get(no) or "").strip()
            row = current.get(no)
            if name and row is None:
                con.execute("INSERT INTO criterion(project_id, no, name) VALUES (?,?,?)", (project_id, no, name))
            elif name and row["name"] != name:
                con.execute("UPDATE criterion SET name=? WHERE criterion_id=?", (name, row["criterion_id"]))
            elif not name and row is not None:
                con.execute("DELETE FROM criterion WHERE criterion_id=?", (row["criterion_id"],))   # 검색어도 함께 삭제


def parse_terms(text):
    """'a, b\nc' → ['a', 'b', 'c'] (앞뒤 공백 제거, 대소문자만 다른 중복 제거)"""
    seen, out = set(), []
    for t in re.split(r"[,\n]", text or ""):
        t = t.strip()
        if t and t.casefold() not in seen:
            seen.add(t.casefold())
            out.append(t)
    return out


def save_terms(con, project_id, terms_by_criterion, member_id):
    """비교요소별 동의어 검색어 저장. {criterion_id: '쉼표로 구분한 문자열'}. 확정된 프로젝트는 수정 불가"""
    proj = _owner_project(con, project_id, member_id, "검색어를 수정")
    if proj["status"] == "확정":
        raise RuleError("확정된 프로젝트는 수정할 수 없습니다 (규칙 10)")
    if not set(terms_by_criterion) <= {c["criterion_id"] for c in criteria_of(con, project_id)}:
        raise RuleError("이 프로젝트의 비교요소가 아닙니다")
    with con:
        for cid, text in terms_by_criterion.items():
            new = parse_terms(text)
            keep = {t.casefold() for t in new}
            old = [r["term"] for r in con.execute("SELECT term FROM search_term WHERE criterion_id=?", (cid,))]
            for t in old:
                if t.casefold() not in keep:
                    con.execute("DELETE FROM search_term WHERE criterion_id=? AND term=?", (cid, t))
            have = {t.casefold() for t in old}
            for t in new:
                if t.casefold() not in have:
                    con.execute("INSERT INTO search_term(criterion_id, term) VALUES (?,?)", (cid, t))


def start_project(con, project_id, member_id):
    """준비 → 진행: 비교요소가 정확히 4개여야 한다 (규칙 9). 진행 후에는 비교요소를 고칠 수 없다"""
    proj = _owner_project(con, project_id, member_id, "진행으로 바꿀")
    if proj["status"] != "준비":
        raise RuleError("준비 상태의 프로젝트만 진행으로 바꿀 수 있습니다 (규칙 9)")
    if len(criteria_of(con, project_id)) != 4:
        raise RuleError("비교요소 4개가 있어야 프로젝트를 진행할 수 있습니다 (규칙 9)")
    with con:
        con.execute("UPDATE project SET status='진행' WHERE project_id=?", (project_id,))


def delete_project(con, project_id, member_id):
    """잘못 만든 프로젝트 삭제: 준비 상태이고 후보가 없을 때만 (비교요소·검색어는 함께 삭제)"""
    proj = _owner_project(con, project_id, member_id, "삭제")
    if proj["status"] != "준비":
        raise RuleError("준비 상태의 프로젝트만 삭제할 수 있습니다 (규칙 9)")
    if con.execute("SELECT COUNT(*) FROM candidate WHERE project_id=?", (project_id,)).fetchone()[0]:
        raise RuleError("후보가 있는 프로젝트는 삭제할 수 없습니다")
    with con:
        con.execute("DELETE FROM project WHERE project_id=?", (project_id,))


# ───────────────────────── 3) 화면 ─────────────────────────
def sql_box(sql):
    with st.expander("실행된 SQL"):
        st.code(sql.strip(), language="sql")


def act(fn, *args, ok="완료", **kw):
    """규칙 위반·DB 거부를 화면에 그대로 보여준다.  성공하면 메시지를 남기고 새로고침."""
    con = connect()
    try:
        fn(con, *args, **kw)
    except RuleError as e:
        st.error(f"규칙 위반(앱 검사): {e}")
    except sqlite3.IntegrityError as e:
        st.error(f"DB가 거부함: {e}")
    else:
        st.session_state["flash"] = ok
        st.rerun()
    finally:
        con.close()


def table_select(data, sig_cols, prefix, drop=()):
    """표를 보여주고, 사용자가 클릭한 행 1개(pandas Series)를 돌려준다. 선택이 없으면 None.

    표의 key 에 '내용 서명'을 넣어서, 검색 조건이 바뀌거나 등록·제출로 표가 달라지면
    이전에 클릭해 둔 선택이 자동으로 풀린다 (다른 행에 선택이 잘못 붙는 것을 막음)."""
    sig = hashlib.md5(data[list(sig_cols)].to_csv(index=False).encode()).hexdigest()[:10]
    event = st.dataframe(data.drop(columns=list(drop)), hide_index=True, width="stretch",
                         on_select="rerun", selection_mode="single-row", key=f"{prefix}_{sig}")
    rows = event.selection.rows
    if rows and rows[0] < len(data):
        return data.iloc[rows[0]]
    return None


def page_dashboard(me, names, project_id):
    st.caption("진행률 = 프로젝트별로 최종 결론이 난 후보의 비율입니다. "
               "'확정을 막는 후보'가 모두 풀려야 프로젝트를 확정할 수 있습니다.")
    st.subheader("전체 프로젝트 현황")
    prog = df(SQL_PROGRESS)
    total_candidates = int(prog["후보수"].sum()) if not prog.empty else 0
    total_concluded = int(prog["결론완료"].sum()) if not prog.empty else 0
    total_projects = len(prog)
    k1, k2, k3 = st.columns(3)
    k1.metric("전체 프로젝트", f"{total_projects}개")
    k2.metric("전체 후보", f"{total_candidates}건")
    k3.metric("최종 결론 완료", f"{total_concluded}건",
              delta=f"{round(100 * total_concluded / total_candidates)}%" if total_candidates else "후보 없음")
    st.dataframe(prog.drop(columns="project_id"), hide_index=True, width="stretch",
                 column_config={"진행률": st.column_config.ProgressColumn("진행률(%)", min_value=0, max_value=100,
                                                                         format="%d")})
    sql_box(SQL_PROGRESS)

    st.subheader(f"내 할 일 — {names[me]}")
    mine = df(SQL_MY_PROGRESS, (me,)).iloc[0]
    assigned = int(mine["배정"])
    done = int(mine["완료"]) if pd.notna(mine["완료"]) else 0
    if assigned == 0:
        st.info("배정된 검토가 없습니다.")
    else:
        st.progress(done / assigned, text=f"내 검토 진행률 {done}/{assigned}건 ({round(100 * done / assigned)}%)")
        st.caption("배정된 후보 중 지금 내가 더 할 일이 없는 후보의 비율입니다 (재검토가 되면 다시 내려갑니다).")
    sql_box(SQL_MY_PROGRESS)
    todo = df(SQL_TODO, (me,))
    if todo.empty:
        st.success("작성할 검토가 없습니다.")
    else:
        st.dataframe(todo.drop(columns="candidate_id"), hide_index=True, width="stretch")
    sql_box(SQL_TODO)

    st.subheader("확정을 막는 후보")
    blk = df(SQL_BLOCKERS, (project_id,))
    candidate_count = int(df("SELECT COUNT(*) AS n FROM candidate WHERE project_id = ?",
                             (project_id,)).iloc[0]["n"])
    if candidate_count == 0:
        st.warning("후보가 아직 없습니다. 후보를 1건 이상 등록해야 프로젝트를 확정할 수 있습니다.")
    elif blk.empty:
        st.success("확정을 막는 후보가 없습니다. 프로젝트를 확정할 수 있습니다.")
    else:
        st.dataframe(blk.drop(columns="candidate_id"), hide_index=True, width="stretch")
    sql_box(SQL_BLOCKERS)
    if st.button("이 프로젝트 확정", help="담당자만 가능 · 막는 후보가 있으면 거부"):
        act(finalize_project, project_id, me, ok="프로젝트를 확정했습니다.")


def page_search(me, names, project_id):
    con = connect()
    crit = criteria_of(con, project_id)
    owner = _project(con, project_id)["owner_id"]
    con.close()
    if not crit:
        st.info("이 프로젝트에는 비교요소가 아직 없습니다. '프로젝트 관리'에서 비교요소와 검색어를 먼저 정하세요.")
        return
    tab_s, tab_r = st.tabs(["동의어 검색", "후보 추천"])

    with tab_s:
        st.caption("고른 비교요소의 검색어(동의어)에 걸린 특허를 찾고, 아래 조건은 모두 함께(AND) 적용됩니다. "
                   "표에서 행을 클릭하면 바로 아래에서 후보로 등록할 수 있습니다 (등록은 프로젝트 담당자만).")
        c1, c2 = st.columns(2)
        cid = c1.selectbox("비교요소", [c["criterion_id"] for c in crit],
                           format_func=lambda i: next(f"{c['no']}. {c['name']}" for c in crit if c["criterion_id"] == i))
        ipc_values = df(SQL_IPC_OPTIONS)["ipc_subclass"].dropna().astype(str).tolist()
        subclass = c2.selectbox("IPC 분류", ["(전체)", "G06V", "G06F", "G06N", "G06T", "G06Q", "H04N", "G06K", "H04L", "G08G", "H04W", "A61B", "G07C"])
        c3, c4, c5 = st.columns(3)
        date_min, date_max = df("SELECT MIN(application_date), MAX(application_date) FROM patent").iloc[0]
        from datetime import date
        min_date = date.fromisoformat(date_min) if date_min else date(2014, 1, 1)
        max_date = date.fromisoformat(date_max) if date_max else date.today()
        date_range = c3.date_input("출원일 범위", value=(min_date, max_date),
                                   min_value=min_date, max_value=max_date)
        applicant = c4.text_input("출원인 이름 (일부만 입력)", "", placeholder="예: 삼성")
        lim = c5.slider("최대 건수", 5, 100, 20)
        if len(date_range) == 2:
            dfrom, dto = (d.isoformat() for d in date_range)
        else:
            dfrom, dto = date_range[0].isoformat(), None
        invalid_range = bool(dfrom and dto and dfrom > dto)
        if invalid_range:
            st.error("출원 시작일은 종료일보다 늦을 수 없습니다.")
            res = pd.DataFrame()
        else:
            params = {"c": cid, "pid": project_id, "f": dfrom or None, "t": dto or None,
                      "a": applicant.strip() or None,
                      "s": None if subclass == "(전체)" else subclass, "lim": lim}
            res = df(SQL_SEARCH, params)
        if not res.empty:
            res["등록됨"] = res["등록됨"].astype(bool)
        st.caption(f"조회 결과: {len(res)}건 (최대 {lim}건 표시)")
        row = table_select(res, ["patent_id", "등록됨"], "res",
                           drop=["patent_id"]) if len(res) else None
        if row is not None:
            show_patent_detail(int(row["patent_id"]))
        sql_box(SQL_SEARCH)
        register_box(row, project_id, owner, names, "reg_s", me)

    with tab_r:
        st.caption("프로젝트의 비교요소 4개 전체 기준으로, 더 많은 비교요소에 걸린 특허를 위로 올립니다 "
                   "(이미 후보인 특허는 제외). 위 검색 조건과는 별개입니다.")
        lim = st.slider("추천 건수", 5, 30, 10, key="rec_lim")
        rec = df(SQL_RECOMMEND, {"pid": project_id, "lim": lim})
        row = table_select(rec, ["patent_id"], "rec", drop=["patent_id", "요약"]) if len(rec) else None
        if row is not None:
            show_patent_detail(int(row["patent_id"]))
        sql_box(SQL_RECOMMEND)
        register_box(row, project_id, owner, names, "reg_r", me)


def show_patent_detail(patent_id):
    """선택한 특허의 요약·IPC·출원인 정보를 후보 등록 전에 확인"""
    detail = df("""
        SELECT patent_id, application_no, title_ko, title_en, application_date,
               abstract, COALESCE(cited_count, 0) AS cited_count
        FROM patent WHERE patent_id = ?
    """, (patent_id,))
    if detail.empty:
        st.warning("특허 상세 정보를 찾을 수 없습니다.")
        return
    r = detail.iloc[0]
    ipc = df("""
        SELECT ipc_subclass
        FROM patent_ipc WHERE patent_id = ?
        GROUP BY ipc_subclass ORDER BY ipc_subclass
    """, (patent_id,))
    applicants = df("""
        SELECT a.name_norm
        FROM patent_applicant pa JOIN applicant a USING (applicant_id)
        WHERE pa.patent_id = ? ORDER BY pa.seq
    """, (patent_id,))
    with st.expander("선택한 특허 상세 정보 (요약 확인)", expanded=True):
        st.markdown(f"**{r['title_ko']}**")
        st.caption(f"출원번호 {r['application_no']} · 출원일 {r['application_date']} · 피인용 {r['cited_count']}")
        if pd.notna(r["title_en"]) and str(r["title_en"]).strip():
            st.caption(str(r["title_en"]))
        st.write(str(r["abstract"]) if pd.notna(r["abstract"]) else "등록된 요약이 없습니다.")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**출원인**")
            st.write(", ".join(applicants["name_norm"].astype(str).tolist()) if not applicants.empty else "정보 없음")
        with c2:
            st.markdown("**IPC 분류**")
            st.write(", ".join(ipc["ipc_subclass"].astype(str).tolist()) if not ipc.empty else "정보 없음")


def register_box(row, project_id, owner, names, key, me):
    """표에서 클릭한 특허(row)를 후보로 등록"""
    st.markdown("**후보로 등록**")
    if owner != me:
        st.info(f"이 프로젝트의 담당자({names[owner]})만 후보를 등록할 수 있습니다. "
                "사이드바에서 담당자를 선택하거나 내 프로젝트로 바꿔주세요.")
        return
    if row is None:
        st.info("위 표에서 등록할 특허의 행을 클릭하세요.")
        return
    if "등록됨" in row.index and bool(row["등록됨"]):
        st.warning("이미 이 프로젝트의 후보입니다.")
        return
    patent_id = int(row["patent_id"])
    st.markdown(f"선택한 특허: **{row['출원번호']}** · {row['명칭']}")
    revs = st.multiselect("검토자 2명 (담당자 제외가 규칙)", list(names), format_func=names.get,
                          key=f"{key}_rv_{patent_id}")
    st.caption(f"프로젝트 담당자: {names[owner]}")
    if st.button("등록", key=f"{key}_btn"):
        act(register_candidate, project_id, patent_id, revs, by_member=me, ok="후보를 등록했습니다.")


def page_review(me, names, projects, project_id):
    st.caption(
        f"검토는 사이드바에서 선택한 사용자({names[me]}) 이름으로 기록됩니다. "
        "'담당자 결정'은 1·2차 검토에서도 의견이 갈린 후보를 해당 프로젝트 담당자가 최종 결정하는 기능입니다."
    )
    tab_m, tab_d = st.tabs(["내 검토", "담당자 결정"])

    with tab_m:
        todo = df(SQL_TODO, (me,))
        if todo.empty:
            st.success("작성할 검토가 없습니다.")
        else:
            st.caption("표에서 검토할 후보의 행을 클릭하면 아래에 검토 양식이 열립니다.")
            row = table_select(todo, ["candidate_id", "후보상태", "차수"], "todo",
                               drop=["candidate_id"])
            if row is None:
                st.info("위 표에서 검토할 후보의 행을 클릭하세요.")
            else:
                review_form(me, int(row["candidate_id"]))

    with tab_d:
        # 담당자 결정은 사이드바에서 현재 선택한 프로젝트 하나에 대해서만 노출
        selected_project = projects[projects.project_id == project_id]
        if selected_project.empty:
            st.error("선택한 프로젝트 정보를 찾을 수 없습니다.")
        else:
            owner_id = int(selected_project.iloc[0]["owner_id"])
            if int(me) != owner_id:
                st.info(
                    f"담당자 결정은 프로젝트 담당자({names[owner_id]})만 할 수 있습니다. "
                    "사이드바에서 담당자 계정으로 전환하면 결정할 수 있습니다."
                )
            else:
                pend = df("""
                    SELECT c.candidate_id, pt.application_no AS 출원번호,
                           pt.title_ko AS 명칭, c.project_id
                    FROM candidate c
                    JOIN patent pt USING (patent_id)
                    WHERE c.status = '담당자결정' AND c.project_id = ?
                    ORDER BY c.candidate_id
                """, (project_id,))

                if pend.empty:
                    st.success("현재 선택한 프로젝트에는 담당자 결정이 필요한 후보가 없습니다.")
                else:
                    lab = {
                        int(r.candidate_id): f"{r.출원번호} · {r.명칭[:40]}"
                        for r in pend.itertuples()
                    }
                    cid = st.selectbox(
                        "결정할 후보", list(lab), format_func=lab.get, key="dec_c"
                    )

                    st.subheader("검토 이력 및 판정 근거")
                    hist = df("""
                        SELECT r.round AS 차수, m.name AS 검토자,
                               r.conclusion AS 결론, r.note AS 검토_종합의견,
                               k.no AS 요소번호, k.name AS 비교요소,
                               i.mark AS 판정, i.evidence AS 판정근거
                        FROM review r
                        JOIN team_member m ON m.member_id = r.member_id
                        LEFT JOIN review_item i ON i.review_id = r.review_id
                        LEFT JOIN criterion k ON k.criterion_id = i.criterion_id
                        WHERE r.candidate_id = ? AND r.status = 'SUBMITTED'
                        ORDER BY r.round, m.name, k.no
                    """, (cid,))

                    if hist.empty:
                        st.warning("제출된 검토 이력을 찾을 수 없습니다.")
                    else:
                        for rnd in sorted(hist["차수"].dropna().unique()):
                            round_data = hist[hist["차수"] == rnd]
                            st.markdown(f"**{int(rnd)}차 검토**")
                            for reviewer in round_data["검토자"].dropna().unique():
                                reviewer_data = round_data[round_data["검토자"] == reviewer]
                                first = reviewer_data.iloc[0]
                                st.markdown(
                                    f"- **{reviewer}**: {first['결론']} "
                                    + (f"/ 종합 의견: {first['검토_종합의견']}"
                                       if pd.notna(first["검토_종합의견"]) and str(first["검토_종합의견"]).strip()
                                       else "")
                                )
                                items = reviewer_data[
                                    reviewer_data["비교요소"].notna()
                                ][["요소번호", "비교요소", "판정", "판정근거"]]
                                if not items.empty:
                                    st.dataframe(items, hide_index=True, width="stretch")

                    st.warning(
                        "최종 결정은 검토자들의 결론과 비교요소별 판정 근거를 확인한 뒤 내려주세요. "
                        "결정 사유는 필수로 기록해야 합니다."
                    )
                    with st.form("decide"):
                        concl = st.radio(
                            "최종 결론", ["채택", "제외"], index=None, horizontal=True
                        )
                        note = st.text_area("결정 사유", placeholder="최종 결론을 내린 근거를 입력하세요.")
                        submitted = st.form_submit_button("최종 결정", type="primary")
                    if submitted:
                        if concl is None:
                            st.error("최종 결론으로 채택 또는 제외를 선택하세요.")
                        elif not note.strip():
                            st.error("결정 사유를 입력해야 합니다.")
                        else:
                            act(
                                decide, cid, me, concl, note,
                                ok="최종 결론을 기록했습니다."
                            )


def review_form(me, cid):
    con = connect()
    cand = con.execute("""SELECT c.candidate_id, c.project_id, c.status, pt.application_no, pt.title_ko,
                                 pt.application_date, pt.abstract
                          FROM candidate c JOIN patent pt USING(patent_id) WHERE c.candidate_id=?""",
                       (cid,)).fetchone()
    rnd = pending_round(cand["status"])
    crit = criteria_of(con, cand["project_id"])
    old = _load_review(con, cid, me, rnd)
    prev = {}
    if old is not None:
        prev = {r["criterion_id"]: (r["mark"], r["evidence"] or "")
                for r in con.execute("SELECT * FROM review_item WHERE review_id=?", (old["review_id"],))}
    first = None                                    # 2차 검토일 때 참고할 내 1차 검토
    if rnd == 2:
        r1 = _load_review(con, cid, me, 1)
        if r1 is not None:
            items = con.execute("""SELECT k.no AS 번호, k.name AS 비교요소, i.mark AS 판정, i.evidence AS 근거
                                   FROM review_item i JOIN criterion k ON k.criterion_id = i.criterion_id
                                   WHERE i.review_id = ? ORDER BY k.no""", (r1["review_id"],)).fetchall()
            first = (r1["conclusion"], pd.DataFrame([dict(r) for r in items]))
    con.close()

    st.markdown(f"**{cand['title_ko']}**  \n출원번호 {cand['application_no']} · 출원일 {cand['application_date']} "
                f"· {rnd}차 검토")
    with st.expander("요약 (검토 시 읽는 본문)", expanded=True):
        st.write(cand["abstract"])
    if first is not None:
        with st.expander("내 1차 검토 (참고용)", expanded=True):
            st.caption(f"내가 1차에서 제출한 결론: **{first[0]}**")
            st.dataframe(first[1], hide_index=True, width="stretch")
    st.caption("판정 기준: ○ 요약에 분명히 나옴 · △ 일부·불명확 · X 없음  /  "
               "결론 가이드: ○ 3개 이상이고 핵심 방법 ○ → 채택, ○ 1개 이하 → 제외, 그 밖 → 보류")
    with st.form(f"rv_{cid}_{rnd}"):
        marks = {}
        for c in crit:
            st.markdown(f"**{c['no']}. {c['name']}**")
            a, b = st.columns([1, 3])
            m = a.radio("판정", MARKS, horizontal=True, label_visibility="collapsed", key=f"m{cid}{rnd}{c['no']}",
                        index=MARKS.index(prev[c["criterion_id"]][0]) if c["criterion_id"] in prev else None)
            ev = b.text_input("근거", value=prev.get(c["criterion_id"], ("", ""))[1], key=f"e{cid}{rnd}{c['no']}",
                              label_visibility="collapsed", placeholder="요약에서 근거가 되는 문장")
            if m is not None:
                marks[c["criterion_id"]] = (m, ev)
        concl = st.selectbox("결론", CONCLUSIONS, index=CONCLUSIONS.index(old["conclusion"])
                             if old is not None and old["conclusion"] else None, placeholder="선택")
        note = st.text_area("종합 의견 (결론이 위 가이드와 다르면 이유를 꼭 적어야 제출됩니다)",
                            value=(old["note"] or "") if old is not None else "")
        b1, b2, b3 = st.columns(3)
        save = b1.form_submit_button("임시저장")
        sub = b2.form_submit_button("제출", type="primary")
        dele = b3.form_submit_button("임시 검토 삭제")
    if save:
        act(save_review, cid, me, marks, concl, note, ok="임시저장했습니다.")
    if sub:
        act(save_review, cid, me, marks, concl, note, submit=True, ok="제출했습니다.")
    if dele:
        act(delete_draft, cid, me, ok="임시 검토를 삭제했습니다.")


CRIT_LABELS = {1: "① 해결 문제", 2: "② 핵심 방법", 3: "③ 입력 데이터", 4: "④ 적용 분야"}


def _create_and_select(con, title, owner_id):
    st.session_state["_select_project"] = create_project(con, title, owner_id)


def _delete_and_reset(con, project_id, member_id):
    delete_project(con, project_id, member_id)
    st.session_state["_reset_project"] = True


def page_manage(me, names, projects, project_id):
    st.caption("프로젝트마다 비교요소 4개와 비교요소별 동의어 검색어를 정한 뒤 '진행'으로 바꾸면 검토를 시작할 수 있습니다. "
               "비교요소는 '준비' 상태에서만 바꿀 수 있고, 진행하면 고칠 수 없습니다 (규칙 9).")
    st.subheader("새 프로젝트")
    with st.form("new_project", clear_on_submit=True):
        title = st.text_input(f"프로젝트 제목 (담당자: {names[me]})", placeholder="예: 신경망 기반 얼굴 인식·인증")
        created = st.form_submit_button("프로젝트 만들기")
    if created:
        act(_create_and_select, title, me, ok="프로젝트를 만들었습니다. 아래에서 비교요소를 정하세요.")

    if project_id is None:
        st.info("아직 프로젝트가 없습니다. 위에서 프로젝트를 만들어 주세요.")
        return
    proj = projects[projects.project_id == project_id].iloc[0]
    owner, status = int(proj["owner_id"]), proj["status"]
    is_owner = owner == me
    st.divider()
    st.subheader(f"{proj['title']} — {status}")
    st.caption(f"담당자: {names[owner]}")
    if not is_owner:
        st.info(f"비교요소·검색어는 프로젝트 담당자({names[owner]})만 바꿀 수 있습니다. 사이드바에서 담당자로 전환하세요.")

    con = connect()
    crit = criteria_of(con, project_id)
    terms = {c["criterion_id"]: [r["term"] for r in con.execute(
        "SELECT term FROM search_term WHERE criterion_id=? ORDER BY rowid", (c["criterion_id"],))] for c in crit}
    con.close()
    by_no = {c["no"]: c for c in crit}

    st.markdown("**비교요소 4개**")
    if is_owner and status == "준비":
        with st.form(f"crit_{project_id}"):
            vals = {no: st.text_input(label, value=by_no[no]["name"] if no in by_no else "", key=f"cn_{project_id}_{no}")
                    for no, label in CRIT_LABELS.items()}
            st.caption("2번은 '핵심 방법'으로 정하세요. 결론 기준(채택)이 2번 판정을 봅니다. "
                       "빈칸으로 저장하면 그 비교요소는 삭제됩니다.")
            saved = st.form_submit_button("비교요소 저장")
        if saved:
            act(save_criteria, project_id, vals, me, ok="비교요소를 저장했습니다.")
    elif crit:
        st.dataframe(pd.DataFrame([{"번호": CRIT_LABELS[c["no"]], "이름": c["name"]} for c in crit]),
                     hide_index=True, width="stretch")
    else:
        st.info("등록된 비교요소가 없습니다.")

    st.markdown("**비교요소별 동의어 검색어**")
    if not crit:
        st.info("비교요소를 먼저 저장하세요.")
    elif is_owner and status != "확정":
        with st.form(f"terms_{project_id}"):
            tvals = {c["criterion_id"]: st.text_input(f"{CRIT_LABELS[c['no']]} — {c['name']}",
                                                      value=", ".join(terms[c["criterion_id"]]),
                                                      key=f"tm_{project_id}_{c['criterion_id']}")
                     for c in crit}
            st.caption("쉼표로 구분해 요소마다 3~5개를 권장합니다. 진행 후에도 고칠 수 있습니다 (확정 전까지).")
            tsaved = st.form_submit_button("검색어 저장")
        if tsaved:
            act(save_terms, project_id, tvals, me, ok="검색어를 저장했습니다.")
    else:
        st.dataframe(pd.DataFrame([{"비교요소": f"{CRIT_LABELS[c['no']]} — {c['name']}",
                                    "검색어": ", ".join(terms[c["criterion_id"]])} for c in crit]),
                     hide_index=True, width="stretch")

    st.markdown("**진행 상태**")
    if status == "준비":
        if len(crit) != 4:
            st.warning("비교요소 4개를 모두 저장해야 진행할 수 있습니다 "
                       f"(빠진 것: {', '.join(l for n, l in CRIT_LABELS.items() if n not in by_no)}).")
        elif any(not terms[c["criterion_id"]] for c in crit):
            st.warning("검색어가 없는 비교요소가 있습니다. 동의어 검색·후보 추천에서 그 요소는 적중하지 않습니다.")
        st.caption("'진행'으로 바꾸면 비교요소를 더 이상 고칠 수 없고, 그때부터 검토를 작성할 수 있습니다.")
        if st.button("진행으로 바꾸기", type="primary", disabled=not is_owner):
            act(start_project, project_id, me, ok="프로젝트를 진행 상태로 바꿨습니다.")
        with st.expander("프로젝트 삭제 (준비 상태, 후보가 없을 때만)"):
            confirm = st.checkbox("이 프로젝트를 삭제합니다", key=f"del_{project_id}")
            if st.button("삭제", disabled=not (is_owner and confirm)):
                act(_delete_and_reset, project_id, me, ok="프로젝트를 삭제했습니다.")
    elif status == "진행":
        st.success("진행 중입니다. 비교요소는 고칠 수 없고, 확정은 대시보드에서 합니다.")
    else:
        st.info("확정된 프로젝트입니다.")


def main():
    st.set_page_config(page_title="선행기술 조사·검토", layout="wide")
    if not os.path.exists(WORK_DB):
        if not os.path.exists(SRC_DB):
            st.error(f"{SRC_DB} 파일이 없습니다. app.py 와 같은 폴더에 DB 파일을 넣어주세요.")
            return
        reset_db()
    members = df("SELECT member_id, name FROM team_member ORDER BY member_id")
    if members.empty:
        st.title("선행기술 조사·검토")
        st.error("DB에 팀원이 없습니다. build_db.py 로 patent_app.db 를 만든 뒤 실행하세요.")
        return
    projects = df("SELECT project_id, title, owner_id, status FROM project ORDER BY project_id")
    names = dict(zip(members.member_id.astype(int), members.name))
    ptitle = {int(r.project_id): f"{r.project_id}. {r.title} [{r.status}]" for r in projects.itertuples()}

    with st.sidebar:
        st.title("선행기술 조사·검토")
        st.caption(f"사용 중인 DB: {os.path.basename(WORK_DB)} (원본 {os.path.basename(SRC_DB)}의 사본)")
        if "mock" in os.path.basename(SRC_DB).lower():
            st.caption("※ mock 데이터 — 프로젝트 주제·검토 숫자는 가짜입니다.")
        me = st.selectbox("현재 사용자", list(names), format_func=names.get)
        own = projects[projects.owner_id == me].project_id.astype(int).tolist()
        ids = list(ptitle)
        if st.session_state.pop("_reset_project", False):         # 선택한 프로젝트를 삭제한 직후
            st.session_state.pop(f"proj_{me}", None)
        if "_select_project" in st.session_state:                 # 방금 만든 프로젝트를 바로 선택
            st.session_state[f"proj_{me}"] = st.session_state.pop("_select_project")
        project_id = st.selectbox(
            "조사 프로젝트", ids, index=ids.index(own[0]) if own else 0, key=f"proj_{me}",   # 사용자별로 기본값 유지
            format_func=lambda i: ptitle[i] + (" ← 내 프로젝트" if i in own else "")) if ids else None
        if "page" not in st.session_state:
            st.session_state["page"] = "대시보드" if ids else "프로젝트 관리"
        page = st.radio("화면", ["대시보드", "특허 탐색", "검토", "프로젝트 관리"], key="page")

    if "flash" in st.session_state:
        st.toast(st.session_state.pop("flash"))
    if page == "프로젝트 관리":
        page_manage(me, names, projects, project_id)
    elif project_id is None:
        st.info("아직 프로젝트가 없습니다. '프로젝트 관리'에서 먼저 만드세요.")
    elif page == "대시보드":
        page_dashboard(me, names, project_id)
    elif page == "특허 탐색":
        page_search(me, names, project_id)
    else:
        page_review(me, names, projects, project_id)


if __name__ == "__main__":
    main()
