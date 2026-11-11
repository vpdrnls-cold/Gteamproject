-- G팀 · 공공 특허 데이터 테이블 (데이터 파트 담당: 박채연)
-- 업무 테이블(project, criterion, candidate 등)은 테이블·규칙 담당이 이 파일 뒤에 추가한다.
PRAGMA foreign_keys = ON;

CREATE TABLE patent (
  patent_id          INTEGER PRIMARY KEY,
  application_no     TEXT NOT NULL UNIQUE CHECK (length(application_no) = 13),
  title_ko           TEXT NOT NULL,
  title_en           TEXT,
  application_date   TEXT NOT NULL,                 -- YYYY-MM-DD
  publication_no     TEXT,
  publication_date   TEXT,
  registration_no    TEXT UNIQUE,
  registration_date  TEXT,
  legal_status       TEXT NOT NULL
                     CHECK (legal_status IN ('공개','등록','소멸','취하','거절','포기')),
  legal_status_detail TEXT,                         -- 예: 등록료불납, 존속기간만료
  abstract           TEXT NOT NULL,
  cited_count        INTEGER CHECK (cited_count IS NULL OR cited_count >= 0),  -- 빈칸=NULL(미집계와 0 구분 불가)
  -- 업무규칙 3: 등록번호·등록일은 둘 다 있거나 둘 다 NULL
  CHECK ((registration_no IS NULL) = (registration_date IS NULL)),
  -- 업무규칙 3: 출원일 ≤ 공개일, 출원일 ≤ 등록일
  CHECK (publication_date  IS NULL OR application_date <= publication_date),
  CHECK (registration_date IS NULL OR application_date <= registration_date)
);

CREATE TABLE applicant (
  applicant_id INTEGER PRIMARY KEY,
  name_raw     TEXT NOT NULL UNIQUE,   -- 원문 표기(주소 포함) 그대로 보존
  name_norm    TEXT NOT NULL           -- 주소 제거·공백 정리한 이름(같은 기관 묶음용)
);

CREATE TABLE patent_applicant (
  patent_id    INTEGER NOT NULL REFERENCES patent(patent_id) ON DELETE CASCADE,
  applicant_id INTEGER NOT NULL REFERENCES applicant(applicant_id),
  seq          INTEGER NOT NULL CHECK (seq >= 1),   -- 원문 기재 순서
  PRIMARY KEY (patent_id, applicant_id)
);

CREATE TABLE patent_ipc (
  patent_id    INTEGER NOT NULL REFERENCES patent(patent_id) ON DELETE CASCADE,
  ipc_code     TEXT NOT NULL,                       -- 예: G06N 3/08 (버전 표기 제거)
  ipc_subclass TEXT NOT NULL CHECK (length(ipc_subclass) = 4),  -- 예: G06N
  seq          INTEGER NOT NULL CHECK (seq >= 1),
  PRIMARY KEY (patent_id, ipc_code)
);

-- 검색용 인덱스(출원일 기간·출원인·IPC 조건)
CREATE INDEX idx_patent_date      ON patent(application_date);
CREATE INDEX idx_applicant_norm   ON applicant(name_norm);
CREATE INDEX idx_pa_applicant     ON patent_applicant(applicant_id);
CREATE INDEX idx_pipc_code        ON patent_ipc(ipc_code);
CREATE INDEX idx_pipc_subclass    ON patent_ipc(ipc_subclass);
