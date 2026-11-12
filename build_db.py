"""앱이 쓸 DB(patent_app.db) 만들기 — 한 번만 실행

실행:  python build_db.py
필요:  이 파일과 같은 폴더에 patent_final.db, schema_work.sql, triggers.sql
결과:  patent_app.db  (patent_final.db 는 그대로 두고 사본에 만든다)

순서가 중요하다: 사본 → schema_work.sql(업무 테이블 8개) → triggers.sql(트리거 34개, 테이블이 있어야 만들어짐) → 팀원 5명.
이미 patent_app.db 가 있으면 데이터를 지우지 않도록 멈춘다. 처음부터 다시 만들려면 patent_app.db 를 직접 지우고 다시 실행.
"""
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "patent_final.db")
OUT = os.path.join(HERE, "patent_app.db")
SCHEMA = os.path.join(HERE, "schema_work.sql")
TRIGGERS = os.path.join(HERE, "triggers.sql")

# 공개 저장소에 올라가므로 학번은 넣지 않는다 (student_no 는 비워도 되는 컬럼)
TEAM = ["김연준", "최재원", "남건희", "양재훈", "박채연"]


def main():
    for path in (SRC, SCHEMA, TRIGGERS):
        if not os.path.exists(path):
            sys.exit(f"{os.path.basename(path)} 파일이 없습니다. build_db.py 와 같은 폴더에 넣어 주세요.")
    if os.path.exists(OUT):
        sys.exit("patent_app.db 가 이미 있습니다. 새로 만들려면 그 파일을 지운 뒤 다시 실행하세요 (기존 데이터가 지워집니다).")

    shutil.copyfile(SRC, OUT)
    con = sqlite3.connect(OUT)
    try:
        con.executescript(open(SCHEMA, encoding="utf-8").read())
        con.executescript(open(TRIGGERS, encoding="utf-8").read())
        with con:
            con.executemany("INSERT INTO team_member(name) VALUES (?)", [(name,) for name in TEAM])
        tables = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        triggers = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0]
        patents = con.execute("SELECT COUNT(*) FROM patent").fetchone()[0]
        members = con.execute("SELECT COUNT(*) FROM team_member").fetchone()[0]
    except Exception:
        con.close()
        os.remove(OUT)                           # 반쯤 만들어진 파일을 남기지 않는다
        raise
    con.close()
    print(f"patent_app.db 완성: 테이블 {tables}개, 트리거 {triggers}개, 특허 {patents:,}건, 팀원 {members}명")
    print("앱 실행:  streamlit run app2.py")


if __name__ == "__main__":
    main()
