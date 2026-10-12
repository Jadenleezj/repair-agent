"""Local Chinese lexical RAG. Retrieval scores are not correctness probabilities."""
from contextlib import contextmanager
import argparse
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from .models import IssueType
from .playbooks import PLAYBOOKS

class Experience(BaseModel):
    model_config = ConfigDict(extra='forbid')
    case_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    issue_type: IssueType
    description: str = Field(min_length=1, max_length=8000)
    root_cause: str = Field(min_length=1, max_length=2000)
    repair_steps: list[str] = Field(max_length=30)
    prerequisites: list[str] = Field(default_factory=list, max_length=30)
    verification: str = Field(min_length=1, max_length=2000)
    playbook_id: str | None = None
    source: str = Field(min_length=1, max_length=500)

# 提取英文数字词与中文双字片段；无需分词依赖，用于词项检索。
def tokens(text):
    parts = re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+', text.lower())
    return [t for p in parts for t in ([p[i:i+2] for i in range(len(p)-1)] if re.fullmatch(r'[\u4e00-\u9fff]{2,}',p) else [p])]

class KnowledgeStore:
    # 创建知识库目录与案例表；不自动导入示例数据。
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS experiences (case_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    # 打开知识库连接，成功提交、异常回滚，最后始终关闭连接。
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        try:
            with conn:
                yield conn
        finally:
            conn.close()
    # 读取 JSON 数组或 JSONL，整批验证后按 case_id 原子更新；拒绝重复 ID 和未知方案。
    def import_file(self, path):
        path = Path(path)
        text = path.read_text(encoding='utf-8')
        raw = [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix == '.jsonl' else json.loads(text)
        if not isinstance(raw,list):
            raise ValueError('JSON must be an array')
        entries = [Experience.model_validate(x) for x in raw]
        if len({e.case_id for e in entries}) != len(entries):
            raise ValueError('duplicate case_id')
        for e in entries:
            if e.playbook_id and e.playbook_id not in PLAYBOOKS:
                raise ValueError('unknown playbook: '+e.playbook_id)
        with self.connect() as conn:
            conn.executemany('INSERT INTO experiences VALUES (?,?) ON CONFLICT(case_id) DO UPDATE SET payload=excluded.payload', [(e.case_id,e.model_dump_json()) for e in entries])
        return len(entries)
    # 按异常类型过滤案例，用 BM25 排序并返回内容哈希；分数不是修复正确率。
    def search(self, query, issue_type=None, limit=3):
        if not 1 <= limit <= 10 or len(query)>8000:
            raise ValueError('limit must be 1..10; query at most 8000 characters')
        with self.connect() as conn:
            entries = [json.loads(r[0]) for r in conn.execute('SELECT payload FROM experiences ORDER BY case_id')]
        entries = [e for e in entries if issue_type is None or e['issue_type']==issue_type]
        docs = [tokens(e['title']+' '+e['description']+' '+e['root_cause']) for e in entries]
        terms = set(tokens(query))
        if not docs or not terms: return []
        avg = sum(map(len,docs))/len(docs) or 1
        df = {t:sum(t in d for d in docs) for t in terms}
        hits=[]
        for e,d in zip(entries,docs):
            score=0
            for t in terms:
                tf=d.count(t)
                if tf:
                    score+=math.log(1+(len(docs)-df[t]+.5)/(df[t]+.5))*tf*2.2/(tf+1.2*(.25+.75*len(d)/avg))
            if score>0:
                digest=hashlib.sha256(json.dumps(e,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                hits.append({'case':e,'score':round(score,6),'content_hash':digest})
        return sorted(hits,key=lambda h:(-h['score'],h['case']['case_id']))[:limit]

# 解析导入文件及数据库参数，校验并导入历史案例。
def main():
    parser=argparse.ArgumentParser(description='Import repair experiences from JSON/JSONL')
    parser.add_argument('file')
    parser.add_argument('--db',default='data/repair_agent.db.knowledge.db')
    args=parser.parse_args()
    print(f'Imported {KnowledgeStore(args.db).import_file(args.file)} experiences')
if __name__=='__main__': main()
