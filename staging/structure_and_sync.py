from __future__ import annotations
import json, re, sqlite3
from collections import Counter, defaultdict
from pathlib import Path

BASE=Path(__file__).resolve().parent.parent
INPUT=BASE/'staging'/'structured_manifest.jsonl'
OUTPUT=BASE/'staging'/'structured_manifest_final.jsonl'
REPORT=BASE/'staging'/'structured_final_report.json'
DB=BASE/'studyvault.db'
NOISE=[re.compile(r'^Current runtime context\.',re.I),re.compile(r'^The approval policy changed from',re.I),re.compile(r'^A new version of',re.I),re.compile(r'^Current working directory:',re.I)]

def is_noise(text:str)->bool:
    return any(p.search(text.strip()) for p in NOISE)

def summary(messages:list[dict], title:str)->str:
    users=[m['content'].strip() for m in messages if m['speaker']=='user' and m['content'].strip()]
    assistants=[m['content'].strip() for m in messages if m['speaker']=='assistant' and m['content'].strip()]
    def clip(s,n): return re.sub(r'\s+',' ',s).strip()[:n]
    pieces=[]
    if users: pieces.append('用户：'+clip(users[0],140))
    if assistants: pieces.append('助手：'+clip(assistants[0],120))
    return '；'.join(pieces)[:280] or clip(title,280)

def clean_records():
    records=[]; dropped=0; dropped_sessions=0
    for line in INPUT.open(encoding='utf-8'):
        r=json.loads(line); kept=[]
        for m in r['messages']:
            text=m.get('content','').strip()
            if not text or is_noise(text): dropped+=1; continue
            kept.append({'speaker':m['speaker'],'content':text,'created_at':m.get('created_at')})
        if not kept: dropped_sessions+=1; continue
        r['messages']=kept
        if any(x in r.get('title','') for x in ('<recommended_plugins>', '<environment_context>', '<codex_internal_context', 'AGENTS.md instructions', 'The active thread goal')):
            first_user=next((m['content'] for m in kept if m['speaker']=='user'), '')
            r['title']=re.sub(r'\s+',' ',first_user).strip()[:120] or '未命名会话'
        r['summary']=summary(kept,r['title'])
        r['message_count']=len(kept); r['user_message_count']=sum(m['speaker']=='user' for m in kept); r['assistant_message_count']=sum(m['speaker']=='assistant' for m in kept)
        r['tags']=sorted({t for t in r.get('tags',[]) if t and not t.startswith('平台/')})[:8]
        records.append(r)
    with OUTPUT.open('w',encoding='utf-8',newline='\n') as f:
        for r in records: f.write(json.dumps(r,ensure_ascii=False,separators=(',',':'))+'\n')
    return records,dropped,dropped_sessions

