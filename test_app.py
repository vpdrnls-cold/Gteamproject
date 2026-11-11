"""app.py 시험 — mock DB 사본 위에서만 실행 (원본 patent_mock.db 는 바뀌지 않음)

실행:  python test_app.py        (또는 pytest -q test_app.py)
"""
import os
import shutil
import sqlite3
import tempfile

import pytest

import app

SRC = os.environ.get("PATENT_DB_SRC", os.path.join(os.path.dirname(os.path.abspath(__file__)), "patent_mock.db"))


@pytest.fixture()
def con():
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    shutil.copyfile(SRC, tmp)
    c = app.connect(tmp)
    yield c
    c.close()


def free_patents(con, project_id, n):
    return [r[0] for r in con.execute(
        "SELECT patent_id FROM patent WHERE patent_id NOT IN "
        "(SELECT patent_id FROM candidate WHERE project_id=?) LIMIT ?", (project_id, n))]


def marks(con, project_id, mark="○"):
    return {c["criterion_id"]: (mark, "근거") for c in app.criteria_of(con, project_id)}


def status(con, cid):
    return dict(con.execute("SELECT status, final_conclusion, decided_by FROM candidate WHERE candidate_id=?",
                            (cid,)).fetchone())


def new_cand(con, pid=1, reviewers=(2, 3)):
    return app.register_candidate(con, pid, free_patents(con, pid, 1)[0], reviewers)


# ───────── 후보 등록 (규칙 2·4) ─────────
def test_register_ok_and_rules(con):
    cid = new_cand(con)
    assert con.execute("SELECT COUNT(*) FROM candidate_reviewer WHERE candidate_id=?", (cid,)).fetchone()[0] == 2
    pat = free_patents(con, 1, 1)[0]
    with pytest.raises(app.RuleError, match="담당자"):           # 담당자(1)를 검토자로
        app.register_candidate(con, 1, pat, [1, 2])
    with pytest.raises(app.RuleError, match="2명"):              # 같은 사람 두 번
        app.register_candidate(con, 1, pat, [2, 2])
    with pytest.raises(app.RuleError, match="2명"):              # 1명만
        app.register_candidate(con, 1, pat, [2])
    before = con.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
    assert before == 51                                          # 거부된 시도는 후보를 남기지 않음


def test_only_owner_registers(con):
    pat = free_patents(con, 1, 1)[0]
    with pytest.raises(app.RuleError, match="담당자만"):
        app.register_candidate(con, 1, pat, [3, 4], by_member=2)       # 프로젝트 1의 담당자는 1
    app.register_candidate(con, 1, pat, [3, 4], by_member=1)


def test_duplicate_candidate_rejected_by_db(con):
    pat = con.execute("SELECT patent_id FROM candidate WHERE project_id=1 LIMIT 1").fetchone()[0]
    n = con.execute("SELECT COUNT(*) FROM candidate_reviewer").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):                   # UNIQUE(project, patent)
        app.register_candidate(con, 1, pat, [2, 3])
    assert con.execute("SELECT COUNT(*) FROM candidate_reviewer").fetchone()[0] == n   # 롤백 확인


# ───────── 검토 작성·제출 (규칙 4~8) ─────────
def test_unassigned_reviewer_rejected(con):
    cid = new_cand(con)
    with pytest.raises(app.RuleError, match="배정된"):
        app.save_review(con, cid, 4, marks(con, 1), "채택", submit=True)


