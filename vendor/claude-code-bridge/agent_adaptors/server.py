"""Local-only MCP facade."""
import argparse
from contextlib import asynccontextmanager
from .config import load_registry
from .contracts import AdaptorError
from .service import AdaptorService


def build_server(config, *, port=8051):
    from mcp.server.fastmcp import FastMCP
    holder = {}
    @asynccontextmanager
    async def lifespan(server):
        service=AdaptorService(load_registry(config)); holder['service']=service
        try: yield {}
        finally: await service.close(); holder.clear()
    mcp=FastMCP('harness-agent-adaptors',host='127.0.0.1',port=port,lifespan=lifespan)
    async def invoke(method,*args,**kwargs):
        try: return {'ok':True,**await getattr(holder['service'],method)(*args,**kwargs)}
        except AdaptorError as exc: return exc.as_dict()
        except Exception: return {'ok':False,'error':{'code':'internal_error','message':'adaptor operation failed'}}
    @mcp.tool()
    async def agent_runtime_status(): return await holder['service'].registry.probe()
    @mcp.tool()
    async def agent_list_sessions(adaptor_id:str,limit:int=50,cursor:str|None=None): return await invoke('list_sessions',adaptor_id,limit=limit,cursor=cursor)
    @mcp.tool()
    async def agent_start_session(adaptor_id:str,name:str='Harness Assistant'): return await invoke('create_session',adaptor_id,name=name)
    @mcp.tool()
    async def agent_resume_session(session_id:str): return await invoke('resume_session',session_id)
    @mcp.tool()
    async def agent_read_session(session_id:str,max_messages:int=30): return await invoke('read_session',session_id,limit=max_messages)
    @mcp.tool()
    async def agent_read_full_session(session_id:str): return await invoke('read_full_session',session_id)
    @mcp.tool()
    async def agent_send_message(session_id:str,message:str,timeout_seconds:float=180): return await invoke('send_turn',session_id,message,timeout=timeout_seconds)
    @mcp.tool()
    async def agent_start_stream_message(session_id:str,message:str,timeout_seconds:float=180): return await invoke('start_turn',session_id,message,timeout=timeout_seconds)
    @mcp.tool()
    async def agent_poll_stream(stream_id:str,cursor:int=0): return await invoke('poll',stream_id,cursor=cursor)
    @mcp.tool()
    async def agent_interrupt_turn(session_id:str): return await invoke('interrupt',session_id)
    return mcp


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--config',required=True); parser.add_argument('--transport',choices=['stdio','http'],default='stdio'); parser.add_argument('--port',type=int,default=8051); args=parser.parse_args()
    if not 1<=args.port<=65535 or 7950<=args.port<=8049: parser.error('invalid or reserved port; select >=8050')
    build_server(args.config,port=args.port).run(transport='streamable-http' if args.transport=='http' else 'stdio')

if __name__=='__main__': main()
