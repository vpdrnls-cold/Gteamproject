"""시험용 DB 만들기 (make_mock_db.py) · 담당: 양재훈
patent_final.db를 복사해 patent_mock.db를 만들고, 업무 테이블(가정 스키마)과
**가짜 검토 데이터**를 넣는다. 조회 쿼리가 문법·로직상 맞게 도는지 확인하는 용도이며,
결과 숫자는 실제 검토 결과 아님. (원본 patent_final.db는 수정X)

- 업무 테이블 열 이름은 연준님 '트리거 설계안.txt· 건희님과 맞출 열' 기준.
  건희님 최종 스키마가 나오면 그쪽 schema로 교체
- 상태 변화(미검토→검토중→재검토→담당자결정→결론)는 트리거 대신 파이썬에서 규칙 8대로 흉내
실행 코드 : python make_mock_db.py patent_final.db patent_mock.db
"""

import sqlite3, shutil, random, sys

SRC = sys.argv[1] if len(sys.argv) > 1 else 'patent_final.db'
DST = sys.argv[2] if len(sys.argv) > 2 else 'patent_mock.db'
shutil.copyfile(SRC, DST)
rng = random.Random(42)
con = sqlite3.connect(DST)
con.execute('PRAGMA foreign_keys = ON')

con.executescript('''
CREATE TABLE team_member (
  member_id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT);
CREATE TABLE project (
  project_id INTEGER PRIMARY KEY,
  owner_id   INTEGER NOT NULL REFERENCES team_member(member_id),
  title      TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT '준비' CHECK (status IN ('준비','진행','확정')));
CREATE TABLE criterion (
  criterion_id INTEGER PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES project(project_id),
  no           INTEGER NOT NULL CHECK (no BETWEEN 1 AND 4),
  name         TEXT NOT NULL,
  UNIQUE (project_id, no));
CREATE TABLE search_term (
  term_id      INTEGER PRIMARY KEY,
  criterion_id INTEGER NOT NULL REFERENCES criterion(criterion_id),
  term         TEXT NOT NULL,
  UNIQUE (criterion_id, term));
CREATE TABLE candidate (
  candidate_id     INTEGER PRIMARY KEY,
  project_id       INTEGER NOT NULL REFERENCES project(project_id),
  patent_id        INTEGER NOT NULL REFERENCES patent(patent_id),
  priority         INTEGER CHECK (priority BETWEEN 1 AND 5),
  status           TEXT NOT NULL DEFAULT '미검토'
                   CHECK (status IN ('미검토','검토중','재검토','담당자결정','결론')),
  final_conclusion TEXT CHECK (final_conclusion IN ('채택','제외')),
  decided_by       INTEGER REFERENCES team_member(member_id),
  decision_note    TEXT,
  UNIQUE (project_id, patent_id));
CREATE TABLE candidate_reviewer (
  candidate_id INTEGER NOT NULL REFERENCES candidate(candidate_id),
  member_id    INTEGER NOT NULL REFERENCES team_member(member_id),
  PRIMARY KEY (candidate_id, member_id));
CREATE TABLE review (
  review_id    INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  member_id    INTEGER NOT NULL,
  round        INTEGER NOT NULL CHECK (round IN (1,2)),
  status       TEXT NOT NULL CHECK (status IN ('DRAFT','SUBMITTED')),
  conclusion   TEXT CHECK (conclusion IN ('채택','제외','보류')),
  note         TEXT,
  FOREIGN KEY (candidate_id, member_id) REFERENCES candidate_reviewer(candidate_id, member_id),
  UNIQUE (candidate_id, member_id, round));
CREATE TABLE review_item (
  review_id    INTEGER NOT NULL REFERENCES review(review_id),
  criterion_id INTEGER NOT NULL REFERENCES criterion(criterion_id),
  mark         TEXT NOT NULL CHECK (mark IN ('○','△','X')),
  evidence     TEXT,
  PRIMARY KEY (review_id, criterion_id));
''')

members = ['김연준', '최재원', '남건희', '양재훈', '박채연']
con.executemany('INSERT INTO team_member(member_id, name) VALUES (?,?)', list(enumerate(members, 1)))

