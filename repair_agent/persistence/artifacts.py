"""工具输出有界保存：大证据归档，模型与检查点只保留摘要和引用。"""
import hashlib
import json
import time
import uuid
from pathlib import Path

class ArtifactStore:
    # 独立目录保存大结果，文件名由内容哈希生成，不接受用户路径。
    def __init__(self, directory, max_bytes):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.max_bytes=max_bytes

    # 大证据不能截断后冒充完整事实，返回不可用证据并提供预览与原文引用。
    def bound(self, evidence):
        raw=json.dumps(evidence,ensure_ascii=False,default=str).encode()
        if len(raw)<=self.max_bytes: return evidence
        key=hashlib.sha256(raw).hexdigest()
        path=self.directory/(key+'.json')
        temp=self.directory/(key+f'.{uuid.uuid4().hex}.tmp')
        temp.write_bytes(raw);temp.replace(path)
        preview=json.dumps(evidence.get('value'),ensure_ascii=False,default=str)[:500]
        return {**evidence,'status':'UNAVAILABLE','authoritative':False,
                'value':{'error':'RESULT_TOO_LARGE','truncated':True,'bytes':len(raw),
                         'artifact_id':key,'preview':preview,'next_action':'narrow query or inspect archived result'}}

    # 固定哈希校验阻止路径穿越，读取仅供本地调试，不直接塞回模型上下文。
    def read(self, key):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key): raise ValueError('invalid artifact id')
        return json.loads((self.directory/(key+'.json')).read_text())

    # 清理不再被任何工单或检查点引用的过期快照，未决工单引用保持保留。
    def cleanup(self, retention_days, referenced):
        cutoff=time.time()-retention_days*86400
        count=0
        for path in self.directory.glob('*.json'):
            if path.stem not in referenced and path.stat().st_mtime<cutoff:
                path.unlink();count+=1
        return count