def sync(records):
    con=sqlite3.connect(DB); con.row_factory=sqlite3.Row; con.execute('PRAGMA foreign_keys=ON')
    try:
        con.execute('BEGIN')
        sources={r['platform_name']:r['source_id'] for r in con.execute('SELECT source_id,platform_name FROM conversation_source')}
        touched=0; inserted_messages=0; deleted_messages=0; restored_notes=0; tag_links=0; point_links=0
        for r in records:
            source_id=sources[r['platform']]
            c=con.execute('SELECT * FROM conversation WHERE source_id=? AND source_conversation_id=?',(source_id,r['source_id'])).fetchone()
            if not c: continue
            old=list(con.execute('SELECT * FROM message WHERE conversation_id=? ORDER BY seq_no',(c['conversation_id'],)))
            con.execute('UPDATE message SET seq_no=-seq_no-1000000 WHERE conversation_id=?',(c['conversation_id'],))
            buckets=defaultdict(list)
            for row in old: buckets[(row['speaker'],row['content'])].append(row)
            kept_ids=[]; new_ids=[]
            for seq,m in enumerate(r['messages'],1):
                bucket=buckets[(m['speaker'],m['content'])]
                row=bucket.pop(0) if bucket else None
                if row:
                    kept_ids.append(row['message_id'])
                    con.execute('UPDATE message SET seq_no=?,created_at=? WHERE message_id=?',(seq,m.get('created_at'),row['message_id']))
                else:
                    cur=con.execute('INSERT INTO message(conversation_id,seq_no,speaker,content,created_at) VALUES(?,?,?,?,?)',(c['conversation_id'],seq,m['speaker'],m['content'],m.get('created_at')))
                    new_ids.append(cur.lastrowid); inserted_messages+=1
            kept_ids.extend(new_ids)
            old_by_id={row['message_id']:row for row in old}; removed=[row for row in old if row['message_id'] not in kept_ids]
            # Preserve annotations/review cards whose exact message survives; reattach removed ones by content below.
            carried=[]
            for row in removed:
                for a in con.execute('SELECT * FROM annotation WHERE message_id=?',(row['message_id'],)): carried.append(('annotation',dict(a),row['speaker'],row['content']))
                for rv in con.execute('SELECT * FROM review_card WHERE message_id=?',(row['message_id'],)): carried.append(('review',dict(rv),row['speaker'],row['content']))
            if kept_ids:
                marks=','.join('?'*len(kept_ids)); con.execute(f'DELETE FROM message WHERE conversation_id=? AND message_id NOT IN ({marks})',(c['conversation_id'],*kept_ids))
            for kind,data,speaker,content in carried:
                target=con.execute('SELECT message_id FROM message WHERE conversation_id=? AND speaker=? AND content=? ORDER BY seq_no LIMIT 1',(c['conversation_id'],speaker,content)).fetchone()
                if not target: continue
                if kind=='annotation':
                    con.execute('INSERT INTO annotation(message_id,author_id,annotation_type,content,is_resolved,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(target[0],data['author_id'],data['annotation_type'],data['content'],data['is_resolved'],data['created_at'],data['updated_at'])); restored_notes+=1
                else:
                    con.execute('INSERT INTO review_card(message_id,owner_id,question,answer,due_at,interval_days,review_count,status,last_reviewed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(target[0],data['owner_id'],data['question'],data['answer'],data['due_at'],data['interval_days'],data['review_count'],data['status'],data['last_reviewed_at'],data['created_at']))
            con.execute('UPDATE conversation SET title=?,summary=? WHERE conversation_id=?',(r['title'],r['summary'],c['conversation_id']))
            con.execute('DELETE FROM conversation_point WHERE conversation_id=?',(c['conversation_id'],))
            for name in r.get('knowledge_points',[]):
                point=con.execute('SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?',(c['course_id'],name)).fetchone()
                if not point: 
                    con.execute('INSERT OR IGNORE INTO knowledge_point(course_id,point_name,level_no) VALUES(?,?,1)',(c['course_id'],name)); point=con.execute('SELECT point_id FROM knowledge_point WHERE course_id=? AND point_name=?',(c['course_id'],name)).fetchone()
                con.execute('INSERT OR IGNORE INTO conversation_point(conversation_id,point_id,source) VALUES(?,?,?)',(c['conversation_id'],point[0],'structured')) ; point_links+=1
            for tag in r.get('tags',[]):
                con.execute('INSERT OR IGNORE INTO tag(owner_id,tag_name) VALUES(?,?)',(c['owner_id'],tag)); tid=con.execute('SELECT tag_id FROM tag WHERE owner_id=? AND tag_name=?',(c['owner_id'],tag)).fetchone()[0]
                con.execute('INSERT OR IGNORE INTO conversation_tag(conversation_id,tag_id) VALUES(?,?)',(c['conversation_id'],tid)); tag_links+=1
            touched+=1; deleted_messages+=len(removed)
        con.execute('INSERT INTO audit_log(actor_id,operation,table_name,after_data) VALUES(?,?,?,?)',(1,'STRUCTURE','conversation',json.dumps({'sessions':touched,'inserted_messages':inserted_messages,'deleted_messages':deleted_messages,'restored_annotations_reviews':restored_notes},ensure_ascii=False)))
        con.commit()
        return {'touched_sessions':touched,'inserted_messages':inserted_messages,'deleted_messages':deleted_messages,'restored_annotations_reviews':restored_notes,'tag_links_added':tag_links,'point_links_added':point_links}
    except:
        con.rollback(); raise
    finally: con.close()

records,dropped,dropped_sessions=clean_records(); sync_stats=sync(records)
report={'input':str(INPUT),'output':str(OUTPUT),'sessions':len(records),'messages':sum(len(r['messages']) for r in records),'dropped_runtime_noise_messages':dropped,'dropped_empty_sessions':dropped_sessions,'sync':sync_stats,'tags':sorted({t for r in records for t in r.get('tags',[])}),'knowledge_points':sorted({p for r in records for p in r.get('knowledge_points',[])})}
REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps({'sessions':report['sessions'],'messages':report['messages'],'dropped_runtime_noise_messages':dropped,**sync_stats},ensure_ascii=False))
