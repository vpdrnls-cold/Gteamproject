"""시연용 데모 데이터 넣기 (build_db.py 로 만든 patent_app.db 위에 실행)

    python build_db.py      # patent_app.db 생성 (스키마 + 트리거 + 팀원 5명)
    python seed_demo.py     # 데모 프로젝트 5개 + 후보 + 검토 샘플 추가

모든 데이터는 앱의 업무 함수(app2.py)를 그대로 호출해 넣으므로 트리거 규칙 4~10을 모두 통과한 상태입니다.
검토 판정(○△X)과 근거는 시연용 샘플이며 실제 판단이 아닙니다. 프로젝트 제목에는 TAG 가 붙습니다.
"""
import sqlite3
import sys

import app2
from app2 import (RuleError, create_project, save_criteria, save_terms, start_project,
                  register_candidate, save_review, decide, criteria_of)

DB = sys.argv[1] if len(sys.argv) > 1 else "patent_app.db"
TAG = "[데모] "

# 담당자 이름 → (프로젝트 주제, 비교요소 4개, 비교요소별 검색어, 후보 탐색 키워드)
DEMO = {
    "김연준": ("마스크 착용 얼굴 인식",
             ["해결 문제: 마스크 착용 얼굴 인식", "핵심 방법: 신경망", "입력 데이터: 얼굴 이미지", "적용 분야: 출입 보안"],
             ["얼굴 인식, face recognition, 마스크", "신경망, neural network, 딥러닝, CNN", "얼굴 이미지, face image, 영상", "출입, 보안, 인증"],
             ["얼굴", "face"]),
    "최재원": ("운전자 졸음 감지",
             ["해결 문제: 운전자 졸음 감지", "핵심 방법: 신경망", "입력 데이터: 운전자 영상", "적용 분야: 차량 안전"],
             ["졸음, drowsiness, 피로", "신경망, 딥러닝, CNN", "운전자 영상, 눈 깜빡임", "차량, 운전, 안전"],
             ["운전", "졸음", "driver"]),
    "남건희": ("필기 문서 인식",
             ["해결 문제: 필기 문서 인식", "핵심 방법: 신경망", "입력 데이터: 문서 이미지", "적용 분야: 문서 디지털화"],
             ["필기, handwriting, 문자 인식", "신경망, 딥러닝, CNN", "문서 이미지, 스캔", "문서, 디지털화, OCR"],
             ["필기", "문자", "handwrit", "문서"]),
    "양재훈": ("불량 검사 자동화",
             ["해결 문제: 제품 불량 검출", "핵심 방법: 신경망", "입력 데이터: 제품 이미지", "적용 분야: 제조 공정"],
             ["불량, 결함, defect", "신경망, 딥러닝, CNN", "제품 이미지, 검사 영상", "제조, 공정, 생산"],
             ["불량", "결함", "defect", "검사"]),
    "박채연": ("보행자 행동 예측",
             ["해결 문제: 보행자 행동 예측", "핵심 방법: 신경망", "입력 데이터: 주행 영상", "적용 분야: 자율주행"],
             ["보행자, pedestrian, 행동 예측", "신경망, LSTM, 딥러닝", "주행 영상, 센서, 궤적", "자율주행, 차량, 안전"],
             ["보행자", "pedestrian", "행동"]),
}
N_CAND = 10
USABLE = "LENGTH(TRIM(abstract)) >= 20 AND TRIM(abstract) <> TRIM(title_ko)"


def guide(marks):
    """검토 가이드 기준 결론: ○ 3개 이상 + 핵심 방법(2번) ○ → 채택, ○ 1개 이하 → 제외, 나머지 보류"""
    o = [m == "○" for m in marks]
    if sum(o) >= 3 and o[1]:
        return "채택"
    return "제외" if sum(o) <= 1 else "보류"


def submit(con, cand, member, marks, crits, round_note="시연용 샘플 판정"):
    m = {c["criterion_id"]: (mk, "시연용 샘플 근거") for c, mk in zip(crits, marks)}
    save_review(con, cand, member, m, guide(marks), note=round_note, submit=True)


ADOPT, EXCLUDE, MIXED = ["○"] * 4, ["X"] * 4, ["○", "○", "△", "X"]


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    if con.execute("SELECT COUNT(*) FROM project").fetchone()[0]:
        sys.exit("이미 프로젝트가 있습니다. build_db.py 로 새 patent_app.db 를 만든 뒤 실행하세요.")
    ids = {r["name"]: r["member_id"] for r in con.execute("SELECT member_id, name FROM team_member")}
    names = list(ids)
    if not set(DEMO) <= set(ids):
        sys.exit(f"팀원 이름이 DEMO 와 다릅니다: {sorted(set(DEMO) - set(ids))}")

    for owner, (title, crit_names, terms, kws) in DEMO.items():
        oid = ids[owner]
        pid = create_project(con, TAG + title, oid)
        save_criteria(con, pid, dict(enumerate(crit_names, 1)), oid)
        crits = criteria_of(con, pid)
        save_terms(con, pid, {c["criterion_id"]: t for c, t in zip(crits, terms)}, oid)
        start_project(con, pid, oid)

        # 후보: 키워드가 제목·요약에 들어간 특허를 번호 순으로 (부족하면 신경망 특허로 채움)
        pick = []
        for kw in kws + ["신경망"]:
            for r in con.execute(f"SELECT patent_id FROM patent WHERE {USABLE} AND (title_ko LIKE ? OR abstract LIKE ?) "
                                 "ORDER BY patent_id", (f"%{kw}%", f"%{kw}%")):
                if r[0] not in pick:
                    pick.append(r[0])
            if len(pick) >= N_CAND:
                break
        pick = pick[:N_CAND]

        # 검토자 2명: 담당자 다음 순서의 두 명
        i = names.index(owner)
        revs = [ids[names[(i + 1) % 5]], ids[names[(i + 2) % 5]]]
        cands = [register_candidate(con, pid, p, revs, by_member=oid) for p in pick]

        for k, c in enumerate(cands):
            a, b = revs
            if k in (0, 1, 2):                       # 두 검토자 결론 일치 → 결론 확정
                marks = [ADOPT, EXCLUDE, ADOPT][k]
                submit(con, c, a, marks, crits)
                submit(con, c, b, marks, crits)
            elif k == 3:                             # 1차 불일치 → 재검토 → 2차에서도 불일치 → 담당자 결정
                submit(con, c, a, ADOPT, crits)
                submit(con, c, b, EXCLUDE, crits)
                submit(con, c, a, ADOPT, crits)
                submit(con, c, b, EXCLUDE, crits)
                decide(con, c, oid, "채택", "시연용: 핵심 방법이 명확해 채택")
            elif k == 4:                             # 한 명만 제출 → 검토중
                submit(con, c, a, MIXED, crits)
            # 나머지는 미검토

    con.commit()
    print(f"데모 데이터 완료: 프로젝트 {con.execute('SELECT COUNT(*) FROM project').fetchone()[0]}개, "
          f"후보 {con.execute('SELECT COUNT(*) FROM candidate').fetchone()[0]}건")
    for r in con.execute("SELECT status, COUNT(*) n FROM candidate GROUP BY status"):
        print(f"  {r['status']}: {r['n']}건")


if __name__ == "__main__":
    try:
        main()
    except RuleError as e:
        sys.exit(f"규칙 위반: {e}")