# 프로젝트 5개 (담당자 = 팀원 1명씩). 비교요소 4개 + 동의어
PROJECTS = [
  ('마스크 착용 얼굴 인식', [('해결 문제: 가려진 얼굴 인식', ['얼굴 인식', '안면 인식', '얼굴 검출', 'face']),
                         ('핵심 방법: 합성곱 신경망', ['합성곱', '컨볼루션', 'CNN', '신경망']),
                         ('입력 데이터: 얼굴 영상', ['얼굴 영상', '얼굴 이미지', '안면 영상']),
                         ('적용 분야: 출입 통제', ['출입', '보안', '인증'])]),
  ('운전자 졸음 감지',      [('해결 문제: 졸음·부주의 감지', ['졸음', '부주의', '피로']),
                         ('핵심 방법: 딥러닝', ['딥러닝', '딥 러닝', '신경망', '뉴럴']),
                         ('입력 데이터: 운전자 영상', ['운전자', '눈 깜빡', '시선']),
                         ('적용 분야: 차량', ['차량', '자동차', '운전'])]),
  ('필기 문자 인식',        [('해결 문제: 손글씨 인식', ['필기', '손글씨', '문자 인식']),
                         ('핵심 방법: 순환 신경망', ['순환 신경망', 'RNN', 'LSTM', '신경망']),
                         ('입력 데이터: 문서 이미지', ['문서', '이미지', '스캔']),
                         ('적용 분야: 문서 처리', ['문서', '전자', 'OCR'])]),
  ('불량 검사 자동화',      [('해결 문제: 제품 결함 검출', ['결함', '불량', '검사']),
                         ('핵심 방법: 기계학습', ['기계 학습', '머신 러닝', '학습 모델', '신경망']),
                         ('입력 데이터: 제품 영상', ['영상', '이미지', '카메라']),
                         ('적용 분야: 제조', ['제조', '공정', '생산'])]),
  ('보행자 행동 예측',      [('해결 문제: 보행자 움직임 예측', ['보행자', '행동 예측', '움직임']),
                         ('핵심 방법: 심층 신경망', ['심층 신경망', '딥러닝', '신경망']),
                         ('입력 데이터: 도로 영상', ['도로', '영상', '카메라']),
                         ('적용 분야: 자율주행', ['자율 주행', '자율주행', '차량'])]),
]
crit_ids = {}
for pid, (title, crits) in enumerate(PROJECTS, 1):
    con.execute('INSERT INTO project VALUES (?,?,?,?)', (pid, pid, title, '진행'))
    for no, (name, terms) in enumerate(crits, 1):
        cid = con.execute('INSERT INTO criterion(project_id, no, name) VALUES (?,?,?)', (pid, no, name)).lastrowid
        crit_ids[(pid, no)] = cid
        con.executemany('INSERT INTO search_term(criterion_id, term) VALUES (?,?)', [(cid, t) for t in terms])

# 후보: 비교요소 1의 검색어에 걸린 특허 중 10건
def guide_conclusion(marks):              # 검토 가이드 3장 기준
    n_o = sum(m == '○' for m in marks)
    if n_o >= 3 and marks[1] == '○': return '채택'
    if n_o <= 1: return '제외'
    return '보류'

def noisy(truth, p):                     # 확률 p로 진짜 판정, 아니면 다른 판정
    return [t if rng.random() < p else rng.choice([m for m in '○△X' if m != t]) for t in truth]

def submit(cand, member, rnd, marks, status='SUBMITTED', deviate=0.1):
    concl = guide_conclusion(marks)
    if rng.random() < deviate:
        concl = rng.choice([c for c in ('채택', '제외', '보류') if c != concl])
    rid = con.execute('INSERT INTO review(candidate_id, member_id, round, status, conclusion) VALUES (?,?,?,?,?)',
                      (cand, member, rnd, status, concl)).lastrowid
    for no, m in enumerate(marks, 1):
        con.execute('INSERT INTO review_item VALUES (?,?,?,?)',
                    (rid, crit_ids[(pid, no)], m, f'요약 {rng.randint(1,4)}번째 문장'))
    return concl