def test_draft_edit_delete_then_submit_lock(con):
    cid = new_cand(con)
    m = marks(con, 1)
    app.save_review(con, cid, 2, dict(list(m.items())[:2]), None)                  # 2개만 임시저장
    assert con.execute("SELECT COUNT(*) FROM review_item").fetchone()[0] == 504 + 2
    app.save_review(con, cid, 2, m, "채택")                                         # 수정(4개)
    assert con.execute("SELECT status FROM review WHERE candidate_id=? AND member_id=2", (cid,)).fetchone()[0] == "DRAFT"
    assert status(con, cid)["status"] == "미검토"                                    # 임시저장은 상태를 안 바꿈
    app.delete_draft(con, cid, 2)                                                   # 임시 검토 삭제
    assert con.execute("SELECT COUNT(*) FROM review WHERE candidate_id=?", (cid,)).fetchone()[0] == 0
    app.save_review(con, cid, 2, m, "채택", submit=True)
    assert status(con, cid)["status"] == "검토중"
    with pytest.raises(app.RuleError, match="제출된"):                              # 제출 후 수정 불가
        app.save_review(con, cid, 2, m, "제외")
    with pytest.raises(app.RuleError, match="제출된"):                              # 제출 후 삭제 불가
        app.delete_draft(con, cid, 2)


def test_submit_needs_all_criteria_and_nothing_left_behind(con):
    cid = new_cand(con)
    m3 = dict(list(marks(con, 1).items())[:3])
    with pytest.raises(app.RuleError, match="모두"):
        app.save_review(con, cid, 2, m3, "채택", submit=True)
    with pytest.raises(app.RuleError, match="결론"):
        app.save_review(con, cid, 2, marks(con, 1), None, submit=True)
    assert con.execute("SELECT COUNT(*) FROM review WHERE candidate_id=?", (cid,)).fetchone()[0] == 0


def test_criterion_of_other_project_rejected(con):
    cid = new_cand(con)
    other = marks(con, 2)                                                           # 프로젝트 2의 비교요소
    with pytest.raises(app.RuleError, match="비교요소"):
        app.save_review(con, cid, 2, other, "채택")


def test_auto_conclusion_when_same(con):
    cid = new_cand(con)
    app.save_review(con, cid, 2, marks(con, 1), "채택", submit=True)
    app.save_review(con, cid, 3, marks(con, 1), "채택", submit=True)
    assert status(con, cid) == {"status": "결론", "final_conclusion": "채택", "decided_by": None}
    with pytest.raises(app.RuleError, match="결론"):                                # 결론 후 수정 불가
        app.save_review(con, cid, 2, marks(con, 1), "제외")


def test_disagree_then_round2_then_owner_decides(con):
    cid = new_cand(con)
    app.save_review(con, cid, 2, marks(con, 1), "채택", submit=True)
    app.save_review(con, cid, 3, marks(con, 1), "제외", submit=True)
    assert status(con, cid)["status"] == "재검토"
    app.save_review(con, cid, 2, marks(con, 1), "채택", submit=True)                # 2차
    assert con.execute("SELECT MAX(round) FROM review WHERE candidate_id=?", (cid,)).fetchone()[0] == 2
    assert status(con, cid)["status"] == "재검토"                                    # 한 명만 제출
    app.save_review(con, cid, 3, marks(con, 1), "제외", submit=True)
    assert status(con, cid)["status"] == "담당자결정"
    with pytest.raises(app.RuleError, match="담당자만"):
        app.decide(con, cid, 2, "채택", "사유")
    with pytest.raises(app.RuleError, match="사유"):
        app.decide(con, cid, 1, "채택", "  ")
    with pytest.raises(app.RuleError, match="채택/제외"):
        app.decide(con, cid, 1, "보류", "사유")
    app.decide(con, cid, 1, "채택", "핵심 방법이 같음")
    assert status(con, cid) == {"status": "결론", "final_conclusion": "채택", "decided_by": 1}


def test_hold_goes_to_recheck(con):
    cid = new_cand(con)
    app.save_review(con, cid, 2, marks(con, 1), "보류", submit=True)
    app.save_review(con, cid, 3, marks(con, 1), "보류", submit=True)
    assert status(con, cid)["status"] == "재검토"                                    # 보류가 있으면 자동 결론 아님


