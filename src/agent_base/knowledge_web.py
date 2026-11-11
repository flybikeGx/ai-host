"""Human administration routes and one-use local browser connection."""
import os
import secrets
import difflib

from fastapi import Body,Depends,Request
from fastapi.responses import JSONResponse

from .knowledge import Knowledge,KnowledgeError
from .knowledge_service import KnowledgeService


def admin_secret(store):
    if os.environ.get('AGENT_BASE_WEB_TOKEN'):return os.environ['AGENT_BASE_WEB_TOKEN']
    file=store.root/'admin-token'
    try:
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(secrets.token_urlsafe(32))
    except FileExistsError:pass
    return file.read_text().strip()


def register(app,config):
    store=Knowledge(config);secret=admin_secret(store)
    app.state.knowledge=store

    @app.exception_handler(KnowledgeError)
    async def invalid(request,error):return JSONResponse({'detail':str(error)},status_code=error.status)

    @app.exception_handler(KeyError)
    async def missing(request,error):
        if request.url.path.startswith('/api/knowledge/'):
            return JSONResponse({'detail':'缺少必要字段，请检查提交内容'},status_code=400)
        return JSONResponse({'detail':'内部状态不完整'},status_code=500)

    def administrator(request:Request):
        if secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+secret):return True
        try:store.grant(request.cookies.get('kb_admin',''),'admin');return True
        except KnowledgeError:raise KnowledgeError('请使用 agent-base open 建立本机管理连接，或在连接设置中填写已配置的管理 Token',401)

    def reader(request:Request):
        if os.environ.get('AGENT_BASE_WEB_TOKEN'):administrator(request)

    @app.post('/api/knowledge/connect')
    def connect(body:dict=Body(...)):
        store.grant(body.get('ticket',''),'bootstrap',consume=True)
        token=store.issue('admin',{'actor':'local-admin'},43200)
        result=JSONResponse({'connected':True})
        result.set_cookie('kb_admin',token,max_age=43200,httponly=True,samesite='strict',path='/api/knowledge')
        return result

    @app.get('/api/knowledge/status')
    def status(request:Request):
        try:administrator(request);writable=True
        except KnowledgeError:writable=False
        return {'can_write':writable,'stats':store.stats(),'service':{k:v for k,v in KnowledgeService(config).status().items() if k!='endpoint'}}

    reads=[Depends(reader)];writes=[Depends(administrator)]

    @app.get('/api/knowledge/namespaces',dependencies=reads)
    def namespaces():return store.namespaces()

    @app.post('/api/knowledge/namespaces',dependencies=writes)
    def create_namespace(body:dict=Body(...)):return store.save_namespace(body)

    @app.put('/api/knowledge/namespaces/{namespace_id}',dependencies=writes)
    def update_namespace(namespace_id:str,body:dict=Body(...)):return store.save_namespace(body,namespace_id)

    @app.get('/api/knowledge/bindings',dependencies=writes)
    def bindings():return store.bindings()

    @app.put('/api/knowledge/bindings/{actor}',dependencies=writes)
    def binding(actor:str,body:dict=Body(...)):
        if actor!='admin':
            from .repositories import Repositories
            if not any('repo:'+i['repo_id']==actor for i in Repositories(config).list()):raise KnowledgeError('只能绑定已安装 repo')
        return store.bind(actor,body)

    @app.get('/api/knowledge/documents',dependencies=reads)
    def documents(namespace:str,query:str='',offset:int=0,archived:bool=False):return store.list_documents(namespace,query,offset,archived=archived)

    @app.post('/api/knowledge/documents',dependencies=writes)
    def create_document(body:dict=Body(...)):return store.publish(body['namespace'],body['candidate'])

    @app.get('/api/knowledge/documents/{document_id}',dependencies=reads)
    def document(document_id:str,version_id:str|None=None):return store.document(document_id,version_id)

    @app.put('/api/knowledge/documents/{document_id}',dependencies=writes)
    def update_document(document_id:str,body:dict=Body(...)):
        doc=store.document(document_id)
        return store.publish(doc['namespace_id'],body['candidate'],document_id,body.get('expected_version_id'))

    @app.post('/api/knowledge/documents/{document_id}/archive',dependencies=writes)
    def archive(document_id:str,body:dict=Body(...)):return store.archive(document_id,body.get('expected_version_id'))

    @app.get('/api/knowledge/documents/{document_id}/versions',dependencies=reads)
    def history(document_id:str):return store.history(document_id)

    @app.get('/api/knowledge/submissions',dependencies=writes)
    def submissions(namespace:str,status:str='pending',offset:int=0):return store.submissions(namespace,status,offset=offset)

    @app.get('/api/knowledge/submissions/{submission_id}',dependencies=writes)
    def submission(submission_id:str):
        result=store.submission(submission_id)
        if result['document_id']:
            result['base']=store.document(result['document_id'],result['base_version_id']) if result['base_version_id'] else None
            result['current']=store.document(result['document_id'])
        before=result.get('base',{}).get('candidate',{}).get('body_markdown','') if result.get('base') else ''
        result['diff']=''.join(difflib.unified_diff(before.splitlines(keepends=True),result['candidate']['body_markdown'].splitlines(keepends=True),fromfile='base',tofile='candidate'))
        return result

    @app.post('/api/knowledge/submissions/{submission_id}/review',dependencies=writes)
    def review(submission_id:str,body:dict=Body(...)):
        return store.review(submission_id,body['action'],body['expected_hash'],body.get('expected_version_id'),body.get('note',''),body.get('amended'))
