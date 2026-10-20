import cProfile,pstats,io,time
from fastapi import Response
from app.api.steps import list_steps
for i in range(2):
 p=cProfile.Profile(); p.enable(); t=time.perf_counter(); out=list_steps('a0d290',Response()); p.disable(); print('run',i,'sec',time.perf_counter()-t,'items',len(out)); s=io.StringIO(); pstats.Stats(p,stream=s).sort_stats('cumtime').print_stats(40); print(s.getvalue())