for pid in range(1, 6):
    cid1 = crit_ids[(pid, 1)]
    pats = [r[0] for r in con.execute('''
        SELECT DISTINCT p.patent_id FROM patent p JOIN search_term t
          ON p.title_ko LIKE '%'||t.term||'%' OR p.abstract LIKE '%'||t.term||'%'
        WHERE t.criterion_id = ? ORDER BY COALESCE(p.cited_count,0) DESC LIMIT 40''', (cid1,))]
    others = [m for m in range(1, 6) if m != pid]
    for k, pat in enumerate(rng.sample(pats, 10)):
        cand = con.execute('INSERT INTO candidate(project_id, patent_id, priority) VALUES (?,?,?)',
                           (pid, pat, rng.randint(1, 5))).lastrowid
        r1, r2 = rng.sample(others, 2)
        if k == 9 and pid == 3:                       # 검토자 1명만 배정된 후보(확정 불가 사례)
            con.execute('INSERT INTO candidate_reviewer VALUES (?,?)', (cand, r1)); continue
        con.executemany('INSERT INTO candidate_reviewer VALUES (?,?)', [(cand, r1), (cand, r2)])
        stage = rng.random()
        if stage < 0.08:                              # 미검토
            continue
        truth = [rng.choices('○△X', weights=w)[0] for w in ([5,3,2], [5,3,2], [6,2,2], [4,3,3])]
        if stage < 0.18:                              # 한 명만 제출, 한 명은 임시저장
            submit(cand, r1, 1, noisy(truth, .75))
            submit(cand, r2, 1, noisy(truth, .75), status='DRAFT')
            con.execute("UPDATE candidate SET status='검토중' WHERE candidate_id=?", (cand,)); continue
        c1 = submit(cand, r1, 1, noisy(truth, .75)); c2 = submit(cand, r2, 1, noisy(truth, .75))
        if c1 == c2 and c1 in ('채택', '제외'):
            con.execute("UPDATE candidate SET status='결론', final_conclusion=? WHERE candidate_id=?", (c1, cand)); continue
        con.execute("UPDATE candidate SET status='재검토' WHERE candidate_id=?", (cand,))
        if rng.random() < 0.2:                        # 2차 아직 안 함
            continue
        d1 = submit(cand, r1, 2, noisy(truth, .9), deviate=.05)
        d2 = submit(cand, r2, 2, noisy(truth, .9), deviate=.05)
        if d1 == d2 and d1 in ('채택', '제외'):
            con.execute("UPDATE candidate SET status='결론', final_conclusion=? WHERE candidate_id=?", (d1, cand)); continue
        if rng.random() < 0.7:                        # 담당자 결정 완료
            con.execute("UPDATE candidate SET status='결론', final_conclusion=?, decided_by=?, decision_note=? "
                        "WHERE candidate_id=?", (rng.choice(['채택', '제외']), pid, '핵심 방법 비교 후 결정', cand))
        else:
            con.execute("UPDATE candidate SET status='담당자결정' WHERE candidate_id=?", (cand,))
con.commit()
for t in ['team_member', 'project', 'criterion', 'search_term', 'candidate', 'candidate_reviewer', 'review', 'review_item']:
    print(t, con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
print('FK 오류:', len(con.execute('PRAGMA foreign_key_check').fetchall()))
con.close()

'''
이 상태 그대로 실행할 경우 
team_member 5 - 팀원 5명
project 5 - 각자 프로젝트 1개씩 총 5개
criterion 20 - 프로젝트 5개 × 비교요소 4개 = 20개
search_term 65 - 비교요소마다 검색어 3~4개씩 = 65개
candidate 50 - 5명 X 후보 10개 = 50개
candidate_reviewer 99 - 후보 검토자 99명 (한 후보에 검토자 2명씩 배정, 1명만 배정된 후보 1건)
review 126 - 리뷰 126개 (1차 88건 + 2차 38건)
review_item 504 - 리뷰 항목 504개
FK 오류: 0 - 참조 관계가 어긋난 행 없음
'''