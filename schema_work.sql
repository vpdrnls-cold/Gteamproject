-- G팀 · 조사·검토 업무 테이블 8개 (테이블·규칙 담당: 남건희)
-- schema_data.sql(공공 데이터 4개) 뒤에 이어서 실행
--
-- 역할 구분
--   이 파일: 한 행만 보고 판단할 수 있는 규칙 (PK·FK·UNIQUE·CHECK·NOT NULL)
--   트리거: 다른 행·다른 표를 봐야 하는 규칙 (검토자 2명, 담당자 제외, 제출 잠금, 재검토, 확정 조건 등)
--
-- 삭제 정책
--   기록을 지키는 쪽은 기본값(NO ACTION): 검토 기록이 있는 후보, 담당 프로젝트가 있는 팀원은 지울 수 없다.
--   딸린 정보만 함께 지움(ON DELETE CASCADE): 비교요소의 검색어, 후보의 검토자 배정, 임시 검토의 판정,
--   후보가 없는 프로젝트의 비교요소.
PRAGMA foreign_keys = ON;

-- 1. 팀원 ------------------------------------------------------------------
CREATE TABLE team_member (
  member_id  INTEGER PRIMARY KEY,
  name       TEXT NOT NULL CHECK (length(trim(name)) > 0),
  student_no TEXT UNIQUE
             CHECK (student_no IS NULL
                    OR (length(student_no) = 8 AND student_no NOT GLOB '*[^0-9]*')),  -- 학번 8자리 숫자
  role       TEXT                                                                       -- 예: 테이블·규칙·ERD
);

-- 2. 조사 프로젝트 ----------------------------------------------------------
CREATE TABLE project (
  project_id INTEGER PRIMARY KEY,
  owner_id   INTEGER NOT NULL REFERENCES team_member(member_id),   -- 담당자 (규칙 4·8의 '담당자')
  title      TEXT NOT NULL CHECK (length(trim(title)) > 0),
  status     TEXT NOT NULL DEFAULT '준비'
             CHECK (status IN ('준비','진행','확정'))                -- 규칙 9·10 (전환 조건은 트리거)
);

-- 3. 비교요소 (프로젝트마다 1~4번) --------------------------------------------
CREATE TABLE criterion (
  criterion_id INTEGER PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  no           INTEGER NOT NULL CHECK (no BETWEEN 1 AND 4),        -- 번호 1~4만
  name         TEXT NOT NULL CHECK (length(trim(name)) > 0),       -- 예: 핵심 방법: 합성곱 신경망
  UNIQUE (project_id, no)                                          -- 한 프로젝트에 같은 번호 두 번 불가 → 최대 4개
);

-- 4. 검색어 (비교요소별 동의어) ------------------------------------------------
CREATE TABLE search_term (
  term_id      INTEGER PRIMARY KEY,
  criterion_id INTEGER NOT NULL REFERENCES criterion(criterion_id) ON DELETE CASCADE,
  term         TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(term)) > 0),
  UNIQUE (criterion_id, term)                                      -- 'CNN'과 'cnn'도 같은 검색어로 보고 중복 차단
);

-- 5. 후보 특허 -------------------------------------------------------------
CREATE TABLE candidate (
  candidate_id     INTEGER PRIMARY KEY,
  project_id       INTEGER NOT NULL REFERENCES project(project_id),
  patent_id        INTEGER NOT NULL REFERENCES patent(patent_id),
  priority         INTEGER CHECK (priority BETWEEN 1 AND 5),
  status           TEXT NOT NULL DEFAULT '미검토'
                   CHECK (status IN ('미검토','검토중','재검토','담당자결정','결론')),
  final_conclusion TEXT CHECK (final_conclusion IN ('채택','제외')),     -- 보류는 최종 결론이 될 수 없음
  decided_by       INTEGER REFERENCES team_member(member_id),            -- 담당자가 결정한 경우만
  decision_note    TEXT CHECK (decision_note IS NULL OR length(trim(decision_note)) > 0),
  UNIQUE (project_id, patent_id),                                        -- 규칙 2: 같은 프로젝트에 같은 특허 두 번 불가
  CHECK (status <> '결론' OR final_conclusion IS NOT NULL),             -- '결론' 상태면 최종 결론이 반드시 있음
  CHECK ((decided_by IS NULL) = (decision_note IS NULL))                -- 규칙 8: 담당자 결정은 결정자와 사유를 함께
);

-- 6. 검토자 배정 (후보 ↔ 팀원) ------------------------------------------------
CREATE TABLE candidate_reviewer (
  candidate_id INTEGER NOT NULL REFERENCES candidate(candidate_id) ON DELETE CASCADE,
  member_id    INTEGER NOT NULL REFERENCES team_member(member_id),
  PRIMARY KEY (candidate_id, member_id)                                  -- 같은 사람 두 번 배정 불가
  -- 정확히 2명·담당자 제외는 트리거(trg_reviewer_max_two, trg_reviewer_not_owner)
);

-- 7. 검토 (검토자 1명의 1차 또는 2차 검토) ---------------------------------------
CREATE TABLE review (
  review_id    INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  member_id    INTEGER NOT NULL,
  round        INTEGER NOT NULL CHECK (round IN (1, 2)),               -- 규칙 8: 1차·2차만
  status       TEXT NOT NULL DEFAULT 'DRAFT'
               CHECK (status IN ('DRAFT','SUBMITTED')),
  conclusion   TEXT CHECK (conclusion IN ('채택','제외','보류')),
  note         TEXT,                                                     -- 검토 메모(선택)
  FOREIGN KEY (candidate_id, member_id)
    REFERENCES candidate_reviewer(candidate_id, member_id),             -- 규칙 4: 배정된 검토자만 작성
  UNIQUE (candidate_id, member_id, round),                               -- 같은 검토자·같은 차수 한 번만
  CHECK (status = 'DRAFT' OR conclusion IS NOT NULL)                     -- 제출된 검토는 결론 필수
);

-- 8. 요소 판정 (검토 ↔ 비교요소) ---------------------------------------------
CREATE TABLE review_item (
  review_id    INTEGER NOT NULL REFERENCES review(review_id) ON DELETE CASCADE,
  criterion_id INTEGER NOT NULL REFERENCES criterion(criterion_id),
  mark         TEXT NOT NULL CHECK (mark IN ('○','△','X')),            -- 규칙 6 (○ U+25CB, △ U+25B3, X 영문 대문자)
  evidence     TEXT NOT NULL CHECK (length(trim(evidence)) > 0),       -- 검토 가이드 4장: 판정마다 근거 한 줄
  PRIMARY KEY (review_id, criterion_id)                                  -- 한 검토에서 같은 비교요소 두 번 판정 불가
  -- 같은 프로젝트의 비교요소인지, 제출 시 4개인지는 트리거(trg_item_same_project, trg_submit_needs_four)
);