# ───────── 프로젝트 확정 (규칙 10) ─────────
def test_finalize(con):
    with pytest.raises(app.RuleError, match="담당자만"):
        app.finalize_project(con, 1, 2)
    with pytest.raises(app.RuleError, match="건 있어"):                               # mock 에는 막는 후보가 있음
        app.finalize_project(con, 1, 1)
    # 막는 후보를 모두 풀어준 상황을 만든다
    con.execute("UPDATE candidate SET status='결론', final_conclusion='채택' WHERE project_id=1")
    for (cid,) in con.execute("SELECT candidate_id FROM candidate WHERE project_id=1").fetchall():
        have = {r[0] for r in con.execute("SELECT member_id FROM candidate_reviewer WHERE candidate_id=?", (cid,))}
        for m in (2, 3, 4, 5):
            if len(have) < 2 and m not in have:
                con.execute("INSERT INTO candidate_reviewer VALUES (?,?)", (cid, m)); have.add(m)
    con.commit()
    app.finalize_project(con, 1, 1)
    assert con.execute("SELECT status FROM project WHERE project_id=1").fetchone()[0] == "확정"
    with pytest.raises(app.RuleError, match="확정"):                                  # 확정 후 수정 불가
        new_cand(con)


def test_finalize_without_candidates(con):
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("DELETE FROM candidate WHERE project_id=1")
    con.commit()
    with pytest.raises(app.RuleError, match="후보가 없어"):
        app.finalize_project(con, 1, 1)


# ───────── 조회 쿼리 ─────────
def test_queries_run(con):
    p = app.df(app.SQL_PROGRESS, path=con.execute("PRAGMA database_list").fetchone()[2])
    assert len(p) == 5 and p["후보수"].sum() == 50
    path = con.execute("PRAGMA database_list").fetchone()[2]
    assert not app.df(app.SQL_TODO, (2,), path=path).empty
    assert len(app.df(app.SQL_BLOCKERS, (3,), path=path)) > 0
    crit = app.criteria_of(con, 1)[0]["criterion_id"]
    s = app.df(app.SQL_SEARCH, {"c": crit, "pid": 1, "f": None, "t": None, "a": None, "s": None, "lim": 10}, path=path)
    assert 0 < len(s) <= 10
    s2 = app.df(app.SQL_SEARCH, {"c": crit, "pid": 1, "f": "2016-01-01", "t": None, "a": None, "s": "G06V", "lim": 10},
                path=path)
    assert (s2["출원일"] >= "2016-01-01").all()
    r = app.df(app.SQL_RECOMMEND, {"pid": 1, "lim": 10}, path=path)
    assert len(r) == 10 and r["적중_요소수"].is_monotonic_decreasing
    cand = {x[0] for x in con.execute("SELECT patent_id FROM candidate WHERE project_id=1")}
    assert not cand & set(r["patent_id"])                                             # 이미 후보인 특허 제외
    blk = app.df(app.SQL_BLOCKERS, (3,), path=path)
    assert con.execute(app.SQL_BLOCKER_COUNT, (3,)).fetchone()[0] == len(blk)       # 개수 쿼리가 목록과 일치


def test_my_progress_sql_and_drop_on_recheck(con):
    path = con.execute("PRAGMA database_list").fetchone()[2]

    def prog(m):
        r = app.df(app.SQL_MY_PROGRESS, (m,), path=path).iloc[0]
        return int(r["배정"]), int(r["완료"] or 0)

    a2, d2 = prog(2)
    a3, d3 = prog(3)
    cid = new_cand(con)                                                              # 검토자 2, 3
    assert prog(2) == (a2 + 1, d2) and prog(3) == (a3 + 1, d3)                       # 배정만 늘고 완료는 그대로
    app.save_review(con, cid, 2, marks(con, 1), "채택", submit=True)
    assert prog(2) == (a2 + 1, d2 + 1)                                               # 내가 제출 → 완료 +1
    app.save_review(con, cid, 3, marks(con, 1), "제외", submit=True)                  # 갈림 → 재검토(2차)
    assert prog(2) == (a2 + 1, d2) and prog(3) == (a3 + 1, d3)                       # 다시 내 할 일이 생겨 내려감
    app.save_review(con, cid, 2, marks(con, 1), "채택", submit=True)
    app.save_review(con, cid, 3, marks(con, 1), "채택", submit=True)                  # 2차 일치 → 결론
    assert prog(2) == (a2 + 1, d2 + 1) and prog(3) == (a3 + 1, d3 + 1)


