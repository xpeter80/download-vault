"""Public NASA video API read-only acceptance; isolated state, no download submitted."""
import sys,tempfile,json
from pathlib import Path
sys.path.insert(0,'/app')
from server import Vault
with tempfile.TemporaryDirectory() as temp:
 a=Vault(Path(temp)/'state',Path(temp)/'data','/test')
 job=a.discovery.start({'site':'https://images-api.nasa.gov/search?q=moon&media_type=video&page_size=2','keyword':'moon'},background=False)
 print(json.dumps({'status':job['status'],'message':job['message'],'endpoints':job['endpoints'],'pages':job['pages'],'notes':job['notes'],'candidates':[{'url':c['url'],'kind':c['kind'],'verification':c['verification'],'detail':c.get('detail','')} for c in job['candidates']]},ensure_ascii=False),flush=True)
 assert job['status'] in ('done','partial'),job['message']
 assert job['endpoints'],'Search endpoint not recognized'
 assert any(c['verification']=='media' for c in job['candidates']),'No verified public media'
 print('PASS: public search API, result exploration and real media response verification',flush=True)
 a.db.close()
