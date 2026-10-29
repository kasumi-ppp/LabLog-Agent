from datetime import datetime,timezone
from lablog.conversations import ConversationIndex

def test_chatgpt_daily_window_boundaries_and_other_sources(tmp_path):
 cfg={'_base':str(tmp_path),'data_dir':'data','chatgpt':{'daily_window':{'enabled':True}}}
 idx=ConversationIndex(cfg)
 times=['2026-10-18T09:59:59+00:00','2026-10-18T10:00:00+00:00','2026-10-19T09:59:59+00:00','2026-10-19T10:00:00+00:00','2026-10-19T10:00:01+00:00']
 with idx._connection(write=True) as c:
  for key in ['chatgpt:one','codex:one']:
   for i,t in enumerate(times):
    c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?)',(key,str(i),'message',t,'{}',0,0,i))
 assert [r['event_id'] for r in idx._events('chatgpt:one','daily','2026-10-19','messages')]==['1','2']
 assert [r['event_id'] for r in idx._events('codex:one','daily','2026-10-19','messages')]==['2','3','4']
 cfg['chatgpt']['daily_window']['enabled']=False
 assert [r['event_id'] for r in idx._events('chatgpt:one','daily','2026-10-19','messages')]==['2','3','4']