def test_paths_are_absolute():
    assert os.path.isabs(app.SRC_DB) and os.path.isabs(app.WORK_DB)


# ───────── 검색어 매칭 규칙 (노트북 5번과 같은 방식) ─────────
def test_match_rules():
    m = sqlite3.connect(":memory:")
    m.execute("CREATE TABLE patent(id INTEGER, title_ko TEXT, title_en TEXT, abstract TEXT)")
    m.execute("CREATE TABLE tt(term TEXT)")
    m.executemany("INSERT INTO patent VALUES (?,?,?,?)", [
        (1, "사용자 인터페이스", "User interface device", "a user interface"),
        (2, "얼굴 인식 장치", "Face recognition device", "이 발명은 (face) 인식"),
        (3, "표면 처리", "Surface treatment", "surface multifaceted interface"),
        (4, "CNN 기반", "x", "CNN을 이용한 방법"),
        (5, "딥러닝 모델", "deep model", "딥러닝으로 학습"),
        (6, "제목", "Deep learning model", "내용 없음"),
        (7, "제목", None, "faces are detected"),
        (8, "x", "딥러닝 english", "y"),
    ])

    def hits(term):
        rows = m.execute(f"SELECT DISTINCT p.id FROM patent p JOIN tt t ON {app._MATCH} WHERE t.term = ?",
                         (term,)).fetchall()
        m.execute("DELETE FROM tt")
        return {r[0] for r in rows}

    for term in ("face", "FACE", "cnn", "딥 러닝", "딥러닝"):
        m.execute("INSERT INTO tt VALUES (?)", (term,))      # hits() 가 끝나면 tt 를 비우므로 하나씩 넣고 확인
        if term in ("face", "FACE"):
            assert hits(term) == {2, 7}            # interface·surface·multifaceted 제외, (face)·faces 포함
        elif term == "cnn":
            assert hits(term) == {4}               # 한글 조사가 붙은 CNN을 → 적중
        else:
            assert hits(term) == {5}               # 띄어쓰기 무시, 영문 명칭(8번)은 보지 않음


# ───────── 화면 (Streamlit AppTest) ─────────
def run_ui(tmp, page, member=None, project=None):
    from streamlit.testing.v1 import AppTest
    os.environ["PATENT_DB"] = tmp
    at = AppTest.from_file("app.py", default_timeout=60).run()
    if member:
        at.sidebar.selectbox[0].set_value(member).run()
    if project:
        at.sidebar.selectbox[1].set_value(project).run()
    at.sidebar.radio[0].set_value(page).run()
    return at


def click_row(at, prefix, row=0):
    """표의 행 클릭을 흉내: 표 위젯의 선택 상태(session_state)를 직접 넣고 다시 실행"""
    import re
    key = [k for k in at.session_state.keys() if re.fullmatch(prefix + r"_[0-9a-f]{10}", k)][0]   # rec_lim 같은 다른 위젯 제외
    at.session_state[key] = {"selection": {"rows": [row], "columns": [], "cells": []}}
    return at.run()


def fresh_db():
    tmp = os.path.join(tempfile.mkdtemp(), "ui.db")
    shutil.copyfile(SRC, tmp)
    return tmp


def search_rows(tmp, project_id=1):
    """특허 탐색 화면 기본값(첫 비교요소, 조건 없음, 20건)과 같은 결과"""
    c = app.connect(tmp)
    cid = app.criteria_of(c, project_id)[0]["criterion_id"]
    c.close()
    res = app.df(app.SQL_SEARCH, {"c": cid, "pid": project_id, "f": None, "t": None, "a": None, "s": None,
                                  "lim": 20}, path=tmp)
    res["등록됨"] = res["등록됨"].astype(bool)
    return res


@pytest.mark.parametrize("page", ["대시보드", "특허 탐색", "검토"])
def test_ui_pages_render(page):
    at = run_ui(fresh_db(), page)
    assert not at.exception, at.exception
    assert not at.error
    assert not at.warning, [w.value for w in at.warning]                                # 'deprecated' 같은 경고도 없어야 함
    assert len(at.sidebar.radio[0].options) == 3                                       # 분석 메뉴는 없음


def test_ui_default_project_follows_user():
    at = run_ui(fresh_db(), "대시보드")
    assert at.sidebar.selectbox[1].value == 1                                         # 김연준 → 프로젝트 1
    at.sidebar.selectbox[0].set_value(4).run()
    assert at.sidebar.selectbox[1].value == 4                                         # 양재훈 → 프로젝트 4


def test_ui_dashboard_shows_my_progress():
    tmp = fresh_db()
    at = run_ui(tmp, "대시보드", member=2)
    assert not at.exception
    r = app.df(app.SQL_MY_PROGRESS, (2,), path=tmp).iloc[0]
    assert any(f"{int(r['완료'])}/{int(r['배정'])}건" in str(e.proto.text) for e in at.get("progress")), \
        [e.proto.text for e in at.get("progress")]


# ── 특허 탐색: 표 클릭 → 등록 ──
def test_ui_search_click_row_then_register():
    tmp = fresh_db()
    res = search_rows(tmp)
    idx = int(res.index[~res["등록됨"]][0])
    pat = res.loc[idx]
    at = run_ui(tmp, "특허 탐색", member=1, project=1)
    assert not at.exception
    assert not [b for b in at.button if b.label == "등록"]                              # 클릭 전에는 등록 버튼 없음
    click_row(at, "res", idx)
    assert any(pat["출원번호"] in m.value for m in at.markdown), "선택한 특허가 등록 상자에 떠야 함"
    [m for m in at.multiselect if m.label.startswith("검토자")][0].set_value([2, 3]).run()
    [b for b in at.button if b.label == "등록"][0].click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    chk = app.connect(tmp)
    assert chk.execute("SELECT COUNT(*) FROM candidate WHERE project_id=1").fetchone()[0] == 11
    assert chk.execute("SELECT COUNT(*) FROM candidate WHERE project_id=1 AND patent_id=?",
                       (int(pat["patent_id"]),)).fetchone()[0] == 1
    chk.close()


def test_ui_register_needs_two_reviewers():
    tmp = fresh_db()
    res = search_rows(tmp)
    at = run_ui(tmp, "특허 탐색", member=1, project=1)
    click_row(at, "res", int(res.index[~res["등록됨"]][0]))
    [b for b in at.button if b.label == "등록"][0].click().run()                        # 검토자를 안 골랐음
    assert any("2명" in e.value for e in at.error), [e.value for e in at.error]


def test_ui_already_registered_row_warns():
    tmp = fresh_db()
    res = search_rows(tmp)
    idx = int(res.index[~res["등록됨"]][0])
    c = app.connect(tmp)
    app.register_candidate(c, 1, int(res.loc[idx, "patent_id"]), [2, 3])
    c.close()
    at = run_ui(tmp, "특허 탐색", member=1, project=1)
    click_row(at, "res", idx)
    assert any("이미 이 프로젝트의 후보" in w.value for w in at.warning)
    assert not [b for b in at.button if b.label == "등록"]


def test_ui_recommend_click_row_opens_register():
    tmp = fresh_db()
    at = run_ui(tmp, "특허 탐색", member=1, project=1)
    click_row(at, "rec", 0)
    assert any("선택한 특허" in m.value for m in at.markdown)


def test_ui_non_owner_cannot_register():
    at = run_ui(fresh_db(), "특허 탐색", member=2, project=1)                           # 최재원이 남의 프로젝트(1)를 봄
    assert not at.exception
    assert not [b for b in at.button if b.label == "등록"]                              # 등록 버튼 자체가 없음
    assert any("담당자" in i.value for i in at.info), [i.value for i in at.info]


# ── 검토: 표 클릭 → 양식 ──
def todo_index(tmp, member, cid):
    todo = app.df(app.SQL_TODO, (member,), path=tmp)
    return int(todo.index[todo.candidate_id == cid][0])


def test_ui_review_click_row_submit_and_rule_error():
    tmp = fresh_db()
    c = app.connect(tmp)
    cid = new_cand(c)                                                                 # 검토자 2,3
    c.close()
    at = run_ui(tmp, "검토", member=2)
    assert not [b for b in at.button if b.label == "제출"]                              # 클릭 전에는 양식 없음
    click_row(at, "todo", todo_index(tmp, 2, cid))
    # 1) 판정 없이 제출 → 규칙 위반 메시지, DB 변화 없음
    [b for b in at.button if b.label == "제출"][0].click().run()
    assert any("모두" in e.value for e in at.error), [e.value for e in at.error]
    chk = app.connect(tmp)
    assert chk.execute("SELECT COUNT(*) FROM review WHERE candidate_id=?", (cid,)).fetchone()[0] == 0
    chk.close()
    # 2) 4개 판정 + 결론을 채우고 제출 → 후보 '검토중'
    for r in at.radio:
        if r.label == "판정":
            r.set_value("○")
    [s for s in at.selectbox if s.label == "결론"][0].set_value("채택")
    [b for b in at.button if b.label == "제출"][0].click().run()
    assert not at.exception and not at.error
    chk = app.connect(tmp)
    assert status(chk, cid)["status"] == "검토중"
    assert chk.execute("SELECT status FROM review WHERE candidate_id=? AND member_id=2", (cid,)).fetchone()[0] == "SUBMITTED"
    chk.close()


def test_ui_draft_save_keeps_form_open():
    tmp = fresh_db()
    c = app.connect(tmp)
    cid = new_cand(c)
    c.close()
    at = run_ui(tmp, "검토", member=2)
    click_row(at, "todo", todo_index(tmp, 2, cid))
    [r for r in at.radio if r.label == "판정"][0].set_value("○")
    [b for b in at.button if b.label == "임시저장"][0].click().run()
    assert not at.exception and not at.error, [e.value for e in at.error]
    chk = app.connect(tmp)
    assert chk.execute("SELECT status FROM review WHERE candidate_id=? AND member_id=2", (cid,)).fetchone()[0] == "DRAFT"
    chk.close()
    assert [b for b in at.button if b.label == "제출"], "임시저장 뒤에도 같은 후보의 양식이 열려 있어야 함"


def test_ui_round2_shows_my_first_review():
    tmp = fresh_db()
    c = app.connect(tmp)
    cid = new_cand(c)
    app.save_review(c, cid, 2, marks(c, 1, "○"), "채택", submit=True)
    app.save_review(c, cid, 3, marks(c, 1, "X"), "제외", submit=True)                   # 갈림 → 재검토(2차)
    c.close()
    at = run_ui(tmp, "검토", member=2)
    click_row(at, "todo", todo_index(tmp, 2, cid))
    assert not at.exception
    labels = [e.label for e in at.expander]
    assert "내 1차 검토 (참고용)" in labels, labels
    assert any("2차 검토" in m.value for m in at.markdown)
    assert any("채택" in str(c_.value) for c_ in at.caption)                            # 내 1차 결론 표시


def test_ui_round1_has_no_first_review_box():
    tmp = fresh_db()
    c = app.connect(tmp)
    cid = new_cand(c)
    c.close()
    at = run_ui(tmp, "검토", member=2)
    click_row(at, "todo", todo_index(tmp, 2, cid))
    assert "내 1차 검토 (참고용)" not in [e.label for e in at.expander]


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", __file__]))
